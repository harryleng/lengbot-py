"""知识库检索测试：`POST /knowledge/{id}/search`。

对照 Java `RagServiceImpl.search(knowledgeId, question, overrides)`。

## 检索

**两路并行**：chunk（文档分块）+ qa_pair（问答对），结果里 **QA 排前面**
（`qa_priority`，默认 true），因为问答对是人工/AI 校验过的高质量答案，
命中比原始分块更可信。

## 参数解析优先级

    overrides > query_params > config > 默认值

和 `RagParamResolver` 一致。`overrides` 是前端"检索测试"面板临时调参用的，
不落库；`query_params` 是持久化配置。别搞反。

## 一个必须说清楚的坑：`deleted=0` / `status='vectorized'`

chunk 检索 SQL 必须带这两个过滤，否则：

- 软删文档的向量仍在表里 → 用户"已经删了这篇 PDF"，但检索还能把它喂给 LLM；
- `failed` 状态的分块（向量化没能通过）也会参与排序占名额。

Java 的 `searchSimilarWithThreshold` 里就带了这两个条件，
Python 版 `rag_service.search` 之前漏了 —— 已在 `_CHUNK_SQL` 里补齐。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entities import Knowledge
from app.schemas.knowledge_qa import KnowledgeSearchIn, RagSearchResultOut
from app.services.embedding_client import embed_query, to_pgvector
from app.services.knowledge_service import KnowledgeService, normalize_query_params

logger = logging.getLogger(__name__)

# 默认值（Java RagParamResolver.DEFAULT_TOP_K / DEFAULT_THRESHOLD）
DEFAULT_TOP_K = 5
DEFAULT_THRESHOLD = 0.5

# HNSW 召回深度，见 rag_service 的注释（SET LOCAL 只作用于当前事务）
HNSW_EF_SEARCH = 100

RESULT_TYPE_CHUNK = "chunk"
RESULT_TYPE_QA = "qa_pair"
QA_DOC_NAME = "问答对"  # 前端靠这三个字区分，别改

_CHUNK_SQL = """
    SELECT c.content       AS content,
           d.name          AS document_name,
           d.id            AS document_id,
           1 - (e.vector <=> CAST(:vec AS vector)) AS score
    FROM embedding e
    JOIN chunk c ON c.id = e.chunk_id
    JOIN document d ON d.id = c.document_id
    WHERE e.knowledge_id = :kb_id
      AND d.deleted = 0
      AND c.status = 'vectorized'
      AND (1 - (e.vector <=> CAST(:vec AS vector))) >= :threshold
    ORDER BY e.vector <=> CAST(:vec AS vector)
    LIMIT :top_k
