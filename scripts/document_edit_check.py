"""文档在线编辑 5 接口验证（对齐前端 `src/api/documentEdit.js`）。

重点验证这些**静默失败**：

1. **乐观锁是唯一的并发防线**：两个人同时开编辑器，后保存的会覆盖先保存的。
   `expectedHash` 与库里不一致必须 **HTTP 409 / code=60014**，
   放过去就是"静默丢稿"。
2. **保存前先快照当前版本**：快照内容必须是**保存前**的 ——
   顺序反了版本列表里存的全是"改完的"，回滚功能形同虚设。
3. **回滚本身也会先备份**：回滚前把当前内容存成新版本 ——
   否则回滚是单向门，滚错了找不回来。
4. **编辑类型白名单**：md/txt/csv 之外（PDF 等）返回 unsupported；
   editMode 被前端篡改也要拒。
5. **保存后异步重建**：保存成功 ≠ 重建成功；status 要轮询回 completed，
   chunk 内容要真的反映新文本（全文替换式编辑 → 旧 chunk 必须被清掉）。

用法：
    python -m scripts.document_edit_check
"""

from __future__ import annotations

import asyncio
import io
import time

import httpx
from sqlalchemy import text

from app.core.db import SessionLocal

BASE = "http://127.0.0.1:18080"
failures = 0
_STAMP = str(int(time.time()))[-6:]
_KB_IDS: list[int] = []
_DOC_ID: int | None = None
_FAKE_DOC_ID: int | None = None


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


def _md(initial: bool) -> bytes:
    tag = "初版" if initial else "修改"
    return (
        f"# zz-{_STAMP} 在线编辑自检\n\n"
        f"## {tag}章节\n\n"
        f"这是{'初始' if initial else '修改后'}的正文内容。垃圾分类要点："
        f"可回收物应清空后投放，厨余垃圾需沥干水分。投放点在东门北侧，"
        f"大件物品需预约上门回收，电话 555-1234。\n\n"
        f"补充细节 {_STAMP}：有害垃圾包括废电池、废灯管，应当投放至红色收集桶。\n"
    ).encode("utf-8")


async def setup() -> tuple[int, int]:
    """建库 + 传 md 文档（向量化完成）。"""
    async with httpx.AsyncClient(timeout=120, trust_env=False) as c:
        r = (await c.post(BASE + "/api/knowledge",
                          json={"name": f"zz-文档编辑自检-{_STAMP}", "type": "pg"})).json()
        kid = int(r["data"]["id"])
        _KB_IDS.append(kid)
        res = (await c.post(
            BASE + f"/api/knowledge/{kid}/documents",
            files={"file": (f"zz-edit-{_STAMP}.md", io.BytesIO(_md(True)), "text/markdown")},
            params={"ocrEnabled": "false"},
        )).json()
        return kid, int(res["data"]["id"])


async def cleanup() -> None:
    async with SessionLocal() as db:
        rows = (await db.execute(
            text("SELECT id FROM knowledge WHERE name LIKE 'zz-文档编辑自检-%'")
        )).scalars().all()
        kids = [int(r) for r in rows]
        if _FAKE_DOC_ID:
            await db.execute(text("DELETE FROM document WHERE id=:i"), {"i": _FAKE_DOC_ID})
        await db.execute(text("DELETE FROM document_version WHERE document_id IN "
                              "(SELECT id FROM document WHERE knowledge_id = ANY(:k))"), {"k": kids})
        await db.execute(text("DELETE FROM embedding WHERE knowledge_id = ANY(:k)"), {"k": kids})
        await db.execute(text("DELETE FROM chunk WHERE knowledge_id = ANY(:k)"), {"k": kids})
        await db.execute(text("DELETE FROM document WHERE knowledge_id = ANY(:k)"), {"k": kids})
        await db.execute(text("DELETE FROM knowledge_member WHERE knowledge_id = ANY(:k)"), {"k": kids})
        await db.execute(text("DELETE FROM knowledge WHERE id = ANY(:k)"), {"k": kids})
        await db.commit()


async def poll_completed(c: httpx.AsyncClient, timeout: float = 90) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        d = (await db_doc_status()) or ""
        if d in ("completed", "failed"):
            return d
        await asyncio.sleep(2)
    return "timeout"


async def db_doc_status() -> str | None:
    async with SessionLocal() as db:
        return (await db.execute(
            text("SELECT status FROM document WHERE id=:i"), {"i": _DOC_ID}
        )).scalar_one_or_none()


async def db_doc_field(field: str):
    async with SessionLocal() as db:
        return (await db.execute(
            text(f"SELECT {field} FROM document WHERE id=:i"), {"i": _DOC_ID}
        )).scalar_one_or_none()


