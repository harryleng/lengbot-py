"""RAG 检索：pgvector 向量召回。

对照 Java：`EmbeddingMapper.searchSimilarWithThreshold` + `KnowledgeSearchService`。

**关键实现细节**：

1. **`SET LOCAL hnsw.ef_search`** —— HNSW 索引的召回深度。
   默认 40，调到 100 召回率从 ~90% 提到 ~98%，代价约 +10~20ms。
   `SET LOCAL` 只影响当前事务，不污染连接池里的其他查询
   （如果是普通 `SET`，这个连接被放回池子后，下一个用户会继承你的设置）。
   范围必须是 [topK, 1000]，小于 topK 会直接漏召回。

2. **为什么用 `<=>` 而不是 `<->`** —— 三个 pgvector 距离运算符：
   - `<->` L2 距离
   - `<#>` 负内积
   - `<=>` 余弦距离 ← 文本 embedding 用这个
   `1 - 距离` 就是相似度分数，越大越相关。

3. **向量要 CAST** —— asyncpg 不认识 pgvector 类型，只能传字符串再
   `CAST(:vec AS vector)`。

4. **检索范围来自 Agent 绑定** —— 不是全库搜。`current_knowledge_ids()`
   从 ContextVar 拿，见 `app/tools/context.py`。
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import bindparam, text

from app.core.db import SessionLocal
from app.services.embedding_client import embed_query, to_pgvector

logger = logging.getLogger(__name__)

DEFAULT_TOP_K = 5
HNSW_EF_SEARCH = 100


async def search(
    query: str,
    knowledge_ids: list[int],
    *,
    top_k: int = DEFAULT_TOP_K,
    min_score: float | None = 0.3,
) -> list[dict[str, Any]]:
    """向量检索。返回按相似度倒序的 chunk 列表。"""
    if not query.strip() or not knowledge_ids:
        return []

    vector = to_pgvector(await embed_query(query))

    stmt = text(
        """
        SELECT c.id            AS chunk_id,
               c.document_id   AS document_id,
               c.knowledge_id  AS knowledge_id,
               c.chunk_index   AS chunk_index,
               c.content       AS content,
               d.name          AS document_name,
               1 - (e.vector <=> CAST(:vec AS vector)) AS score
        FROM embedding e
        JOIN chunk c ON c.id = e.chunk_id
        LEFT JOIN document d ON d.id = c.document_id
        WHERE c.knowledge_id IN :kb_ids
        ORDER BY e.vector <=> CAST(:vec AS vector)
        LIMIT :top_k
        """
    ).bindparams(bindparam("kb_ids", expanding=True))

    async with SessionLocal() as db:
        # 只在当前事务内提高召回深度，事务结束自动失效
        await db.execute(text(f"SET LOCAL hnsw.ef_search = {int(HNSW_EF_SEARCH)}"))
        rows = (
            (
                await db.execute(
                    stmt,
                    {"vec": vector, "kb_ids": list(knowledge_ids), "top_k": top_k},
                )
            )
            .mappings()
            .all()
        )

    results = []
    for row in rows:
        item = dict(row)
        score = float(item.get("score") or 0.0)
        if min_score is not None and score < min_score:
            continue
        item["score"] = round(score, 4)
        results.append(item)
    return results


async def knowledge_chunk_counts(knowledge_ids: list[int]) -> dict[int, int]:
    """各知识库的可用向量数，用来给出"为什么搜不到"的准确原因。

    实测发现：Agent 绑定的那个库有 3097 个 chunk 但 **0 条 embedding**
    （从未向量化成功），检索会永远返回空。这种"静默失败"必须能被诊断出来。
    """
    if not knowledge_ids:
        return {}
    stmt = text(
        """
        SELECT k.id AS knowledge_id,
               count(DISTINCT c.id) AS chunks,
               count(DISTINCT e.id) AS embeddings
        FROM knowledge k
        LEFT JOIN chunk c ON c.knowledge_id = k.id
        LEFT JOIN embedding e ON e.chunk_id = c.id
        WHERE k.id IN :kb_ids
        GROUP BY k.id
        """
    ).bindparams(bindparam("kb_ids", expanding=True))
    async with SessionLocal() as db:
        rows = (await db.execute(stmt, {"kb_ids": list(knowledge_ids)})).mappings().all()
    return {int(r["knowledge_id"]): int(r["embeddings"] or 0) for r in rows}
