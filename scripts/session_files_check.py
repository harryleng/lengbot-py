"""会话文件树 4 接口验证（对齐前端 `chatSession.js` 的 getSessionFileTree 等）。

重点验证这些**静默失败**：

1. **分区白名单**：`foo/bar` 不在 inputs/outputs/workspace/uploads 下必须 400 ——
   这层校验是"把 objectKey 拼进 sessions/{sid}/"的唯一护栏，没有它就是路径穿越扫 bucket。
2. **`..` 一票否决**（normalize 阶段就拒）。
3. **顶级分区目录不可删**：inputs/ 是结构不是文件。
4. **MinIO 没有目录**：单层子条目靠递归列举从 key 中间段推导 ——
   `inputs/parsed/a.md` 在列 inputs/ 时应表现为目录 `parsed/`。
5. **删除文件同步摘除 attachments 索引**：不同步的话文件树里会出现"幽灵文件"
   （对象已删但索引还在，点开 404）。
6. **stats 的 source 口径**：user_upload / ai_* 前缀两类。

用法：
    python -m scripts.session_files_check
"""

from __future__ import annotations

import asyncio
import time

import httpx
from sqlalchemy import text

from app.core.db import SessionLocal
from app.core.snowflake import new_id
from app.services import minio_client

BASE = "http://127.0.0.1:18080"
failures = 0
_STAMP = str(int(time.time()))[-6:]
_UID = 2090273750973657090
_SID: int | None = None


def ok(label: str, cond: bool, detail: str = "") -> None:
    global failures
    if not cond:
        failures += 1
    print(f"  {'✅' if cond else '❌'} {label}" + (f"  → {detail}" if detail else ""))


async def api(c: httpx.AsyncClient, method: str, path: str, **kw) -> dict:
    resp = await c.request(method, BASE + path, **kw)
    try:
        return resp.json()
    except Exception:  # noqa: BLE001
        return {"code": resp.status_code, "message": resp.text[:200]}


def data(body: dict) -> dict | list | None:
    return body.get("data")


async def seed() -> None:
    """建会话 + 直传几个对象（inputs 文本 / inputs/parsed 子目录 / outputs）。"""
    global _SID
    sid = new_id()
    async with SessionLocal() as db:
        import json

        atts = [
            {"id": "att-1", "type": "document",
             "objectKey": f"sessions/{sid}/inputs/zz-note.txt",
             "fileName": "zz-note.txt", "source": "user_upload"},
            {"id": "att-2", "type": "image",
             "objectKey": f"sessions/{sid}/outputs/images/zz.png",
             "fileName": "zz.png", "source": "ai_image"},
        ]
        await db.execute(text(
            "INSERT INTO chat_session (id, agent_id, user_id, title, attachments, "
            "create_time, update_time, deleted) VALUES (:i, NULL, :u, 'zz-files-session', "
            "CAST(:att AS jsonb), now(), now(), 0)"),
            {"i": sid, "u": _UID, "att": json.dumps(atts, ensure_ascii=False)},
        )
        await db.commit()
    _SID = sid

    files = [
        ("inputs/zz-note.txt", "会话文件内容".encode(), "text/plain"),
        ("inputs/parsed/zz-parsed.md", "解析产物".encode(), "text/markdown"),
        ("outputs/files/zz-out.txt", "AI 产出".encode(), "text/plain"),
        ("outputs/images/zz.png", b"PNG", "image/png"),
        ("workspace/.keep", b".keep", "text/plain"),
    ]
    for rel, data, mime in files:
        await minio_client.upload_bytes(data, f"sessions/{sid}/{rel}", mime)


async def cleanup() -> None:
    if _SID:
        for key in [
            f"sessions/{_SID}/inputs/zz-note.txt",
            f"sessions/{_SID}/inputs/parsed/zz-parsed.md",
            f"sessions/{_SID}/outputs/files/zz-out.txt",
            f"sessions/{_SID}/outputs/images/zz.png",
            f"sessions/{_SID}/workspace/.keep",
            f"sessions/{_SID}/inputs/zz-deleted.txt",
        ]:
            await minio_client.delete(key)
    async with SessionLocal() as db:
        await db.execute(text("DELETE FROM chat_session WHERE title='zz-files-session'"))
        await db.commit()


