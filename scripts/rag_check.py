"""RAG 链路验证。

验证策略很关键：**用 chunk 自己的内容片段当查询**，看能不能把它召回来。
这叫"自命中测试"——如果查不到自己，说明链路某个环节（模型/维度/归一化/索引）
有问题；如果查得到，说明整条链路是通的。

跑法：
    uv run python -m scripts.rag_check
"""

from __future__ import annotations

import asyncio
import json

from sqlalchemy import text

from app.core.db import SessionLocal
from app.services.rag_service import knowledge_chunk_counts, search
from app.tools.context import AgentToolContext, reset_context, set_context
from app.tools.registry import registry

PS2_KB = 2094693967276167170  # 有 161 条向量（唯一可检索的库）
MILVUS_KB = 2094074885853978626  # Agent 绑定的库，3097 chunks 但 0 向量


async def main() -> None:
    print("=" * 70)
    print("1. 各知识库的向量情况")
    print("=" * 70)
    counts = await knowledge_chunk_counts([PS2_KB, MILVUS_KB])
    for kb, emb in counts.items():
        flag = "✅ 可检索" if emb > 0 else "❌ 无向量，检索必然为空"
        print(f"  knowledge_id={kb}  embeddings={emb}  {flag}")

    print()
    print("=" * 70)
    print("2. 取 ps-2 的样本内容，用它的片段当查询（自命中测试）")
    print("=" * 70)
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                text(
                    "SELECT c.id, c.content FROM chunk c "
                    "JOIN embedding e ON e.chunk_id = c.id "
                    "WHERE c.knowledge_id = :kb ORDER BY c.id LIMIT 3"
                ),
                {"kb": PS2_KB},
            )
        ).all()
    if not rows:
        print("没有可用样本")
        return

    for chunk_id, content in rows:
        snippet = " ".join(str(content).split())[:40]
        print(f"\n查询（来自 chunk {chunk_id} 的前 40 字）: {snippet!r}")
        results = await search(snippet, [PS2_KB], top_k=3)
        if not results:
            print("  ❌ 没检索到任何内容")
            continue
        hit = [r for r in results if r["chunk_id"] == chunk_id]
        print(f"  返回 {len(results)} 条，自命中: {'✅ 是' if hit else '❌ 否'}")
        for r in results:
            mark = "→" if r["chunk_id"] == chunk_id else " "
            print(
                f"  {mark} score={r['score']:.4f}  "
                f"{r.get('document_name') or '?'}  {str(r['content'])[:50]!r}"
            )

    print()
    print("=" * 70)
    print("3. 工具层：query_knowledge（走 ContextVar 注入知识库）")
    print("=" * 70)
    spec = registry.get("query_knowledge")
    assert spec is not None

    token = set_context(AgentToolContext(agent_id=None, user_id=None, knowledge_ids=[PS2_KB]))
    try:
        out = await spec.handler(question="什么是向量检索")
        data = json.loads(out)
        print(f"  ok={data.get('ok')} count={data.get('count')}")
        for item in (data.get("results") or [])[:2]:
            print(f"  - {item['documentName']} score={item['score']}: {item['content'][:60]}")
    finally:
        reset_context(token)

    print()
    print("=" * 70)
    print("4. 工具层：agent 实际绑定的库（Milvus）→ 应给出明确诊断而不是空结果")
    print("=" * 70)
    token = set_context(AgentToolContext(agent_id=None, user_id=None, knowledge_ids=[MILVUS_KB]))
    try:
        out = await spec.handler(question="任意问题")
        print(f"  {out[:300]}")
    finally:
        reset_context(token)


if __name__ == "__main__":
    asyncio.run(main())