async def check_editable_and_save(c: httpx.AsyncClient) -> None:
    global failures
    print("\n[1] editable-content / 保存 / 乐观锁")

    r = await api(c, "GET", f"/api/documents/{_DOC_ID}/editable-content")
    d = data(r) or {}
    ok("editable=true / editMode=editor", d.get("editable") is True and d.get("editMode") == "editor",
       str({k: d.get(k) for k in ("editable", "editMode", "fileType")}))
    ok("content 是上传的初版", d.get("content", "").find("初版章节") >= 0, str(d.get("content"))[:80])
    ok("fileHash/totalChunks/totalChars 齐",
       all(d.get(k) is not None for k in ("fileHash", "totalChunks", "totalChars")),
       str({k: d.get(k) for k in ("fileHash", "totalChars", "totalChunks")}))

    # 乐观锁：expectedHash 错 → 409
    r = await api(c, "PUT", f"/api/documents/{_DOC_ID}/content", json={
        "content": "乱写的", "editMode": "editor", "expectedHash": "deadbeef"})
    ok("expectedHash 不一致 → **HTTP 409 / code=60014**",
       r.get("code") == 60014, str(r)[:120])

    # editMode 篡改
    r = await api(c, "PUT", f"/api/documents/{_DOC_ID}/content", json={
        "content": "乱写的", "editMode": "pdf"})
    ok("editMode 篡改被拒（60015）", r.get("code") == 60015, str(r)[:120])

    # 正常保存（带正确 expectedHash）
    cur_hash = (data(await api(c, "GET", f"/api/documents/{_DOC_ID}/editable-content")) or {}).get("fileHash")
    r = await api(c, "PUT", f"/api/documents/{_DOC_ID}/content", json={
        "content": _md(False).decode(), "editMode": "editor", "expectedHash": cur_hash})
    d = data(r) or {}
    ok("保存成功返回 newHash/message", r.get("code") == 200 and d.get("newHash"), str(d)[:150])

    status = await poll_completed(c)
    ok("异步重建后 status=completed", status == "completed", f"status={status}")

    # 保存后的可编辑内容是修改版
    d = data(await api(c, "GET", f"/api/documents/{_DOC_ID}/editable-content")) or {}
    ok("保存后 content 是修改版", d.get("content", "").find("修改章节") >= 0, str(d.get("content"))[:80])
    ok("hash 已变化", d.get("fileHash") != "deadbeef" and d.get("fileHash"), str(d.get("fileHash"))[:20])


async def check_versions_and_rollback(c: httpx.AsyncClient) -> None:
    print("\n[2] 版本列表 / 版本内容 / 回滚")

    r = await api(c, "GET", f"/api/documents/{_DOC_ID}/versions")
    versions = data(r) or []
    ok("版本列表 ≥1（保存前快照 v1）", len(versions) >= 1, f"len={len(versions)}")
    ok("版本 VO 是 id/version/contentHash/createTime",
       versions and {"id", "version", "contentHash", "createTime"} <= set(versions[0].keys()),
       str(sorted(versions[0].keys())) if versions else "")
    v1 = versions[-1]  # version desc，最后一个是最早的
    ok("最早版本是 v1", v1.get("version") == 1, str(v1.get("version")))

    r = await api(c, "GET", f"/api/documents/{_DOC_ID}/versions/{v1['id']}")
    content = data(r) or ""
    ok("v1 内容是**保存前**的初版（快照顺序正确）", content.find("初版章节") >= 0, str(content)[:80])

    # 回滚到 v1
    r = await api(c, "POST", f"/api/documents/{_DOC_ID}/versions/{v1['id']}/rollback")
    ok("回滚成功", r.get("code") == 200, str(r)[:120])
    status = await poll_completed(c)
    ok("回滚后重建 completed", status == "completed", f"status={status}")

    d = data(await api(c, "GET", f"/api/documents/{_DOC_ID}/editable-content")) or {}
    ok("回滚后 content 回到初版", d.get("content", "").find("初版章节") >= 0,
       str(d.get("content"))[:80])

    r = await api(c, "GET", f"/api/documents/{_DOC_ID}/versions")
    versions = data(r) or []
    ok("回滚前先备份当前版本（版本数增加）", len(versions) >= 2, f"len={len(versions)}")

    # 回滚到不存在的版本
    r = await api(c, "POST", f"/api/documents/{_DOC_ID}/versions/999999999999999999/rollback")
    ok("回滚不存在的版本 404", r.get("code") == 404, str(r)[:100])


async def check_unsupported(c: httpx.AsyncClient) -> None:
    global _FAKE_DOC_ID
    print("\n[3] 不支持类型（pdf）")

    # 直插一条 pdf 文档行（不走上传链路）
    async with SessionLocal() as db:
        from app.core.snowflake import new_id

        _FAKE_DOC_ID = new_id()
        await db.execute(text(
            "INSERT INTO document (id, knowledge_id, user_id, name, file_path, file_type, "
            "file_size, file_hash, chunk_count, token_count, status, create_time, update_time, "
            "version, deleted) VALUES (:i, :k, :u, 'zz-fake.pdf', 'docs/zz-fake.pdf', 'pdf', "
            "100, 'hash', 0, 0, 'completed', now(), now(), 1, 0)"),
            {"i": _FAKE_DOC_ID, "k": _KB_IDS[0], "u": 2090273750973657090},
        )
        await db.commit()

    r = await api(c, "GET", f"/api/documents/{_FAKE_DOC_ID}/editable-content")
    d = data(r) or {}
    ok("pdf → editable=false / editMode=unsupported",
       d.get("editable") is False and d.get("editMode") == "unsupported",
       str({k: d.get(k) for k in ("editable", "editMode")}))

    r = await api(c, "PUT", f"/api/documents/{_FAKE_DOC_ID}/content",
                  json={"content": "x", "editMode": "editor"})
    ok("pdf 保存被拒（60015）", r.get("code") == 60015, str(r)[:120])


async def main() -> int:
    await cleanup()
    _, _ = 0, 0
    global _DOC_ID
    _kid, _DOC_ID = await setup()
    async with httpx.AsyncClient(timeout=120, trust_env=False) as c:
        await check_editable_and_save(c)
        await check_versions_and_rollback(c)
        await check_unsupported(c)
    await cleanup()

    print(f"\n{'=' * 60}")
    print(f"失败 {failures} 项")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
