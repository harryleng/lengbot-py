"""验证 checkpointer + interrupt 的三个场景。

    1. 写文件任务 → 图挂起在 approval 节点（HITL）
    2. Command(resume="approve") → 继续执行，文件写入
    3. 另一个 thread → Command(resume="reject") → 模型重新措辞

跑法：
    uv run python -m scripts.durable_check
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from typing import Any

# 必须在建事件循环之前切到 SelectorEventLoop：
# Windows 默认的 ProactorEventLoop 与 psycopg 异步模式不兼容。
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from langchain_core.messages import HumanMessage, SystemMessage  # noqa: E402
from langgraph.types import Command  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.agents.langgraph_durable import (  # noqa: E402
    APPROVAL_REQUIRED,
    build_durable_graph,
    close_checkpointer,
    get_checkpointer,
)
from app.core.db import SessionLocal  # noqa: E402
from app.models.entities import Agent, ModelProvider  # noqa: E402
from app.services.llm_client import LlmConfig  # noqa: E402
from app.tools.registry import registry as tool_registry  # noqa: E402

AGENT_ID = 2099681584963604481
WRITE_TASK = "请在工作区创建文件 notes/durable.md，内容写「HITL 测试通过」，然后告诉我结果"
READ_TASK = "请列出工作区 notes 目录下有哪些文件"


async def load_agent():
    async with SessionLocal() as db:
        agent = await db.get(Agent, AGENT_ID)
        assert agent is not None, f"Agent {AGENT_ID} 不存在"
        cfg_map = agent.config or {}
        provider = await db.get(ModelProvider, int(cfg_map["providerId"]))
        assert provider is not None
        pairs = await tool_registry.resolve_with_meta(db, cfg_map)
    cfg = LlmConfig(
        base_url=provider.base_url or "",
        api_key=provider.api_key or "",
        model=str(cfg_map.get("modelId")),
        max_tokens=int(cfg_map.get("maxTokens") or 2048),
        temperature=cfg_map.get("temperature"),
        top_p=cfg_map.get("topP"),
    )
    return agent, cfg, [s for s, _ in pairs]


async def drain(graph, payload: Any, config: dict) -> list[dict]:
    """跑一遍图，收集事件；遇到 interrupt 会自然结束并返回 __interrupt__。"""
    events: list[dict] = []
    async for ev in graph.astream(payload, config, stream_mode="updates"):
        for node, update in ev.items():
            if node == "__interrupt__":
                print(f"  ⏸  挂起 @ __interrupt__: {json.dumps(update[0].value, ensure_ascii=False)[:200]}")
                events.append({"node": node, "update": update})
            else:
                msgs = (update or {}).get("messages") if isinstance(update, dict) else None
                desc = ""
                if msgs:
                    last = msgs[-1]
                    kind = type(last).__name__
                    snippet = str(getattr(last, "content", ""))[:80].replace("\n", " ")
                    desc = f"{kind}: {snippet}"
                print(f"  ▶ 节点 {node} {desc}")
                events.append({"node": node, "update": update})
    return events


async def main() -> None:
    agent, cfg, specs = await load_agent()
    saver = await get_checkpointer()
    graph, recursion_limit = build_durable_graph(cfg, specs, saver, max_steps=5)
    print(f"模型: {cfg.model}")
    print(f"工具: {[s.name for s in specs]}")
    print(f"需人工确认的工具: {sorted(APPROVAL_REQUIRED)}\n")

    system = (agent.system_prompt or "").strip() or "你是一个乐于助人的 AI 助手。"

    # ---------------- 场景 1：挂起 ----------------
    thread = f"demo-{int(time.time())}"
    config = {"configurable": {"thread_id": thread}, "recursion_limit": recursion_limit}
    print("=" * 70)
    print(f"场景 1：写文件任务（thread_id={thread}）")
    print("=" * 70)
    await drain(
        graph,
        {"messages": [SystemMessage(content=system), HumanMessage(content=WRITE_TASK)]},
        config,
    )

    state = await graph.aget_state(config)
    print(f"\n挂起后状态: next={state.next}")
    if state.tasks:
        for task in state.tasks:
            for itr in getattr(task, "interrupts", ()) or ():
                print(f"  interrupt.value = {json.dumps(itr.value, ensure_ascii=False)[:300]}")

    # 查库：checkpoint 真的落盘了吗
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                text("SELECT count(*) AS n FROM checkpoints WHERE thread_id = :t"),
                {"t": thread},
            )
        ).first()
        print(f"\n库里 checkpoints 行数（thread={thread}）: {rows[0] if rows else 0}")

        tables = (
            (
                await db.execute(
                    text("SELECT tablename FROM pg_tables WHERE tablename LIKE 'checkpoint%' ORDER BY 1")
                )
            )
            .scalars()
            .all()
        )
        print(f"checkpointer 自动创建的表: {list(tables)}")

    # ---------------- 场景 2：批准 ----------------
    print()
    print("=" * 70)
    print("场景 2：resume('approve') → 继续执行")
    print("=" * 70)
    await drain(graph, Command(resume="approve"), config)

    state2 = await graph.aget_state(config)
    print(f"\n结束后 next={state2.next}（空表示已完成）")

    # 历史回放：每一步都能取出来
    print("\ncheckpoint 历史（每步一条，可回放）:")
    async for snap in graph.aget_state_history(config):
        step = snap.metadata.get("step") if snap.metadata else "?"
        source = snap.metadata.get("source") if snap.metadata else "?"
        nxt = snap.next or ("END" if not snap.next else "")
        print(f"  step={step:<3} source={source:<8} next={nxt or 'END'}")

    # ---------------- 场景 3：拒绝 ----------------
    thread2 = f"demo-reject-{int(time.time())}"
    config2 = {"configurable": {"thread_id": thread2}, "recursion_limit": recursion_limit}
    print()
    print("=" * 70)
    print(f"场景 3：reject（thread_id={thread2}）")
    print("=" * 70)
    await drain(
        graph,
        {"messages": [SystemMessage(content=system), HumanMessage(content=WRITE_TASK)]},
        config2,
    )
    print("\n→ 回复 reject")
    await drain(graph, Command(resume="reject"), config2)

    await close_checkpointer()


if __name__ == "__main__":
    asyncio.run(main())