"""


def _round4(value: Any) -> float | None:
    return None if value is None else round(float(value), 4)


def _parse_json(raw: Any) -> dict[str, Any]:
    """`knowledge.query_params` / `config` 都可能是 jsonb 对象或字符串。"""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            logger.warning("[检索] 配置 JSON 解析失败，按空处理: %s", raw[:100])
    return {}


class KnowledgeSearchService:
    def __init__(self, db: AsyncSession, user_id: int) -> None:
        self.db = db
        self.user_id = user_id
        self._kb = KnowledgeService(db, user_id)

    def _resolve_params(self, entity: Knowledge, overrides: dict[str, Any] | None) -> dict[str, Any]:
        """overrides > query_params > 默认值。"""
        ov = overrides or {}
        qp = _parse_json(entity.query_params)
        cfg = _parse_json(entity.config)

        base = normalize_query_params(entity.type or "pg", qp)
        top_k = int(ov.get("top_k") or ov.get("final_top_k") or base.get("final_top_k", DEFAULT_TOP_K))
        threshold = float(
            ov.get("threshold")
            if ov.get("threshold") is not None
            else ov.get("similarity_threshold")
            if ov.get("similarity_threshold") is not None
            else base.get("similarity_threshold", DEFAULT_THRESHOLD)
        )
        qa_enabled = bool(ov.get("qa_enabled", base.get("qa_enabled", True)))
        qa_top_k = int(ov.get("qa_top_k") or base.get("qa_top_k", 3))
        qa_threshold = float(ov.get("qa_threshold") or base.get("qa_threshold", 0.85))
        qa_priority = bool(ov.get("qa_priority", base.get("qa_priority", True)))

        return {
            "top_k": max(1, min(top_k, 100)),
            "threshold": max(0.0, min(threshold, 1.0)),
            "qa_enabled": qa_enabled,
            "qa_top_k": max(1, min(qa_top_k, 20)),
            "qa_threshold": max(0.0, min(qa_threshold, 1.0)),
            "qa_priority": qa_priority,
            # knowledgeId 之外的上下文：给诊断用
            "_cfg_keys": sorted(cfg.keys()),
        }

    async def search(self, knowledge_id: int, dto: KnowledgeSearchIn) -> list[RagSearchResultOut]:
        entity = (
            await self.db.execute(
                select(Knowledge).where(
                    Knowledge.id == knowledge_id, Knowledge.deleted == 0
                )
            )
        ).scalar_one_or_none()
        if entity is None:
            raise HTTPException(status_code=404, detail="知识库不存在")
        await self._kb.ensure_member(knowledge_id)

        params = self._resolve_params(entity, dto.overrides)
        vector = to_pgvector(await embed_query(dto.question))

        # ---- 第一路：chunk ----
        await self.db.execute(text(f"SET LOCAL hnsw.ef_search = {int(HNSW_EF_SEARCH)}"))
        chunk_rows = (
            (
                await self.db.execute(
                    text(_CHUNK_SQL),
                    {
                        "vec": vector,
                        "kb_id": knowledge_id,
                        "threshold": params["threshold"],
                        "top_k": params["top_k"],
                    },
                )
            )
            .mappings()
            .all()
        )
        chunk_vos = [
            RagSearchResultOut(
                content=r["content"] or "",
                rank=0,  # 稍后统一编号
                score=_round4(r["score"]),
                document_name=r["document_name"],
                document_id=str(r["document_id"]) if r["document_id"] is not None else None,
                result_type=RESULT_TYPE_CHUNK,
                original_score=_round4(r["score"]),
            )
            for r in chunk_rows
        ]

        # ---- 第二路：qa_pair（Dify 是外部检索，没有本地问答对）----
        qa_vos: list[RagSearchResultOut] = []
        if params["qa_enabled"] and (entity.type or "").lower() != "dify":
            from app.services.qa_pair_service import QaPairService

            qa_rows = await QaPairService(self.db, self.user_id).search_similar(
                knowledge_id,
                [float(x) for x in _split_vector(vector)],
                top_k=params["qa_top_k"],
                threshold=params["qa_threshold"],
            )
            for r in qa_rows:
                qa_vos.append(
                    RagSearchResultOut(
                        # 格式是硬契约：前端按 "【问答对】Q: ... A: ..." 展示
                        content=f"【问答对】Q: {r['question']}\nA: {r['answer']}",
                        rank=0,
                        score=_round4(r["score"]),
                        document_name=QA_DOC_NAME,
                        document_id=None,
                        result_type=RESULT_TYPE_QA,
                        original_score=_round4(r["score"]),
                    )
                )

        ordered = (qa_vos + chunk_vos) if params["qa_priority"] else (chunk_vos + qa_vos)
        for idx, vo in enumerate(ordered, start=1):
            vo.rank = idx

        logger.info(
            "[检索] kb=%s chunk=%s qa=%s topK=%s threshold=%s",
            knowledge_id,
            len(chunk_vos),
            len(qa_vos),
            params["top_k"],
            params["threshold"],
        )
        return ordered


def _split_vector(vector_str: str) -> list[str]:
    """`'[0.1,0.2]' → ['0.1','0.2']`。

    需要它而不是直接用 JSON.loads：`to_pgvector` 输出的字符串末尾没有逗号，
    是合法 JSON —— 但这里只是把已经算好的字符串回填成 float 列表给 QA 检索复用，
    避免为了一次查询重新 embed 一遍（省一次 API 调用 + 一次 100ms 延迟）。
    """
    return [x for x in vector_str.strip("[]").split(",") if x]
