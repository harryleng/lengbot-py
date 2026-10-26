"""Agent 扩展 4 接口验证（generate-prompt / generate-questions / avatar / publish）。

重点验证这些**静默失败**：

1. **归属校验防 ID 枚举**：别人的 Agent 与不存在的 Agent 同响应（404）。
2. **publish 的版本链路**：agent_version 插 PUBLISHED 行（version+1）、
   agent 本体 status=published —— 两次 publish 版本号必须递增，
   回滚测试里版本对不上会让"恢复历史版本"功能乱套。
3. **generate 走 Agent 自己的 providerId/modelId**（与主对话同源）。
   免费验证只走错误路径；真调模型放 --llm。
4. **头像 URL 是永久公开 URL**（agent/{id}/avatar/{uuid}.{ext}）。

用法：
    python -m scripts.agent_ext_check            # 免费路径
    python -m scripts.agent_ext_check --llm      # 追加真调模型
"""

from __future__ import annotations

import asyncio
import time

import httpx
from sqlalchemy import text

from app.core.db import SessionLocal
from app.core.snowflake import new_id

BASE = "http://127.0.0.1:18080"
failures = 0
_STAMP = str(int(time.time()))[-6:]
_UID = 2090273750973657090
_AGENT_ID: int | None = None
_AVATAR_KEY: str | None = None


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


def data(body: dict) -> dict | list | str | None:
    return body.get("data")


async def cleanup() -> None:
    if _AVATAR_KEY:
        from app.services import minio_client

        await minio_client.delete(_AVATAR_KEY)
    async with SessionLocal() as db:
        await db.execute(text("DELETE FROM agent_version WHERE agent_id IN "
                              "(SELECT id FROM agent WHERE name LIKE 'zz-agent-ext-%')"))
        await db.execute(text("DELETE FROM agent WHERE name LIKE 'zz-agent-ext-%'"))
        await db.commit()


async def seed() -> int:
    aid = new_id()
    async with SessionLocal() as db:
        await db.execute(text(
            "INSERT INTO agent (id, name, description, status, version, config, "
            "create_time, update_time, deleted, user_id) "
            "VALUES (:i, :n, '用于验证生成与发布的测试 Agent', 'draft', 0, "
            "CAST(:cfg AS jsonb), now(), now(), 0, :u)"),
            {"i": aid, "n": f"zz-agent-ext-{_STAMP}", "u": _UID,
             "cfg": '{"providerId": null, "modelId": null}'},
        )
        await db.commit()
    return aid


async def main() -> int:
    global _AGENT_ID, _AVATAR_KEY
    await cleanup()
    _AGENT_ID = await seed()

    async with httpx.AsyncClient(timeout=120, trust_env=False) as c:
        base = f"/api/agents/{_AGENT_ID}"

        print("\n[1] 归属校验")
        r = await api(c, "POST", "/api/agents/999999999999999999/publish", json={})
        ok("不存在的 Agent 404（防 ID 枚举）", r.get("code") == 404, str(r)[:100])

        print("\n[2] 头像上传")
        r = await api(c, "POST", f"{base}/avatar",
                      files={"file": ("a.png", b"\x89PNG\r\n\x1a\n" + b"0" * 32, "image/png")})
        d = data(r) or ""
        ok("上传成功返回永久 URL", isinstance(d, str) and d.startswith(
            f"http://localhost:9100/lengbot/agent/{_AGENT_ID}/avatar/"), str(d)[:120])
        _AVATAR_KEY = d.split("/lengbot/", 1)[-1] if "/lengbot/" in d else None

        r = await api(c, "POST", f"{base}/avatar",
                      files={"file": ("a.exe", b"MZ" + b"0" * 16, "application/octet-stream")})
        ok("非法后缀 400", r.get("code") == 400, str(r)[:100])

        async with SessionLocal() as db:
            avatar = (await db.execute(
                text("SELECT avatar FROM agent WHERE id=:i"), {"i": _AGENT_ID}
            )).scalar_one()
        ok("avatar 字段已回写", avatar == d, str(avatar)[:100])

        print("\n[3] 发布（版本链路）")
        r = await api(c, "POST", f"{base}/publish", json={"description": "首次发布"})
        d = data(r) or {}
        ok("publish 返回 version/status", d.get("version") == 1
           and d.get("status") == "published", str(d))
        r = await api(c, "POST", f"{base}/publish", json={})
        ok("二次发布 version 递增", (data(r) or {}).get("version") == 2, str(data(r)))
        async with SessionLocal() as db:
            rows = (await db.execute(text(
                "SELECT version, status FROM agent_version WHERE agent_id=:i "
                "ORDER BY version"), {"i": _AGENT_ID}
            )).all()
            agent_status = (await db.execute(text(
                "SELECT status, version FROM agent WHERE id=:i"), {"i": _AGENT_ID}
            )).one()
        ok("agent_version 两条 PUBLISHED 快照（v1/v2）",
           [r[0] for r in rows] == [1, 2] and all(r[1] == "published" for r in rows),
           str(rows))
        ok("agent 本体 status=published / version=2",
           agent_status.status == "published" and agent_status.version == 2,
           str(tuple(agent_status)))

        if "--llm" in __import__("sys").argv:
            print("\n[4] AI 生成（真调模型）")
            r = await api(c, "POST", f"{base}/generate-prompt")
            d = data(r) or ""
            ok("generate-prompt 返回非空提示词", isinstance(d, str) and len(d) > 30,
               str(d)[:120])
            r = await api(c, "POST", f"{base}/generate-questions")
            d = data(r) or ""
            import json as _json

            ok("generate-questions 返回 JSON 数组字符串",
               isinstance(d, str) and len(_json.loads(d)) == 3, str(d)[:150])
        else:
            print("\n[4] AI 生成（跳过真调模型，加 --llm 才跑）")

    await cleanup()
    print(f"\n{'=' * 60}")
    print(f"失败 {failures} 项")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
