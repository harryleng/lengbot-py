"""工作流引擎验证：跑库里真实的 7 节点流程。

流程：start → input → retrieval → variable_handle → llm → output → end

跑法：
    uv run python -m scripts.workflow_check
"""

from __future__ import annotations

import asyncio
import json

from sqlalchemy import text

from app.core.db import SessionLocal
from app.workflow.executor import WorkflowExecutor
from app.workflow.graph import WorkflowGraph
from app.workflow.nodes import WorkflowContext

AGENT_WORKFLOW = 2092221234394365954  # 示例：RAG 知识问答助手（7 节点）
TEST_QUERY = "OpenAI 刚把多 Agent 做成产品，o1 奠基人认为多 Agent 贡献多大？"


async def load_workflow(agent_id: int):
    async with SessionLocal() as db:
        row = (
            await db.execute(
                text(
                    "SELECT av.config, a.config AS agent_config, a.system_prompt "
                    "FROM agent_version av JOIN agent a ON a.id = av.agent_id "
                    "WHERE av.agent_id = :aid AND av.node_count > 0 AND av.deleted = 0 "
                    "ORDER BY av.version DESC LIMIT 1"
                ),
                {"aid": agent_id},
            )
        ).first()
        if row is None:
            return None
        av_config, agent_config, system_prompt = row

        provider_id = (agent_config or {}).get("providerId")
        provider = None
        if provider_id:
            provider = (
                await db.execute(
                    text("SELECT base_url, api_key FROM model_provider WHERE id = :id"),
                    {"id": int(provider_id)},
                )
            ).first()
    return av_config, agent_config, system_prompt, provider


async def main() -> None:
    loaded = await load_workflow(AGENT_WORKFLOW)
    if loaded is None:
        print(f"Agent {AGENT_WORKFLOW} 没有工作流版本")
        return
    av_config, agent_config, system_prompt, provider = loaded

    raw_graph = (av_config or {}).get("graph") or {}
    graph = WorkflowGraph.parse(raw_graph)
    print("=" * 70)
    print(graph.describe())
    print("=" * 70)
    print(f"入口节点: {graph.entry_node()}\n")

    cfg = agent_config or {}
    ctx = WorkflowContext(
        # workflow 型 Agent 的 config 通常是空的，模型配置在 LLM 节点里
        # （node.data.providerId / modelId），这里只提供兜底值
        llm_base_url=provider[0] if provider else "",
        llm_api_key=provider[1] if provider else "",
        llm_model=str(cfg.get("modelId") or "deepseek-flash"),
        llm_max_tokens=int(cfg.get("maxTokens") or 2048),
        knowledge_ids=[int(k) for k in (cfg.get("knowledges") or []) if str(k).isdigit()],
    )
    print(f"兜底模型: {ctx.llm_model}（节点自带 provider 时以节点为准）")
    print(f"Agent 级知识库: {ctx.knowledge_ids or '无（用节点里的 knowledgeId）'}\n")

    executor = WorkflowExecutor(graph, ctx)
    print("=" * 70)
    print(f"执行（输入 query={TEST_QUERY!r}）")
    print("=" * 70)
    async for ev in executor.run({"query": TEST_QUERY}):
        t = ev.get("type")
        if t == "workflow_node_start":
            print(f"  ▶ {ev['nodeLabel']:<12} ({ev['nodeType']}) step={ev['step']}")
        elif t == "workflow_node_end":
            status = ev.get("status")
            mark = "✅" if status == "success" else "❌"
            print(f"  {mark} {ev['nodeLabel']:<12} handle={ev.get('handle')}  {str(ev.get('output'))[:100]}")
            if status == "error":
                print(f"     错误: {ev.get('message')}")
        elif t == "workflow_done":
            print(f"\n完成: steps={ev.get('steps')} error={ev.get('error')}")
            out = ev.get("output")
            print(f"最终输出:\n{str(out)[:600]}")
        elif t == "workflow_suspended":
            payload = json.dumps(ev.get("payload"), ensure_ascii=False)[:200]
            print(f"\n⏸ 挂起于 {ev.get('nodeLabel')}: {payload}")


if __name__ == "__main__":
    asyncio.run(main())
