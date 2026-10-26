"""零碎批次验证：模型管理 4 + 会话待办 2 + 对话附件 3。

重点验证这些**静默失败**：

1. **todos 的参数名陷阱**：Java 参数叫 `parentRequestId`，SQL 查的却是
   `metadata->>'requestId'`（注释原文承认按 requestId 查）—— 照抄，别"修正"。
2. **todos 快照迭代覆盖**：同一轮里 write_todos 可能被调用多次（先初稿再细化），
   只留最后一次成功的快照；失败的结果（success≠true）不参与覆盖。
3. **附件路径前缀即信任边界**：`sessions/{sid}/` 开头校验会话归属、
   `chat/{aid}/temp/` 开头校验 agentId 一致，其他前缀一律拒删 ——
   少了这道校验，拿到别人 objectKey 就能删别人的文件。
4. **Agent 能力开关**：allowFileUpload 未开启时上传必须 403（Java BizException）。
5. **type 三分类上限**：image 1MB / video 5MB / document 5MB。

用法：
    python -m scripts.misc2_check
"""

from __future__ import annotations

import asyncio
import json
import time

import httpx
from sqlalchemy import text

from app.core.db import SessionLocal
from app.core.snowflake import new_id

BASE = "http://127.0.0.1:18080"
failures = 0
_STAMP = str(int(time.time()))[-6:]
_UID = 2090273750973657090
_SESSION_ID: int | None = None
_AGENT_ID: int | None = None


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
    """建一个 agent + 会话 + 带 write_todos 工具事件的消息。"""
    global _SESSION_ID, _AGENT_ID
    aid = new_id()
    sid = new_id()
    mid = new_id()
    from datetime import datetime

    now = datetime.now()
    rid = f"zz-rid-{_STAMP}"
    async with SessionLocal() as db:
        await db.execute(text(
            "INSERT INTO agent (id, name, description, status, config, create_time, "
            "update_time, deleted, user_id) VALUES (:i, 'zz-agent', '', 'draft', "
            "CAST(:cfg AS jsonb), :t, :t, 0, :u)"),
            {"i": aid, "cfg": json.dumps({"allowFileUpload": True}), "t": now, "u": _UID},
        )
        await db.execute(text(
            "INSERT INTO chat_session (id, agent_id, user_id, title, create_time, "
            "update_time, deleted) VALUES (:i, :a, :u, 'zz-todo-session', :t, :t, 0)"),
            {"i": sid, "a": aid, "u": _UID, "t": now},
        )
        # 第一轮：write_todos 初稿（2 条）→ 第二版（3 条）；中间夹一次失败结果
        events = [
            {"type": "tool_result", "toolName": "write_todos", "toolCallId": "t1",
             "result": json.dumps({"success": True, "todos": [
                 {"id": "1", "content": "初稿A", "status": "pending"},
                 {"id": "2", "content": "初稿B", "status": "pending"},
             ]}, ensure_ascii=False)},
            {"type": "tool_result", "toolName": "write_todos", "toolCallId": "t2",
             "result": json.dumps({"success": False, "todos": [
                 {"id": "x", "content": "失败的快照不应生效", "status": "pending"},
             ]}, ensure_ascii=False)},
            {"type": "tool_result", "toolName": "write_todos", "toolCallId": "t3",
             "result": json.dumps({"success": True, "todos": [
                 {"id": "1", "content": "细化A", "status": "completed"},
                 {"id": "2", "content": "细化B", "status": "in_progress"},
                 {"id": "3", "content": "新增C", "status": "pending"},
             ]}, ensure_ascii=False)},
        ]
        await db.execute(text(
            "INSERT INTO message (id, session_id, role, content, content_type, token_count, "
            "metadata, tool_events, create_time, message_type) "
            "VALUES (:i, :s, 'assistant', '处理中', 'text', 0, "
            "CAST(:meta AS jsonb), CAST(:events AS jsonb), :t, 'text')"),
            {"i": mid, "s": sid,
             "meta": json.dumps({"requestId": rid}, ensure_ascii=False),
             "events": json.dumps(events, ensure_ascii=False), "t": now},
        )
        await db.commit()
    _SESSION_ID, _AGENT_ID = sid, aid


