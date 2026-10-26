"""知识库工具（RAG 的对外接口）。

对齐数据库里 `tool_type='knowledge'` 的 6 个工具，这里先实现两个核心的：

    query_knowledge(question)   ← RAG 检索，最重要的一个
    list_knowledge_bases()      ← 让模型先知道有哪些库，避免瞎搜

另外四个（get_mindmap / open_kb_document / search_documents / find_in_document）
依赖文档树、思维导图等额外结构，后续再补。

**设计要点：检索范围来自 ContextVar，不是工具参数。**
如果把 knowledgeId 做成参数，模型会编造 ID。正确做法是后端从
`agent.config.knowledges` 取，通过 ContextVar 传进来（见 app/tools/context.py）。
"""

from __future__ import annotations

import json
from typing import Annotated

from sqlalchemy import bindparam, text

from app.core.db import SessionLocal
from app.services.rag_service import knowledge_chunk_counts, search
from app.tools.base import field, tool
from app.tools.context import current_context, current_knowledge_ids

# 单个 chunk 进工具结果的截断长度。
# 实测：800 字符 × 5 条 × 7 次检索 = 上下文爆炸（一轮烧了 11,561 token）。
# 400 字符足够模型判断相关性，细节可以靠 open_kb_document 再看原文。
MAX_CHUNK_CHARS = 400

# 每次检索返回几条。3 条已经能覆盖大部分问题，5 条大多是冗余。
DEFAULT_TOP_K = 3

# 同一轮对话最多检索几次。
# 模型会自动做 multi-query（实测自发拆了 7 个角度），召回是好了，
# 但每次结果都进上下文，成本按倍数涨。3 次是"够用且可控"的平衡点。
MAX_RETRIEVAL_ROUNDS = 3


def _clip(text_value: str, limit: int = MAX_CHUNK_CHARS) -> str:
    if len(text_value) <= limit:
        return text_value
    return text_value[:limit] + f"…（截断，原文 {len(text_value)} 字）"