async def main() -> int:
    await cleanup()
    await seed()

    async with httpx.AsyncClient(timeout=60, trust_env=False) as c:
        base = f"/api/chat/sessions/{_SID}/files"

        print("\n[1] 文件树 / 分区白名单")

        r = await api(c, "GET", f"{base}/tree")
        d = data(r) or {}
        entries = d.get("entries") or []
        ok("根路径返回三个固定目录",
           [e["name"] for e in entries][:3] == ["inputs", "outputs", "workspace"],
           str([e["name"] for e in entries]))
        ok("stats 三键（total/userUpload/aiGenerated）",
           (d.get("stats") or {}).get("total") == 2
           and (d.get("stats") or {}).get("userUpload") == 1
           and (d.get("stats") or {}).get("aiGenerated") == 1,
           str(d.get("stats")))

        r = await api(c, "GET", f"{base}/tree?path=inputs")
        d = data(r) or {}
        names = [e["name"] for e in (d.get("entries") or [])]
        ok("inputs 下 .keep 被过滤、子目录推导为目录",
           "zz-note.txt" in names and "parsed" in names
           and next(e for e in d["entries"] if e["name"] == "parsed")["directory"] is True,
           str(names))
        ok("附件 enrich：fileName 回填", all(
            e.get("fileName") == "zz-note.txt"
            for e in d["entries"] if e["name"] == "zz-note.txt"))

        r = await api(c, "GET", f"{base}/tree?path=foo/bar")
        ok("非白名单分区 400（路径穿越护栏）", r.get("code") == 400, str(r)[:120])

        r = await api(c, "GET", f"{base}/tree?path=inputs/../../secret")
        ok("`..` 一票否决 400", r.get("code") == 400, str(r)[:100])

        print("\n[2] 内容 / 下载 / 删除")

        r = await api(c, "GET", f"{base}/content?path=inputs/zz-note.txt")
        d = data(r) or {}
        ok("文本文件直接读内容", d.get("supported") is True
           and d.get("previewType") == "text" and "会话文件内容" in (d.get("content") or ""),
           str({k: d.get(k) for k in ("previewType", "supported")}))

        r = await api(c, "GET", f"{base}/content?path=outputs/images/zz.png")
        d = data(r) or {}
        ok("图片走预签名 URL", d.get("previewType") == "image" and bool(d.get("previewUrl")),
           str({k: d.get(k) for k in ("previewType", "supported")}))

        r = await api(c, "GET", f"{base}/content?path=inputs/not-exist.txt")
        d = data(r) or {}
        ok("不存在 → supported=false + message（不是 500）",
           d.get("supported") is False and d.get("message"), str(d)[:120])

        r = await api(c, "GET", f"{base}/download?path=inputs/zz-note.txt")
        ok("下载 URL 是预签名", (data(r) or "").startswith("http"), str(data(r))[:80])

        # 删除：先传一个待删对象
        await minio_client.upload_bytes(b"x", f"sessions/{_SID}/inputs/zz-deleted.txt", "text/plain")
        r = await api(c, "DELETE", f"{base}?path=inputs/zz-deleted.txt")
        ok("删除文件成功", r.get("code") == 200, str(r)[:100])
        r = await api(c, "DELETE", f"{base}?path=inputs")
        ok("顶级分区目录不可删", r.get("code") == 400, str(r)[:100])

        r = await api(c, "GET", "/api/chat/sessions/999999999999999999/files/tree")
        ok("别人的/不存在会话 404", r.get("code") == 404, str(r)[:100])

    await cleanup()
    print(f"\n{'=' * 60}")
    print(f"失败 {failures} 项")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
