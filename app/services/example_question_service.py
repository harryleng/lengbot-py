"""示例问题 + 思维导图：两个都要真调 LLM 的知识库附属能力。

对照 Java：
- `KnowledgeServiceImpl.generateExampleQuestions` / `generateOneExampleQuestion`
- `MindmapUtil.generateFromFiles`

## 为什么合并到一个文件

三者共用同一套「取 provider → 拼 prompt → 调一次 → 抠 JSON」的范式，
Java 侧散在 `KnowledgeServiceImpl`（1057 行）和 `MindmapUtil` 两个类里，
复制粘贴的痕迹很明显。Python 版抽一个 `_complete()` 复用。

## prompt 必须逐字照抄的原因

这些 prompt 里的约束是**踩坑踩出来的**，改一个字效果就会漂：
- 示例问题：「10-100 个字，问题长度严格不超过 100 字」——
  不写死，模型会生成"请详细介绍一下这个文档"这种没检索价值的空问题
- 思维导图：「每个文件名只能出现一次，绝不重复」——
  不强调，模型会把同一个文件同时放进两个分类，
  前端渲染时会出现两棵看起来一样的子树

## 兜底策略

Java 的 `generateExampleQuestions`（批量版）失败只打日志，因为它跑在
入库任务里，不能因为生成问题失败就让整个文档入库失败。
Python 版保持同样分层：
- 单条生成（用户手动点）→ 失败返回空串，让前端提示重试
- 批量生成（入库后自动跑）→ 失败吞掉，只记日志
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entities import Chunk, Document, Knowledge
from app.services.llm_client import LlmConfig, stream_chat

logger = logging.getLogger(__name__)

# 示例问题系统提示词（逐字对齐 Java `QUESTION_GEN_SYSTEM_PROMPT`）
QUESTION_GEN_SYSTEM_PROMPT = """你是知识库测试专家。根据文档内容生成1个用户可能会问的具体问题。
要求：10-100个字，具体明确，能通过知识库检索找到答案。问题长度严格不超过100字。
只返回JSON格式，不要有其他内容：{"question": "..."}"""

# 思维导图系统提示词（逐字对齐 Java `MINDMAP_SYSTEM_PROMPT`）
MINDMAP_SYSTEM_PROMPT = """你是一个思维导图生成助手。根据提供的文件列表生成JSON格式的思维导图结构。
严格规则：
- 只输出JSON，不要任何其他文字、解释或markdown标记
- 每个文件名在思维导图中只能出现一次，绝不重复
- 层级为2-4层，叶子节点的content必须是文件名
- 根节点content必须是知识库名称
- 根据文件名和类型进行逻辑分组，选择最主要的一个分类维度
- 使用emoji图标增强可读性
- children数组中每个子节点必须有content字段，可选children字段
- 叶子节点的children必须是空数组[]
"""

MINDMAP_USER_TEMPLATE = """请为知识库"{name}"生成思维导图结构。

文件列表（共{count}个文件）：
{file_list}

重要提醒：
1. 每个文件名只能在思维导图中出现一次
2. 不要让同一个文件出现在多个分类下
3. 为每个文件选择最合适的唯一分类

