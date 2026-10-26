"""RAG 引用接口（`GET /api/chat/rag-references`）。

**什么时候会被调**：前端在消息气泡上点「引用」按钮时，用当前问题**重新检索一次**
知识库，把命中的片段列出来。不是从消息里存的 `ragReferences` 读 ——
那个只存了当时那一轮的，用户可能想拿同一句话再查别的库。

对照 Java `ChatServiceImpl.getRagReferences`。

**Java 支持两种 `sourceType`**：`chunk`（文档块）和 `qa_pair`（问答对）。
Python 版只做 `chunk` —— 库里没有 qa_pair 数据（`qa_pair` 表存在但未启用向量化），
所以 `qaPairId` 恒为 null。这不是漏做，是数据源本来就没有。

**失败一律返回空数组**，不抛错：
- Agent 没有绑知识库 → 空
- 知识库没向量化 → 空（前端显示"暂无引用"，比弹一个红色错误框合理）
- 向量模型调不通 → 空
用户点「引用」是想看有没有额外内容，不是想处理异常。
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entities import Agent
from app.schemas.feedback import RagReferenceOut
from app.services import rag_service

logger = logging.getLogger(__name__)

# 引用预览的截断长度（对齐 Java mapToRagReference 的 200）
PREVIEW_LIMIT = 200
# 引用不需要那么多，5 条足够让用户判断"有没有可用资料"
DEFAULT_TOP_K = 5


class RagReferenceService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def _agent_knowledge_ids(self, agent_id: int | None) -> list[int]:
        """Agent 绑定的知识库 id。

        `agent.config` 里 `knowledges` 是**字符串数组**（雪花 id 存成字符串防精度丢失），
        所以这里要 `int()` 转换。空数组或字段缺失都返回 []。
        """
        if not agent_id:
            return []
        cfg = (
            await self.db.execute(select(Agent.config).where(Agent.id == agent_id))
        ).scalar()
        if not isinstance(cfg, dict):
            return []
        raw = cfg.get("knowledges") or cfg.get("knowledgeIds") or []
        if not isinstance(raw, list):
            return []
        out: list[int] = []
        for item in raw:
            try:
                out.append(int(item))
            except (TypeError, ValueError):
                continue
        return out

    async def get_references(
        self, session_id: int, agent_id: int | None, question: str
    ) -> list[RagReferenceOut]:
        if not (question or "").strip():
            return []

        knowledge_ids = await self._agent_knowledge_ids(agent_id)
        if not knowledge_ids:
            # Agent 没绑库：这不是错误，就是没有引用可给
            return []

        try:
            rows = await rag_service.search(
                question, knowledge_ids, top_k=DEFAULT_TOP_K, min_score=None
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("RAG 引用检索失败 agent=%s: %s", agent_id, exc)
            return []

        return [self._to_reference(r) for r in rows]

    @staticmethod
    def _to_reference(row: dict[str, Any]) -> RagReferenceOut:
        content = str(row.get("content") or "")
        preview = content if len(content) <= PREVIEW_LIMIT else content[:PREVIEW_LIMIT] + "..."
        return RagReferenceOut(
            document_name=row.get("document_name"),
            content_preview=preview,
            score=float(row["score"]) if row.get("score") is not None else None,
            knowledge_id=str(row["knowledge_id"]) if row.get("knowledge_id") else None,
            document_id=str(row["document_id"]) if row.get("document_id") else None,
            chunk_id=str(row["chunk_id"]) if row.get("chunk_id") else None,
            # Python 版只做 chunk；qa_pair 数据源不存在
            source_type="chunk",
            qa_pair_id=None,
        )