@tool(
    "query_knowledge",
    "在知识库中检索与问题相关的内容。当用户询问知识库里的资料、文档内容时调用。返回匹配的原文片段和相似度。",
)
async def query_knowledge(
    question: Annotated[str, field("搜索问题，用自然语言描述你想找什么")],
) -> str:
    kb_ids = current_knowledge_ids()
    if not kb_ids:
        return json.dumps(
            {"ok": False, "error": "当前 Agent 没有绑定任何知识库，无法检索"}, ensure_ascii=False
        )

    ctx = current_context()

    # 次数闸门：模型很爱"换个角度再搜一次"，但没有节制的 multi-query 会让
    # 上下文按倍数膨胀。到上限后要返回**成功但无新内容**，不能返回 error ——
    # 实测返回 ok:false 时模型会理解成"工具出错了，再试一次"，白烧两轮 step。
    if ctx is not None and ctx.query_count >= MAX_RETRIEVAL_ROUNDS:
        return json.dumps(
            {
                "ok": True,
                "count": 0,
                "results": [],
                "note": f"已达本次对话的检索上限（{MAX_RETRIEVAL_ROUNDS} 次），没有更多新资料了。"
                f"请直接基于已经获得的内容回答；如果确实不足，如实告诉用户缺什么，不要编造。",
            },
            ensure_ascii=False,
        )
    if ctx is not None:
        ctx.query_count += 1

    # 先确认这些库到底有没有向量 —— 有些库只有 chunk 没有 embedding（从未向量化成功），
    # 直接搜会返回空，让人误以为"没找到相关内容"。诊断信息比空结果有用得多。
    counts = await knowledge_chunk_counts(kb_ids)
    if counts and all(v == 0 for v in counts.values()):
        return json.dumps(
            {
                "ok": False,
                "error": "绑定的知识库尚未完成向量化（有文本块但没有向量），无法做语义检索",
                "knowledge_ids": kb_ids,
                "detail": counts,
            },
            ensure_ascii=False,
        )

    results = await search(question, kb_ids, top_k=DEFAULT_TOP_K)
    if not results:
        return json.dumps(
            {
                "ok": True,
                "query": question,
                "count": 0,
                "results": [],
                "note": "没有找到相似度足够高的内容，可以换个说法或放宽条件",
            },
            ensure_ascii=False,
        )

    # 跨轮去重：把这次检索里"之前已经给过模型"的 chunk 剔掉。
    # 这是最省 token 的一刀 —— 模型换个角度重搜时，命中的往往是同一批 chunk，
    # 原样再塞一遍纯属浪费（前 200 字都一样，模型信息量增量为零）。
    fresh = results
    if ctx is not None:
        fresh = [r for r in results if str(r.get("chunk_id")) not in ctx.seen_chunk_ids]
        for r in fresh:
            ctx.seen_chunk_ids.add(str(r.get("chunk_id")))

    if not fresh:
        return json.dumps(
            {
                "ok": True,
                "query": question,
                "count": 0,
                "results": [],
                "note": "检索到的内容与之前重复，没有新信息。请基于已有结果回答。",
            },
            ensure_ascii=False,
        )

    # 累计引用，供前端渲染"引用来源"卡片。
    # 注意 contentPreview 只留 200 字 —— 它会被存进 message.metadata，
    # 每个引用都存 400 字的话，metadata 会膨胀得比正文还大。
    if ctx is not None:
        ctx.add_references(
            [
                {
                    "documentName": r.get("document_name") or "(未知文档)",
                    "contentPreview": _clip(str(r.get("content") or ""), 200),
                    "score": r.get("score"),
                    "sourceType": "document",
                    "knowledgeId": str(r.get("knowledge_id") or ""),
                    "documentId": str(r.get("document_id") or ""),
                    "chunkId": str(r.get("chunk_id") or ""),
                }
                for r in fresh
            ]
        )

    return json.dumps(
        {
            "ok": True,
            "query": question,
            "count": len(fresh),
            "results": [
                {
                    "documentName": r.get("document_name") or "(未知文档)",
                    "documentId": str(r.get("document_id") or ""),
                    "chunkIndex": r.get("chunk_index"),
                    "score": r.get("score"),
                    "content": _clip(str(r.get("content") or "")),
                }
                for r in fresh
            ],
        },
        ensure_ascii=False,
    )


@tool("list_knowledge_bases", "列出当前可检索的知识库及其规模。不确定有哪些资料时先调用这个。")
async def list_knowledge_bases() -> str:
    kb_ids = current_knowledge_ids()
    if not kb_ids:
        return json.dumps({"ok": False, "error": "当前 Agent 没有绑定知识库"}, ensure_ascii=False)

    stmt = text(
        """
        SELECT k.id, k.name, k.description,
               count(DISTINCT c.id) AS chunks,
               count(DISTINCT e.id) AS embeddings
        FROM knowledge k
        LEFT JOIN chunk c ON c.knowledge_id = k.id
        LEFT JOIN embedding e ON e.chunk_id = c.id
        WHERE k.id IN :kb_ids AND k.deleted = 0
        GROUP BY k.id, k.name, k.description
        """
    ).bindparams(bindparam("kb_ids", expanding=True))
    async with SessionLocal() as db:
        rows = (await db.execute(stmt, {"kb_ids": list(kb_ids)})).mappings().all()

    return json.dumps(
        {
            "ok": True,
            "count": len(rows),
            "knowledgeBases": [
                {
                    "id": str(r["id"]),
                    "name": r["name"],
                    "description": r["description"],
                    "chunks": int(r["chunks"] or 0),
                    "embeddings": int(r["embeddings"] or 0),
                    "searchable": int(r["embeddings"] or 0) > 0,
                }
                for r in rows
            ],
        },
        ensure_ascii=False,
    )


KB_TOOLS = [query_knowledge, list_knowledge_bases]