输出格式（只输出JSON）：
{{"content": "知识库名称", "children": [...]}}"""

# 每个文档取前 3000 字进 prompt（Java 同）
CONTENT_PREVIEW_CHARS = 3000
# 思维导图最多喂 20 个文件（Java `MAX_FILES`），否则 prompt 太长且模型会开始编
MAX_MINDMAP_FILES = 20


def _strip_fence(text: str) -> str:
    """去掉 ``` / ```json 围栏。模型几乎一定会加。"""
    out = text.strip()
    if out.startswith("```"):
        first_nl = out.find("\n")
        last = out.rfind("```")
        if first_nl > 0 and last > first_nl:
            out = out[first_nl + 1 : last]
    return out.strip()


def _extract_json(text: str) -> str:
    """从自由文本里抠出第一个完整 JSON 对象/数组。"""
    cleaned = _strip_fence(text)
    for opener, closer in (("{", "}"), ("[", "]")):
        start = cleaned.find(opener)
        end = cleaned.rfind(closer)
        if start >= 0 and end > start:
            return cleaned[start : end + 1]
    return cleaned


async def _complete(
    db: AsyncSession,
    *,
    system: str | None,
    user: str,
    provider_id: int | None = None,
    model_id: str | None = None,
    max_tokens: int = 1024,
) -> str:
    """调一次 LLM 拿完整文本。

    provider 解析复用 `workflow.nodes._resolve_provider` —— 那里已有
    进程内缓存和多供应商 base_url 归一化（`/v1` 之外还有 `/plan/v3`）。
    重新写一份必然又踩一次"腾讯云被拼成 404"的坑。
    """
    from app.workflow.nodes import _resolve_provider

    resolved_pid = provider_id or await _default_provider_id(db)
    if resolved_pid is None:
        raise RuntimeError("没有可用的模型供应商，请先在「模型供应商」里配置一个")

    resolved = await _resolve_provider(int(resolved_pid))
    if not resolved:
        raise RuntimeError(f"模型供应商 {resolved_pid} 不存在或未配置 API Key")
    base_url, api_key = resolved

    model = model_id or await _default_model_id(db, int(resolved_pid))
    if not model:
        raise RuntimeError(f"模型供应商 {resolved_pid} 下没有可用模型")

    messages: list[dict[str, Any]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": user})

    cfg = LlmConfig(base_url=base_url, api_key=api_key, model=model, max_tokens=max_tokens)
    parts: list[str] = []
    async for ev in stream_chat(cfg, messages):
        if ev.kind == "text":
            parts.append(ev.text)
    return "".join(parts)


async def _default_provider_id(db: AsyncSession) -> int | None:
    """取第一个有 api_key 的 active provider。

    刻意**不挑"有 LLM 模型"的** —— 那要再 join 一次 model 表。
    反正 `_default_model_id` 拿不到模型会明确报错，比静默选错 provider 好查。
    """
    from sqlalchemy import text as sql_text

    row = (
        await db.execute(
            sql_text(
                """
                SELECT id FROM model_provider
                WHERE api_key IS NOT NULL AND api_key <> ''
                ORDER BY id
                LIMIT 1
                """
            )
        )
    ).first()
    return int(row[0]) if row else None


async def _default_model_id(db: AsyncSession, provider_id: int) -> str | None:
    """取该 provider 下第一个可用的 LLM 模型 id。

    ⚠️ **列名是 `type`，不是 `model_type`**。
    `model` 表的列是 `id / provider_id / model_id / name / type / status / ...`，
    这一行 SQL 原本写成 `COALESCE(model_type, 'llm')`，而 PostgreSQL 里
    `model_type` 根本不存在 —— 报的是 `column "model_type" does not exist`（42883），
    **不是"没有模型"**。后果是所有走这条路的 LLM 功能（示例问题生成、
    思维导图、问答对 AI 生成）全部在真正调用前就 500，
    而且错误信息完全指向不到"列名写错"这个原因。

    另外必须带 `deleted = 0`：软删的模型还留在表里，取到它会在调用时才 404。
    """
    from sqlalchemy import text as sql_text

    row = (
        await db.execute(
            sql_text(
                """
                SELECT model_id FROM model
                WHERE provider_id = :pid AND deleted = 0 AND type = 'llm'
                ORDER BY id LIMIT 1
                """
            ),
            {"pid": provider_id},
        )
    ).first()
    if row:
        return str(row[0])

    # 兜底：type 标成别的（比如历史数据里没标）也试一下，总比直接报错强
    row2 = (
        await db.execute(
            sql_text(
                """
                SELECT model_id FROM model
                WHERE provider_id = :pid AND deleted = 0
                ORDER BY id LIMIT 1
                """
            ),
            {"pid": provider_id},
        )
    ).first()
    return str(row2[0]) if row2 else None


# ----------------------------------------------------------------------
# 示例问题
# ----------------------------------------------------------------------


async def _read_doc_content(db: AsyncSession, doc: Document, limit: int = CONTENT_PREVIEW_CHARS) -> str:
    """取文档正文用于喂模型。

    优先拼 chunk（chunk 就在库里，比去 MinIO 拉 markdown 快且不依赖对象存储）；
    chunk 为空时退到 `document.metadata.summary`，再退到空串。
    """
    rows = (
        (
            await db.execute(
                select(Chunk.content)
                .where(Chunk.document_id == doc.id)
                .order_by(Chunk.chunk_index)
                .limit(20)
            )
        )
        .scalars()
        .all()
    )
    if rows:
        return "\n".join(rows)[:limit]

    meta = doc.metadata_ or {}
    summary = meta.get("summary") if isinstance(meta, dict) else None
    return str(summary)[:limit] if summary else ""


def _parse_question(reply: str) -> str | None:
    """从模型输出抠 `{"question": "..."}`。失败返回 None（不抛）。"""
    try:
        node = json.loads(_extract_json(reply))
    except json.JSONDecodeError:
        return None
    if isinstance(node, dict):
        q = node.get("question")
        if isinstance(q, str) and q.strip():
            return q.strip()
    return None


async def generate_one(
    db: AsyncSession, knowledge_id: int, provider_id: int | None = None
) -> str:
    """为第一个已完成文档生成一条示例问题。失败返回空串。"""
    kb = (
        await db.execute(select(Knowledge).where(Knowledge.id == knowledge_id))
    ).scalar_one_or_none()
    if kb is None:
        return ""

    doc = (
        await db.execute(
            select(Document)
            .where(Document.knowledge_id == knowledge_id, Document.deleted == 0)
            .where(Document.status == "completed")
            .order_by(Document.create_time)
            .limit(1)
        )
    ).scalar_one_or_none()
    if doc is None:
        logger.warning("[示例问题] 知识库没有已完成文档 kid=%s", knowledge_id)
        return ""

    content = await _read_doc_content(db, doc)
    if not content.strip():
        logger.warning("[示例问题] 文档内容为空 docId=%s", doc.id)
        return ""

    prompt = f"文档名称：{doc.name}\n\n文档内容（前{CONTENT_PREVIEW_CHARS}字）：\n{content}"
    try:
        reply = await _complete(
            db, system=QUESTION_GEN_SYSTEM_PROMPT, user=prompt, provider_id=provider_id, max_tokens=256
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("[示例问题] AI 生成失败 kid=%s err=%s", knowledge_id, exc)
        return ""

    question = _parse_question(reply)
    if not question:
        logger.warning("[示例问题] 返回格式异常，原文=%s", reply[:200])
        return ""
    return question


async def generate_for_documents(
    db: AsyncSession, knowledge_id: int, provider_id: int | None = None
) -> int:
    """为知识库所有已完成文档各生成一条，追加进 exampleQuestions。

    返回实际新增条数。**失败不抛** —— 这个函数在入库链路里被调用，
    生成问题失败不该让文档入库失败（Java 同款分层）。
    """
    from app.core.config import settings
    from app.services.knowledge_service import KnowledgeService

    docs = (
        (
            await db.execute(
                select(Document)
                .where(Document.knowledge_id == knowledge_id, Document.deleted == 0)
                .where(Document.status == "completed")
                .order_by(Document.create_time)
            )
        )
        .scalars()
        .all()
    )
    if not docs:
        return 0

    svc = KnowledgeService(db, settings.dev_user_id)
    added = 0
    for doc in docs:
        content = await _read_doc_content(db, doc)
        if not content.strip():
            continue
        prompt = f"文档名称：{doc.name}\n\n文档内容（前{CONTENT_PREVIEW_CHARS}字）：\n{content}"
        try:
            reply = await _complete(
                db, system=QUESTION_GEN_SYSTEM_PROMPT, user=prompt, provider_id=provider_id, max_tokens=256
            )
            q = _parse_question(reply)
            if q and await svc.append_example_question(knowledge_id, q):
                added += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("[示例问题] 生成失败 docId=%s err=%s", doc.id, exc)
    logger.info("[示例问题] 批量生成完成 kid=%s 新增=%s", knowledge_id, added)
    return added


# ----------------------------------------------------------------------
# 思维导图
# ----------------------------------------------------------------------


async def generate_mindmap(
    db: AsyncSession, knowledge_id: int, provider_id: int | None = None
) -> Any:
    """按文件列表生成思维导图树，写回 `knowledge.mindmap_data` 并返回。"""
    kb = (
        await db.execute(select(Knowledge).where(Knowledge.id == knowledge_id))
    ).scalar_one_or_none()
    if kb is None:
        raise ValueError("知识库不存在")

    docs = (
        (
            await db.execute(
                select(Document)
                .where(Document.knowledge_id == knowledge_id, Document.deleted == 0)
                .where(Document.status == "completed")
                .order_by(Document.create_time)
                .limit(MAX_MINDMAP_FILES)
            )
        )
        .scalars()
        .all()
    )
    # 空库不让调模型 —— 空文件列表生成的导图只有一个根节点，纯浪费 token
    if not docs:
        from fastapi import HTTPException

        raise HTTPException(status_code=400, detail="知识库没有已完成的文档，无法生成思维导图")

    file_list = "\n".join(f"- {d.name} ({d.file_type})" for d in docs)
    user_prompt = MINDMAP_USER_TEMPLATE.format(
        name=kb.name, count=len(docs), file_list=file_list
    )

    reply = await _complete(
        db, system=MINDMAP_SYSTEM_PROMPT, user=user_prompt, provider_id=provider_id, max_tokens=2048
    )

    try:
        tree = json.loads(_extract_json(reply))
    except json.JSONDecodeError as exc:
        from fastapi import HTTPException

        logger.error("[思维导图] JSON 解析失败，原文=%s", reply[:300])
        raise HTTPException(status_code=500, detail="AI 返回的内容不是合法 JSON，请重试") from exc

    kb.mindmap_data = tree
    from datetime import datetime

    kb.update_time = datetime.now()
    await db.commit()
    logger.info("[思维导图] 生成完成 kid=%s docs=%s", knowledge_id, len(docs))
    return tree