async def cleanup() -> None:
    async with SessionLocal() as db:
        await db.execute(text("DELETE FROM message WHERE session_id IN "
                              "(SELECT id FROM chat_session WHERE title='zz-todo-session')"))
        await db.execute(text("DELETE FROM chat_session WHERE title='zz-todo-session'"))
        await db.execute(text("DELETE FROM agent WHERE name='zz-agent'"))
        await db.execute(text("DELETE FROM model WHERE model_id LIKE 'zz-m-%'"))
        await db.commit()


async def check_models(c: httpx.AsyncClient) -> None:
    print("\n[1] 模型管理 4")

    # 先造一个 provider（直插，避免依赖 model-providers 的必填链路）
    async with SessionLocal() as db:
        pid = new_id()
        await db.execute(text(
            "INSERT INTO model_provider (id, name, type, base_url, api_key, status, "
            "create_time, update_time, deleted) VALUES (:i, 'zz-prov', 'OPENAI', "
            "'http://x', 'k', 'active', now(), now(), 0)"),
            {"i": pid},
        )
        await db.commit()

    r = await api(c, "POST", "/api/models", json={
        "providerId": str(pid), "modelId": f"zz-m-{_STAMP}", "name": "自检模型", "type": "llm"})
    d = data(r) or {}
    ok("创建成功 / status=active", r.get("code") == 200 and d.get("status") == "active",
       str(d)[:150])
    ok("id 是字符串", isinstance(d.get("id"), str))
    model_row = int(d["id"])

    r = await api(c, "POST", "/api/models", json={
        "providerId": str(pid), "modelId": f"zz-m-{_STAMP}", "name": "重复"})
    ok("同 provider 下 modelId 重复 400", r.get("code") == 400, str(r)[:100])

    r = await api(c, "GET", f"/api/models/by-provider/{pid}")
    items = data(r) or []
    ok("by-provider 命中", any(int(x["id"]) == model_row for x in items), f"len={len(items)}")

    r = await api(c, "GET", "/api/models/by-type/llm")
    ok("by-type 只出 active 且按类型过滤",
       all(x.get("type") == "llm" for x in (data(r) or [])), f"len={len(data(r) or [])}")

    r = await api(c, "DELETE", f"/api/models/{model_row}")
    ok("删除成功", r.get("code") == 200, str(r)[:100])
    r = await api(c, "GET", f"/api/models/by-provider/{pid}")
    ok("删后列表消失", not any(int(x["id"]) == model_row for x in (data(r) or [])))

    async with SessionLocal() as db:
        await db.execute(text("DELETE FROM model_provider WHERE id=:i"), {"i": pid})
        await db.commit()


async def check_todos(c: httpx.AsyncClient) -> None:
    print("\n[2] 会话待办 2")

    r = await api(c, "GET",
                  f"/api/sessions/{_SESSION_ID}/todos?parentRequestId=zz-rid-{_STAMP}")
    todos = data(r) or []
    ok("按 requestId 取到**最后一次成功**的快照（3 条细化版）",
       len(todos) == 3 and todos[0].get("content") == "细化A", str(todos)[:200])
    ok("todo 字段 id/content/status",
       all({"id", "content", "status"} <= set(t.keys()) for t in todos))
    ok("失败快照未生效（无'失败'字样）",
       all("不应生效" not in (t.get("content") or "") for t in todos))

    r = await api(c, "GET", f"/api/sessions/{_SESSION_ID}/todos/latest")
    ok("latest 同样取最后成功快照", len(data(r) or []) == 3, str(len(data(r) or [])))

    r = await api(c, "GET", f"/api/sessions/{_SESSION_ID}/todos")
    ok("parentRequestId 缺省返回空列表（不算错误）", data(r) == [], str(r)[:100])

    r = await api(c, "GET", "/api/sessions/999999999999999999/todos/latest")
    ok("别人的/不存在会话 404", r.get("code") == 404, str(r)[:100])


