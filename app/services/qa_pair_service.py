"""问答对服务。对照 Java `QaPairServiceImpl`。

## 状态机（`pending → vectorizing → active | failed`）

`status` 决定前端显示哪个图标，也决定能不能重新向量化：

| status | 含义 | 允许手动/再次向量化 |
|---|---|---|
| `pending` | 待向量化 | ✅ |
| `vectorizing` | 向量化中 | ❌（Java 直接 return，避免并发写同一条向量） |
| `active` | 已生效 | ❌ |
| `failed` | 失败了 | ✅ |

**只有改了 question 才需要重新向量化**：`answer` 不参与 embedding
（embedding 的输入是 question），所以改答案只改库、不碰向量也不改状态。

## 一个和 documents 不同的删除语义

QaPair 的删除走 MyBatis-Plus 的 `removeById` → **逻辑删**（`deleted=1`）；
但关联的 `embedding` 行是**物理删**。两边必须配套做，否则会留下孤儿向量：
向量表里 `qa_pair_id` 指向一条已软删的记录，检索时 JOIN 不到但白白占着 HNSW 索引，
还会在"向量数/64 条 vs 问答对活跃数"这种统计里对不上账。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.snowflake import new_id
from app.models.entities import Knowledge, QaPair
from app.schemas.knowledge_qa import QaPairIn, QaPairOut, QaPairUpdateIn
from app.services.knowledge_service import KnowledgeService

logger = logging.getLogger(__name__)

# 状态：库里存 code，出参转 desc（详见 schemas/knowledge_qa.py 的 docstring）
STATUS_DESC = {
    "pending": "待向量化",
    "vectorizing": "向量化中",
    "active": "生效",
    "failed": "失败",
}
SOURCE_CODE = {"manual", "import", "ai"}

# 单次 AI 生成上限（Java `Math.min(count, 20)`）
MAX_AI_COUNT = 20
DEFAULT_AI_COUNT = 10
# 喂给模型的文档正文上限（Java 是 5000 字符）
CONTENT_LIMIT = 5000

# 允许手动重试的状态（见模块 docstring）
RETRYABLE_STATUS = {"pending", "failed"}

# AI 生成系统提示词（逐字对齐 Java `QaPairGenerateExecutor.SYSTEM_PROMPT`）
QA_GEN_SYSTEM_PROMPT = """你是一个知识库问答对提取专家。根据文档内容，提取用户可能会问的具体问题及其标准答案。
要求：
1. 问题要具体明确，10-30个字
2. 答案要准确完整，基于文档内容
3. 返回JSON数组格式，不要有其他内容
4. 每个元素包含 question 和 answer 字段
示例：
[{"question": "如何重置密码？", "answer": "1. 点击「忘记密码」\n2. 输入邮箱\n3. 设置新密码"}]
"""


def _now() -> datetime:
    return datetime.now()


def _status_desc(code: str | None) -> str:
    return STATUS_DESC.get(code or "", "待向量化")


def _parse_qa_pairs(reply: str) -> list[QaPairIn]:
    """从 LLM 回复里抠 JSON 数组。对齐 Java `parseQaPairs`。

    三重防御（照抄 Java，每一条都是踩过的）：
    1. 先剥 `<think>...<think>` —— MiniMax / DeepSeek-R1 这类推理模型会在
       JSON 前输出思考过程，不剥会直接 JSONDecodeError
    2. 再剥 markdown 代码块围栏
    3. 剥完之后还以 `<` 开头 → **这是 HTML 错误页**（网关 502 / 鉴权失败 / 模型名错），
       不是"模型忘了输出 JSON"，必须给出指向真正原因的提示，
       否则用户会在 prompt 上反复调试一整天
    """
    import json as _json
    import re

    if not reply or not reply.strip():
        return []

    raw = reply.strip()
    raw = re.sub(r"(?is)<think>.*?</think>", "", raw)
    raw = re.sub(r"(?is)<thinking>.*?</thinking>", "", raw)
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        raw = raw.strip()
    if raw.startswith("<"):
        logger.error("[QA] AI 返回非 JSON（疑似网关/鉴权错误页）: %s", raw[:300])
        raise RuntimeError(
            "AI 返回非 JSON 内容（疑似网关/鉴权错误页），请检查模型供应商配置"
        )

    try:
        nodes = _json.loads(raw)
    except _json.JSONDecodeError as exc:
        logger.error("[QA] AI 返回的 JSON 无法解析: %s", raw[:300])
        raise RuntimeError(f"AI 返回内容不是合法 JSON：{exc}") from exc

    if isinstance(nodes, dict):
        # 少数模型会给 {"qa_pairs": [...]} 这种包一层的形式
        nodes = next((v for v in nodes.values() if isinstance(v, list)), [])

    out: list[QaPairIn] = []
    for node in nodes or []:
        if not isinstance(node, dict):
            continue
        question = str(node.get("question") or "").strip()
        answer = str(node.get("answer") or "").strip()
        if question and answer:
            out.append(QaPairIn(question=question[:2000], answer=answer[:2000]))
    return out


def _task_out(task: Any) -> dict[str, Any]:
    """task 行出参。**id 必须是字符串**（19 位雪花超 JS 安全整数）。"""
    return {
        "id": str(task.id),
        "name": task.name,
        "type": task.type,
        "status": task.status,
        "progress": task.progress or 0,
        "message": task.message,
        "refId": str(task.ref_id) if task.ref_id else None,
        "createTime": task.create_time.isoformat() if task.create_time else None,
    }


def _estimate_token_count(text_value: str) -> int:
    """估算 token 数，对齐 Java `estimateTokenCount`（中文 1.5 字/token，其它 4 字符/token）。

    这个估算只用于**展示**（`tokenCount` 字段），不参与任何限流/计费决策，
    所以保留 Java 那套粗糙算法是可以的 —— 换算法反而会让老数据看起来"变了"。
    """
    if not text_value:
        return 0
    chinese = sum(1 for c in text_value if "\u4e00" <= c <= "\u9fa5")
    other = len(text_value) - chinese
    return int(chinese / 1.5 + other / 4.0)


def _to_out(entity: QaPair) -> QaPairOut:
    return QaPairOut(
        id=str(entity.id),
        knowledge_id=str(entity.knowledge_id),
        question=entity.question,
        answer=entity.answer,
        source=entity.source,  # code
        status=_status_desc(entity.status),  # desc ← 前端按中文渲染
        token_count=entity.token_count,
        create_time=entity.create_time,
        update_time=entity.update_time,
    )


class QaPairService:
    def __init__(self, db: AsyncSession, user_id: int) -> None:
        self.db = db
        self.user_id = user_id
        self._kb = KnowledgeService(db, user_id)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    async def _get(self, qa_id: int) -> QaPair:
        entity = (
            await self.db.execute(
                select(QaPair).where(QaPair.id == qa_id, QaPair.deleted == 0)
            )
        ).scalar_one_or_none()
        if entity is None:
            raise HTTPException(status_code=404, detail="问答对不存在")
        return entity

    async def _delete_vectors(self, qa_id: int) -> None:
        """删 QA 对对应的向量。

        QA 对的向量在 embedding 表里是 `qa_pair_id` 那一列（和 chunk 互斥）。
        必须物理删：SQL 里 `WHERE qa_pair_id = :id`，不是软删标记。
        """
        await self.db.execute(
            text("DELETE FROM embedding WHERE qa_pair_id = :id"), {"id": qa_id}
        )

    async def _vectorize_one(self, entity: QaPair) -> bool:
        """同步向量化一条。成功 True，失败 False（状态已写 failed）。

        Java 用 `@Async` 丢线程池 + 新事务；这里同步做完直接返回结果。
        理由同 `_embed_texts_resilient` 那一节：单条问答对就是一次 HTTP 调用，
        几百毫秒的事，没必要为了"异步"再引入事务边界和失败不可见的问题。
        """
        entity.status = "vectorizing"
        entity.update_time = _now()
        await self.db.commit()

        from app.services.embedding_client import embed, to_pgvector
        from app.core.config import settings

        try:
            vectors = await embed([entity.question])
            vector = vectors[0]
        except Exception as exc:  # noqa: BLE001
            logger.error("[QA向量化] 失败 id=%s err=%s", entity.id, exc)
            entity.status = "failed"
            entity.update_time = _now()
            await self.db.commit()
            return False

        model_name = settings.embedding_model
        kb = (
            await self.db.execute(
                select(Knowledge.embedding_model).where(
                    Knowledge.id == entity.knowledge_id
                )
            )
        ).scalar_one_or_none()
        if kb:
            model_name = kb

        await self._delete_vectors(entity.id)
        await self.db.execute(
            text(
                """
                INSERT INTO embedding
                    (id, chunk_id, qa_pair_id, model_name, dimension, vector, knowledge_id)
                VALUES
                    (:id, NULL, :qa_id, :model, :dim, CAST(:vec AS vector), :kb_id)
                """
            ),
            {
                "id": new_id(),
                "qa_id": entity.id,
                "model": model_name,
                "dim": len(vector),
                "vec": to_pgvector(vector),
                "kb_id": entity.knowledge_id,
            },
        )
        entity.status = "active"
        entity.update_time = _now()
        await self.db.commit()
        return True

    # ------------------------------------------------------------------
    # 对外
    # ------------------------------------------------------------------

    async def create(self, knowledge_id: int, dto: QaPairIn) -> QaPairOut:
        await self._kb.ensure_role(knowledge_id, "developer")

        entity = QaPair(
            id=new_id(),
            knowledge_id=knowledge_id,
            question=dto.question,
            answer=dto.answer,
            source="manual",
            status="pending",
            token_count=_estimate_token_count(dto.question),
            create_time=_now(),
            update_time=_now(),
        )
        self.db.add(entity)
        await self.db.commit()
        await self.db.refresh(entity)

        await self._vectorize_one(entity)
        logger.info("[QA] 创建 id=%s kb=%s", entity.id, knowledge_id)
        return _to_out(entity)

    async def update(self, qa_id: int, dto: QaPairUpdateIn) -> QaPairOut:
        entity = await self._get(qa_id)
        await self._kb.ensure_role(entity.knowledge_id, "developer")

        question_changed = False
        if dto.question is not None and dto.question != entity.question:
            entity.question = dto.question
            entity.token_count = _estimate_token_count(dto.question)
            question_changed = True
        if dto.answer is not None:
            entity.answer = dto.answer

        if question_changed:
            await self._delete_vectors(entity.id)
            await self._vectorize_one(entity)
        else:
            entity.update_time = _now()
            await self.db.commit()

        await self.db.refresh(entity)
        return _to_out(entity)

    async def page(
        self, knowledge_id: int, page_num: int = 1, page_size: int = 20, keyword: str = ""
    ) -> tuple[list[QaPairOut], int]:
        await self._kb.ensure_member(knowledge_id)

        cond = [QaPair.knowledge_id == knowledge_id, QaPair.deleted == 0]
        if keyword:
            cond.append(QaPair.question.ilike(f"%{keyword}%"))

        rows = (
            (
                await self.db.execute(
                    select(QaPair)
                    .where(*cond)
                    .order_by(QaPair.create_time.desc())
                    .offset((page_num - 1) * page_size)
                    .limit(page_size)
                )
            )
            .scalars()
            .all()
        )
        total = (
            await self.db.execute(
                select(func.count()).select_from(QaPair).where(*cond)
            )
        ).scalar_one()
        return [_to_out(r) for r in rows], int(total)

    async def delete(self, qa_id: int) -> None:
        entity = await self._get(qa_id)
        await self._kb.ensure_role(entity.knowledge_id, "developer")
        await self._delete_vectors(entity.id)
        entity.deleted = 1
        entity.update_time = _now()
        await self.db.commit()
        logger.info("[QA] 删除 id=%s", qa_id)

    async def batch_import(
        self, knowledge_id: int, items: list[QaPairIn], source: str = "import"
    ) -> int:
        """批量导入。返回成功条数。

        `source`：`import`（批量导入接口）/ `ai`（AI 生成落库）。
        """
        await self._kb.ensure_role(knowledge_id, "developer")

        entities: list[QaPair] = []
        for dto in items:
            e = QaPair(
                id=new_id(),
                knowledge_id=knowledge_id,
                question=dto.question,
                answer=dto.answer,
                source=source if source in SOURCE_CODE else "import",
                status="pending",
                token_count=_estimate_token_count(dto.question),
                create_time=_now(),
                update_time=_now(),
            )
            entities.append(e)
        self.db.add_all(entities)
        await self.db.commit()

        ok_count = 0
        for e in entities:
            if await self._vectorize_one(e):
                ok_count += 1
        logger.info(
            "[QA] 批量导入 kb=%s 总数=%s 成功=%s source=%s",
            knowledge_id,
            len(entities),
            ok_count,
            source,
        )
        return len(entities)

    async def vectorize(self, qa_id: int) -> bool:
        """手动触发向量化。`vectorizing` / `active` 直接跳过（对齐 Java）。"""
        entity = await self._get(qa_id)
        await self._kb.ensure_role(entity.knowledge_id, "developer")
        if entity.status not in RETRYABLE_STATUS:
            logger.info("[QA] 跳过向量化 id=%s status=%s", qa_id, entity.status)
            return False
        await self._delete_vectors(entity.id)
        return await self._vectorize_one(entity)

    async def batch_vectorize(self, qa_ids: list[int]) -> int:
        """批量向量化，返回**触发成功**的条数。

        逐条 try：某一条权限不足/已 active 就跳过，不能让整批失败
        （Java `batchVectorize` 也是这个语义，返回值是 success 计数）。
        """
        count = 0
        for qa_id in qa_ids:
            try:
                if await self.vectorize(qa_id):
                    count += 1
            except HTTPException:
                logger.warning("[QA] 批量向量化跳过 id=%s（无权限或不存在）", qa_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[QA] 批量向量化失败 id=%s err=%s", qa_id, exc)
        return count

    async def ai_generate(
        self,
        knowledge_id: int,
        count: int | None = None,
        provider_id: int | None = None,
        model_id: str | None = None,
    ) -> dict[str, Any]:
        """AI 生成问答对。对照 Java `QaPairGenerateExecutor`。

        Java 是「建 task 行 → 后台线程池执行 → 前端轮询 task 状态」；
        Python 版**同步做完再返回**（和 `generate-questions` 同理）：
        一次 LLM 调用 + N 次 embedding，几十秒内够，
        换成异步就要再引入轮询和"任务丢了没人知道"的问题。

        仍然**写 task 行**：让这次执行有迹可循（成功/失败/生成条数），
        也方便以后真要接队列时沿用同一个表。

        ⚠️ 只用**第一个已完成文档**、截断到 5000 字（照抄 Java）：
        拿全部文档会让 prompt 超出上下文窗口，而"多发几个文档"带来的
        收益远不如"稳定不炸"。
        """
        await self._kb.ensure_role(knowledge_id, "developer")

        entity = (
            await self.db.execute(
                select(Knowledge).where(Knowledge.id == knowledge_id, Knowledge.deleted == 0)
            )
        ).scalar_one_or_none()
        if entity is None:
            raise HTTPException(status_code=404, detail="知识库不存在")

        actual_count = max(1, min(count or DEFAULT_AI_COUNT, MAX_AI_COUNT))

        from app.models.entities import Document, TaskRecord
        from app.services.example_question_service import _complete

        task = TaskRecord(
            id=new_id(),
            name=f"问答对生成 - {entity.name}",
            type="qa_pair_generate",
            status="running",
            progress=0,
            message="正在加载知识库文档",
            payload={
                "knowledgeId": str(knowledge_id),
                "count": actual_count,
                "providerId": str(provider_id) if provider_id else None,
                "modelId": model_id,
            },
            user_id=self.user_id,
            ref_id=knowledge_id,
            # ⚠️ `update_time` 是 **NOT NULL**：漏写会让 INSERT 直接 500，
            # 而且是在 LLM 调用之前就炸，用户看到的是莫名其妙的服务端错误。
            create_time=_now(),
            update_time=_now(),
            started_at=_now(),
        )
        self.db.add(task)
        await self.db.commit()

        try:
            doc = (
                await self.db.execute(
                    select(Document)
                    .where(
                        Document.knowledge_id == knowledge_id,
                        Document.deleted == 0,
                        Document.status == "completed",
                    )
                    .order_by(Document.create_time.asc())
                )
            ).scalars().first()
            if doc is None:
                raise RuntimeError("知识库中没有已完成的文档，无法生成问答对")

            content = await self._read_doc_content(doc)
            if not content.strip():
                raise RuntimeError(f"文档内容为空：{doc.name}")

            reply = await _complete(
                self.db,
                system=QA_GEN_SYSTEM_PROMPT,
                user=(
                    f"文档名称：{doc.name}\n\n"
                    f"请从以下文档内容中提取 {actual_count} 个问答对。\n\n"
                    f"文档内容：\n{content[:CONTENT_LIMIT]}"
                ),
                provider_id=provider_id,
                model_id=model_id,
                max_tokens=4096,
            )
            items = _parse_qa_pairs(reply)
            if not items:
                raise RuntimeError("AI 未能生成有效的问答对")
            items = items[:actual_count]

            saved = await self.batch_import(knowledge_id, items, source="ai")

            # ⚠️ 终态用 `completed` 而不是 Java 的 `success`：
            # Python 侧已建立的约定是 `completed`（见
            # `eval_experiment_service.STATUS_COMPLETED`），同一张 `task` 表里
            # Java 历史行写的是 `success`。目前前端不读这个字段（AI 生成后
            # 只刷新列表），所以不会炸；将来要做统一的"任务中心"列表时必须
            # 一次性把两边拉齐，否则同表两种状态会让过滤器永远漏一半。
            task.status = "completed"
            task.progress = 100
            task.message = f"生成完成，共生成 {saved} 条问答对"
            task.result = str(saved)
            task.completed_at = _now()
            task.update_time = _now()
            await self.db.commit()
            logger.info("[QA] AI生成 kb=%s 生成=%s", knowledge_id, saved)
            return _task_out(task)
        except Exception as exc:  # noqa: BLE001
            logger.error("[QA] AI生成失败 kb=%s err=%s", knowledge_id, exc)
            # ⚠️ 必须先 rollback 再写失败状态：
            # 上面任何一步 SQL 报错都会让当前事务进入 aborted 状态，
            # 之后所有语句一律 "current transaction is aborted" ——
            # 不 rollback 的话，这个"记录失败原因"的 UPDATE 自己也会炸，
            # 用户拿到的是 500 而不是我们整理好的 400 + 原因。
            await self.db.rollback()
            task.status = "failed"
            task.progress = 100
            task.message = str(exc)[:500]
            task.error = str(exc)[:2000]
            task.completed_at = _now()
            task.update_time = _now()
            await self.db.commit()
            raise HTTPException(status_code=400, detail=str(exc)[:500]) from exc

    async def _read_doc_content(self, doc: Any, limit: int = CONTENT_LIMIT) -> str:
        """取文档正文。优先拼 chunk（在库里，不用走 MinIO），退化到 markdown。"""
        from sqlalchemy import text as sql_text

        rows = (
            await self.db.execute(
                sql_text(
                    "SELECT content FROM chunk WHERE document_id = :d "
                    "AND status = 'vectorized' ORDER BY chunk_index LIMIT 60"
                ),
                {"d": doc.id},
            )
        ).scalars().all()
        text_value = "\n".join(rows)
        if text_value.strip():
            return text_value[:limit]

        if doc.markdown_path:
            from app.services import minio_client

            try:
                cached = await minio_client.download_text(doc.markdown_path)
                return (cached or "")[:limit]
            except Exception as exc:  # noqa: BLE001
                logger.warning("[QA] 拉 markdown 失败 docId=%s err=%s", doc.id, exc)
        return ""

    async def search_similar(
        self,
        knowledge_id: int,
        query_vector: list[float],
        top_k: int = 3,
        threshold: float = 0.85,
    ) -> list[dict[str, Any]]:
        """QA 对向量检索（`embedding.qa_pair_id` 那一岔）。

        和 chunk 检索的区别：这里 **join qa_pair 且要求 deleted=0**，
        否则软删的问答对还会被召回给 LLM 当上下文。
        """
        from app.services.embedding_client import to_pgvector

        rows = (
            await self.db.execute(
                text(
                    """
                    SELECT q.id          AS id,
                           q.question    AS question,
                           q.answer      AS answer,
                           1 - (e.vector <=> CAST(:vec AS vector)) AS score
                    FROM embedding e
                    JOIN qa_pair q ON q.id = e.qa_pair_id AND q.deleted = 0
                    WHERE e.knowledge_id = :kb_id
                      AND (1 - (e.vector <=> CAST(:vec AS vector))) >= :threshold
                    ORDER BY e.vector <=> CAST(:vec AS vector)
                    LIMIT :top_k
                    """
                ),
                {
                    "vec": to_pgvector(query_vector),
                    "kb_id": knowledge_id,
                    "threshold": threshold,
                    "top_k": top_k,
                },
            )
        ).mappings().all()
        return [dict(r) for r in rows]
