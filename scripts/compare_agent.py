"""手写循环 vs LangGraph：同一个任务，两种实现，对比事件流与结果。

跑法：
    uv run python -m scripts.compare_agent
"""

from __future__ import annotations

import asyncio
import json
import time

from langchain_core.messages import HumanMessage, SystemMessage

from app.agents.langgraph_agent import astream_events
from app.core.constants import DEFAULT_SYSTEM_PROMPT
from app.core.db import SessionLocal
from app.models.entities import Agent, ModelProvider
from app.services.llm_client import LlmConfig
from app.tools.registry import registry as tool_registry

AGENT_ID = 2099681584963604481
TASK = "请在工作区创建文件 notes/lg_test.md，内容写「LangGraph 写入成功」，然后用读取工具读回来告诉我内容"


async def main() -> None:
    async with SessionLocal() as db:
        agent = await db.get(Agent, AGENT_ID)
        if agent is None:
            print(f"Agent {AGENT_ID} 不存在")
            return
        cfg_map = agent.config or {}
        provider = await db.get(ModelProvider, int(cfg_map["providerId"]))
        pairs = await tool_registry.resolve_with_meta(db, cfg_map)

    assert provider is not None
    cfg = LlmConfig(
        base_url=provider.base_url or "",
        api_key=provider.api_key or "",
        model=str(cfg_map.get("modelId")),
        max_tokens=int(cfg_map.get("maxTokens") or 2048),
        temperature=cfg_map.get("temperature"),
        top_p=cfg_map.get("topP"),
    )
    specs = [spec for spec, _ in pairs]
    print(f"模型: {cfg.model} @ {cfg.base_url}")
    print(f"工具: {[s.name for s in specs]}")
    print()

    messages = [
        SystemMessage(content=(agent.system_prompt or "").strip() or DEFAULT_SYSTEM_PROMPT),
        HumanMessage(content=TASK),
    ]

    print("=" * 70)
    print("LangGraph 事件流（只打印关键事件）")
    print("=" * 70)
    text_parts: list[str] = []
    tool_rounds = 0
    started = time.time()
    seen_events: dict[str, int] = {}

    async for ev in astream_events(cfg, specs, messages, max_steps=5):
        kind = ev.get("event", "")
        seen_events[kind] = seen_events.get(kind, 0) + 1

        if kind == "on_chat_model_stream":
            chunk = ev["data"].get("chunk")
            content = getattr(chunk, "content", None)
            if content:
                text_parts.append(str(content))
        elif kind == "on_tool_start":
            tool_rounds += 1
            payload = json.dumps(ev["data"].get("input"), ensure_ascii=False)
            print(f"\n[tool_start] {ev.get('name')}  input={payload}")
        elif kind == "on_tool_end":
            output = ev["data"].get("output")
            snippet = str(getattr(output, "content", output))[:120].replace("\n", " ")
            print(f"[tool_end]   {ev.get('name')}  output={snippet}")
        elif kind == "on_chain_end" and ev.get("name") == "LangGraph":
            print(f"\n[graph_end] 耗时 {int((time.time() - started) * 1000)}ms")

    print()
    print("=" * 70)
    print("汇总")
    print("=" * 70)
    print(f"工具调用次数: {tool_rounds}")
    print(f"正文字符数: {len(''.join(text_parts))}")
    print(f"总耗时: {int((time.time() - started) * 1000)}ms")
    print("\n框架发出的事件类型统计（这就是 LangGraph 提供的信息量）:")
    for name, count in sorted(seen_events.items(), key=lambda kv: -kv[1]):
        print(f"  {name:<28} × {count}")
    print(f"\n模型最终回答:\n{''.join(text_parts)[:400]}")


if __name__ == "__main__":
    asyncio.run(main())