async def check_attachments(c: httpx.AsyncClient) -> None:
    print("\n[3] 对话附件 3")

    r = await api(c, "POST", "/api/chat/attachments",
                  data={"agentId": str(_AGENT_ID), "sessionId": str(_SESSION_ID)},
                  files={"file": ("zz-note.txt", "附件内容".encode(), "text/plain")})
    d = data(r) or {}
    ok("上传成功 / type=document", r.get("code") == 200 and d.get("type") == "document",
       str(d)[:180])
    ok("objectKey 在 sessions/ 下（会话级）", (d.get("objectKey") or "").startswith(
        f"sessions/{_SESSION_ID}/inputs/"), str(d.get("objectKey")))
    ok("预签名 URL 已生成", bool(d.get("previewUrl")), str(d.get("previewUrl"))[:80])
    ok("txt 解析出文本无 warning", not d.get("warning"), str(d.get("warning")))

    # refresh-preview
    r = await api(c, "POST", "/api/chat/attachments/refresh-preview", json=[d])
    refreshed = data(r) or []
    ok("refresh 返回新 URL", len(refreshed) == 1 and bool(refreshed[0].get("previewUrl")),
       str(refreshed[:1])[:120])

    # 越权删除：伪造别人的 objectKey
    evil = dict(d, objectKey="sessions/999999999999999999/inputs/evil.txt")
    r = await api(c, "DELETE",
                  f"/api/chat/attachments?agentId={_AGENT_ID}&sessionId={_SESSION_ID}",
                  json=evil)
    ok("删别人的会话附件被拒（400）", r.get("code") == 400, str(r)[:120])

    evil2 = dict(d, objectKey="chat/999/temp/evil.txt")
    r = await api(c, "DELETE",
                  f"/api/chat/attachments?agentId={_AGENT_ID}&sessionId={_SESSION_ID}",
                  json=evil2)
    ok("chat/ 前缀但 agentId 不匹配被拒", r.get("code") == 400, str(r)[:100])

    evil3 = dict(d, objectKey="elsewhere/evil.txt")
    r = await api(c, "DELETE",
                  f"/api/chat/attachments?agentId={_AGENT_ID}&sessionId={_SESSION_ID}",
                  json=evil3)
    ok("未知前缀一律拒删", r.get("code") == 400, str(r)[:100])

    # 正常删除
    r = await api(c, "DELETE",
                  f"/api/chat/attachments?agentId={_AGENT_ID}&sessionId={_SESSION_ID}",
                  json=d)
    ok("删除自己的附件成功", r.get("code") == 200, str(r)[:100])

    # 能力开关：造一个未开启上传的 agent
    async with SessionLocal() as db:
        aid = new_id()
        await db.execute(text(
            "INSERT INTO agent (id, name, description, status, config, create_time, "
            "update_time, deleted, user_id) VALUES (:i, 'zz-agent-no', '', 'draft', "
            "'{}'::jsonb, now(), now(), 0, :u)"),
            {"i": aid, "u": _UID},
        )
        await db.commit()
    r = await api(c, "POST", "/api/chat/attachments",
                  data={"agentId": str(aid)},
                  files={"file": ("zz.txt", b"x", "text/plain")})
    ok("allowFileUpload 未开启 → 403", r.get("code") == 403, str(r)[:120])
    async with SessionLocal() as db:
        await db.execute(text("DELETE FROM agent WHERE id=:i"), {"i": aid})
        await db.commit()


async def main() -> int:
    await cleanup()
    await seed()
    async with httpx.AsyncClient(timeout=60, trust_env=False) as c:
        await check_models(c)
        await check_todos(c)
        await check_attachments(c)
    await cleanup()

    print(f"\n{'=' * 60}")
    print(f"失败 {failures} 项")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
