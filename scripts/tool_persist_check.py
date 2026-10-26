"""工具调用持久化端到端验证：跑一轮真实对话，核对**三处落库**都对了。

验证链路（缺一不可）：

    1. message.tool_events  ← 前端刷新后渲染工具卡片靠它
    2. tool_calls 表         ← 前端「查看结果」按 toolCallId 回查完整原文
    3. /tool-calls/{id}/result ← 上面那条的接口，要能取回且有越权保护

用法：
    python -m scripts.tool_persist_check
    python -m scripts.tool_persist_check --agent 2099681584963604481
"""

from __future__ import annotations

import asyncio
import json
import sys

from sqlalchemy import text

from app.core.config import settings
from app.core.db import SessionLocal
from app.repositories.chat_session_repo import ChatSessionRepository
from app.schemas.chat import ChatRequestIn
from app.services.chat_session_service import ChatSessionService
from app.services.chat_stream_service import ChatStreamService

AGENT_ID = "2099681584963604481"  # 未来已来
QUESTION = "请用 execute_code 工具计算 2 的 10 次方，只调一次工具，然后报告结果"


def unwrap(frame: str) -> str:
    for line in frame.splitlines():
        if line.startswith("data:"):
            return line[5:]
    return frame


async def main() -> None:
    args = sys.argv[1:]
    agent_id = AGENT_ID
    if "--agent" in args:
        agent_id = args[args.index("--agent") + 1]
    question = QUESTION
    if "--ask" in args:
        question = args[args.index("--ask") + 1]

    print(f"=== 提问: {question}  (agent={agent_id}) ===")
    svc = ChatStreamService()
    req = ChatRequestIn(message=question, agentId=agent_id)
    assistant_id: str | None = None
    tool_calls_seen: list[str] = []

    async for frame in svc.stream(req, settings.dev_user_id):
        payload = unwrap(frame)
        if payload.startswith("[STATUS]"):
            try:
                p = json.loads(payload[8:])
            except json.JSONDecodeError:
                continue
            if p.get("type") == "tool_call":
                tool_calls_seen.append(p.get("toolCallId") or "")
                print(f"  >> {p.get('toolName')}  toolCallId={p.get('toolCallId')}")
            elif p.get("type") == "tool_result":
                print(f"  << result 前 120 字: {str(p.get('result'))[:120]}")
            elif p.get("type") == "error":
                print(f"  !! {p.get('message')}")
        elif payload.startswith("[DONE]"):
            meta = json.loads(payload[6:] or "{}")
            assistant_id = meta.get("assistantMessageId")
            print(f"  [DONE] assistantMessageId={assistant_id}")

    if not assistant_id:
        print("\n❌ 没拿到 assistantMessageId，无法继续校验")
        raise SystemExit(1)

    failures = 0

    def ok(label: str, cond: bool, detail: str = "") -> None:
        nonlocal failures
        if not cond:
            failures += 1
        print(f"  {'✅' if cond else '❌'} {label}" + (f"  → {detail}" if detail else ""))

    async with SessionLocal() as db:
        print("\n--- 1. message.tool_events ---")
        row = (
            await db.execute(
                text("SELECT tool_events FROM message WHERE id = :id"),
                {"id": int(assistant_id)},
            )
        ).first()
        events = (row[0] if row else None) or []
        ok(f"tool_events 已落库（{len(events)} 个事件）", len(events) > 0)
        types = [e.get("type") for e in events if isinstance(e, dict)]
        ok("包含 tool_call 事件", "tool_call" in types, str(types))
        ok("包含 tool_result 事件", "tool_result" in types, str(types))
        call_evt = next((e for e in events if e.get("type") == "tool_call"), None)
        ok("tool_call 带 toolCallId", bool(call_evt and call_evt.get("toolCallId")))
        ok("tool_call 带 contentOffset", call_evt is not None and "contentOffset" in call_evt)
        ok("tool_call.args 是字符串（前端直接渲染）", isinstance((call_evt or {}).get("args"), str))

        print("\n--- 2. tool_calls 表 ---")
        tcs = (
            await db.execute(
                text(
                    "SELECT id, tool_name, status, tool_output, tool_input, error_message "
                    "FROM tool_calls WHERE message_id = :mid ORDER BY id"
                ),
                {"mid": int(assistant_id)},
            )
        ).mappings().all()
        ok(f"落库 {len(tcs)} 条记录", len(tcs) == len(tool_calls_seen), f"事件里 {len(tool_calls_seen)} 条")
        if tcs:
            tc = tcs[0]
            ok("id 与 SSE 的 toolCallId 一致", str(tc["id"]) in tool_calls_seen, f"{tc['id']}")
            ok("tool_input 是 JSON 对象/数组（不是字符串包着 JSON）", not isinstance(tc["tool_input"], str))
            ok("status 合法", tc["status"] in ("success", "error"), str(tc["status"]))
            ok("tool_output 非空", bool(tc["tool_output"]))
            print(f"     tool_name={tc['tool_name']} status={tc['status']} "
                  f"output_len={len(tc['tool_output'] or '')}")
            print(f"     tool_input={json.dumps(tc['tool_input'], ensure_ascii=False)[:200]}")

        print("\n--- 3. 详情接口 ---")
        if tcs:
            tid = tcs[0]["id"]
            async with SessionLocal() as db2:
                s = ChatSessionService(ChatSessionRepository(db2))
                detail = await s.get_tool_result_detail(tid, settings.dev_user_id)
                ok("能取回完整结果", bool(detail))
                ok("与库里 tool_output 完全一致", detail == (tcs[0]["tool_output"] or ""))
                # 越权：换一个不存在的 user 应该拿到 404
                try:
                    await s.get_tool_result_detail(tid, 999999999999999999)
                    ok("越权校验生效", False, "陌生人竟然读到了结果")
                except Exception as e:  # noqa: BLE001
                    ok("越权校验生效", True, type(e).__name__)

        print("\n--- 4. 历史列表瘦身 ---")
        async with SessionLocal() as db3:
            s = ChatSessionService(ChatSessionRepository(db3))
            session_id = (
                await db3.execute(
                    text("SELECT session_id FROM message WHERE id = :id"),
                    {"id": int(assistant_id)},
                )
            ).scalar()
            page = await s.list_messages(session_id, settings.dev_user_id, 1, 5)
            target = next((m for m in page.records if str(m.id) == assistant_id), None)
            ok("列表里能拿到这条消息", target is not None)
            if target:
                list_events = target.tool_events or []
                r = next((e for e in list_events if e.get("type") == "tool_result"), None)
                if r:
                    if "result" in r:
                        ok("短结果保留原文（≤500 字）", len(str(r["result"])) <= 500,
                           f"len={len(str(r['result']))}")
                    else:
                        ok("长结果已瘦身并带 resultTotalLength", "resultTotalLength" in r,
                           str(r.get("resultTotalLength")))
                    ok("瘦身后 toolCallId 仍在", bool(r.get("toolCallId")))

        print("\n--- 5. 库里原始数据没被瘦身污染 ---")
        row2 = (
            await db.execute(
                text("SELECT tool_events FROM message WHERE id = :id"),
                {"id": int(assistant_id)},
            )
        ).first()
        raw_events = (row2[0] if row2 else None) or []
        raw_r = next((e for e in raw_events if e.get("type") == "tool_result"), None)
        if raw_r:
            raw_len = len(str(raw_r.get("result") or ""))
            ok("库里仍是完整结果（瘦身只发生在 DTO 层）", raw_len > 0, f"库里 {raw_len} 字")

    print()
    if failures:
        print(f"❌ 失败 {failures} 项")
        raise SystemExit(1)
    print("✅ 全部通过")


if __name__ == "__main__":
    asyncio.run(main())
