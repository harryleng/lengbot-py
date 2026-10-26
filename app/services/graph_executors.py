"""图谱抽取 / 社区检测 / 社区摘要 执行器（对照 Java `GraphExtractionExecutor`、
`CommunityDetectExecutor`、`CommunitySummarizeExecutor` 的紧凑移植）。

三个执行器都由 `/api/knowledge/{id}/graph/*` 的触发端点创建 task 后
`asyncio.create_task` 进程内执行（与评测同款：重启即丢，已知限制）。

- **抽取**：每 3 个 chunk 一批 → LLM 逐块抽三元组（JSONL）→ 写 Neo4j
  （document_id + graph_source 标记）→ 回写 GraphDocument / KnowledgeGraph 状态。
  **会调 LLM（运行时行为），测试只测触发与快败路径**。
- **社区检测**：纯算法（并查集弱连通分量），不调模型 —— 可完整测试。
- **社区摘要**：按社区聚合成员 + 关系 → LLM 生成摘要 → 写 Community 节点。
  同抽取，测试不触发。
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import neo4j_client
from app.core.snowflake import new_id
from app.models.entities import (
    Chunk,
    Document,
    GraphDocument,
    Knowledge,
    KnowledgeGraph,
    TaskRecord,
)
from app.services.graph_service import GraphService, kb_label, _q

logger = logging.getLogger(__name__)

COMMUNITY_MAX_EDGES = 50000

EXTRACT_PROMPT = """你是知识图谱构建专家。从以下文本中抽取实体和关系，
以 JSONL 输出（每行一个 JSON 对象），字段：
{"head":"实体名","headType":"实体类型","headDesc":"实体描述(20字内)","relation":"关系名(动词或短语)","relationDesc":"关系描述(20字内)","tail":"实体名","tailType":"实体类型","tailDesc":"实体描述(20字内)"}

要求：
1. 只输出 JSONL，不要输出任何解释、markdown 代码块标记。
2. 实体类型使用通用类别（人物/组织/概念/技术/地点/事件/产品/其他）。
3. 没有可抽取的实体关系时输出空内容。

文本：
%s
"""


async def _get_or_create_graph(db: AsyncSession, knowledge_id: int) -> KnowledgeGraph:
    row = (
        await db.execute(
            select(KnowledgeGraph).where(
                KnowledgeGraph.knowledge_id == knowledge_id, KnowledgeGraph.deleted == 0
            )
        )
    ).scalars().first()
    if row is None:
        now = datetime.now()
        row = KnowledgeGraph(
            id=new_id(), knowledge_id=knowledge_id, status="pending",
            node_count=0, edge_count=0, create_time=now, update_time=now,
        )
        db.add(row)
        await db.commit()
    return row


async def _clean_stale_status(db: AsyncSession, kg: KnowledgeGraph) -> None:
    """running/pending 但 task 行已终结或不存在 → 复位为 failed（幂等防卡死）。"""
    if kg.status not in ("pending", "running") or not kg.task_id:
        return
    task = await db.get(TaskRecord, int(kg.task_id))
    if task is None or (task.status or "") in ("success", "failed", "cancelled"):
        kg.status = "failed"
        kg.error_message = "历史任务已中断，可重新触发抽取"
        kg.task_id = None
        kg.update_time = datetime.now()
        await db.commit()


# ==================== 抽取（运行时调 LLM；测试不触发） ====================

async def run_graph_extraction(task_id: int, payload: dict) -> str:
    knowledge_id = int(payload["knowledgeId"])
    document_ids = payload.get("documentIds") or None
    provider_id = payload.get("providerId")
    model_id = payload.get("modelId")
    concurrency = max(1, min(int(payload.get("concurrency") or 2), 8))

    async with SessionLocal() as db:
        kg = await _get_or_create_graph(db, knowledge_id)
        await _clean_stale_status(db, kg)
        if kg.status in ("pending", "running") and kg.task_id:
            raise RuntimeError("已有运行中的抽取任务")

        kg.status = "running"
        kg.task_id = task_id
        kg.error_message = None
        kg.update_time = datetime.now()
        await db.commit()

    graph_docs: list[GraphDocument] = []
    try:
        async with SessionLocal() as db:
            kg = await _get_or_create_graph(db, knowledge_id)
            is_single = document_ids is not None and len(document_ids) == 1
            doc_ids = document_ids or [
                int(d)
                for d in (
                    await db.execute(
                        select(Document.id).where(
                            Document.knowledge_id == knowledge_id,
                            Document.status == "completed",
                            Document.deleted == 0,
                        )
                    )
                ).scalars().all()
            ]
            if not doc_ids:
                raise RuntimeError("没有可抽取的文档（无已完成入库的文档）")

            if is_single:
                for doc_id in doc_ids:
                    gd = (
                        await db.execute(
                            select(GraphDocument).where(
                                GraphDocument.graph_id == kg.id,
                                GraphDocument.document_id == doc_id,
                                GraphDocument.deleted == 0,
                            )
                        )
                    ).scalars().first()
                    if gd is None:
                        gd = GraphDocument(
                            id=new_id(), graph_id=kg.id, document_id=doc_id,
                            status="pending", entity_count=0, relation_count=0,
                            create_time=datetime.now(), update_time=datetime.now(),
                        )
                        db.add(gd)
                    else:
                        gd.status = "pending"
                        gd.entity_count = 0
                        gd.relation_count = 0
                        gd.error_message = None
                        gd.update_time = datetime.now()
                    graph_docs.append(gd)
                await db.commit()

        total_nodes = 0
        total_edges = 0
        total_chunks = 0
        graph_source = "merged" if (document_ids is None or len(document_ids) != 1) else "single_doc"
        svc = GraphService(kb_label(knowledge_id))

        for doc_id in doc_ids:
            async with SessionLocal() as db:
                chunks = (
                    await db.execute(
                        select(Chunk).where(
                            Chunk.knowledge_id == knowledge_id,
                            Chunk.document_id == doc_id,
                            Chunk.status == "vectorized",
                            Chunk.deleted == 0,
                        )
                    )
                ).scalars().all()
                total_chunks += len(chunks)
                if graph_docs:
                    gd = next((g for g in graph_docs if g.document_id == doc_id), None)
                    if gd:
                        gd.status = "running"
                        gd.update_time = datetime.now()
                        await db.commit()

            triples = await _extract_from_chunks(
                [c.content for c in chunks], provider_id, model_id, concurrency, task_id
            )
            doc_stats = {"nodeCount": 0, "edgeCount": 0}
            if triples:
                doc_stats = await svc.import_triples(
                    triples, knowledge_id=knowledge_id, document_id=doc_id,
                    source="auto", graph_source=graph_source,
                )
                total_nodes += doc_stats["nodeCount"]
                total_edges += doc_stats["edgeCount"]

            async with SessionLocal() as db:
                gd = next(
                    (g for g in graph_docs if int(g.document_id) == int(doc_id)), None
                ) if graph_docs else None
                if gd is not None:
                    gd.status = "completed"
                    gd.entity_count = doc_stats["nodeCount"]
                    gd.relation_count = doc_stats["edgeCount"]
                    gd.update_time = datetime.now()
                    await db.commit()

        if total_chunks > 0 and total_nodes == 0 and total_edges == 0:
            raise RuntimeError("图谱抽取失败：未能从文档中抽取到任何实体关系")
        if total_chunks == 0:
            raise RuntimeError("图谱抽取失败：文档没有可用的已向量化分块")

        # 统计回写（knowledge + knowledge_graph）
        stats = await svc.get_stats()
        async with SessionLocal() as db:
            kg = await _get_or_create_graph(db, knowledge_id)
            kg.status = "completed"
            kg.node_count = stats["nodeCount"]
            kg.edge_count = stats["edgeCount"]
            kg.task_id = None
            kg.error_message = None
            kg.update_time = datetime.now()
            kb = await db.get(Knowledge, knowledge_id)
            if kb is not None:
                kb.node_count = stats["nodeCount"]
                kb.edge_count = stats["edgeCount"]
                kb.update_time = datetime.now()
            await db.commit()

        msg = (
            f"图谱抽取完成, knowledgeId={knowledge_id}, "
            f"nodes={stats['nodeCount']}, edges={stats['edgeCount']}"
        )
        logger.info("[GraphExtract] %s", msg)
        return msg

    except Exception as exc:  # noqa: BLE001
        async with SessionLocal() as db:
            kg = await _get_or_create_graph(db, knowledge_id)
            kg.status = "failed"
            kg.error_message = str(exc)
            kg.task_id = None
            kg.update_time = datetime.now()
            for gd in graph_docs:
                if gd.status in ("pending", "running"):
                    gd.status = "failed"
                    gd.error_message = str(exc)
                    gd.update_time = datetime.now()
            await db.commit()
        raise


async def _extract_from_chunks(
    contents: list[str], provider_id: Any, model_id: Any, concurrency: int, task_id: int
) -> list[dict]:
    """每 3 个 chunk 一批并发抽（Java 同款 batch=3），LLM 失败逐块降级。"""
    return await _extract_impl(contents, provider_id, model_id, concurrency, task_id)


async def _extract_impl(contents, provider_id, model_id, concurrency, task_id):
    from app.services.rag_eval_engine import call_llm

    sem = asyncio.Semaphore(concurrency)
    all_triples: list[dict] = []
    completed = 0

    async def one(content: str) -> None:
        nonlocal completed
        async with sem:
            raw = await call_llm(
                provider_id, model_id, EXTRACT_PROMPT % content[:3000]
            )
            completed += 1
            if raw:
                all_triples.extend(_parse_triples(raw))

    batch_size = 3
    for i in range(0, len(contents), batch_size):
        batch = contents[i : i + batch_size]
        await asyncio.gather(*(one(c) for c in batch))
        from app.core.db import SessionLocal
        from sqlalchemy import text as _text

        async with SessionLocal() as db:
            await db.execute(
                _text("UPDATE task SET progress = :p, message = :m, update_time = :ts WHERE id = :i"),
                {"p": min(90, int(completed / max(1, len(contents)) * 80) + 10),
                 "m": f"正在抽取 ({completed}/{len(contents)})...",
                 "ts": datetime.now(), "i": task_id},
            )
            await db.commit()
    return all_triples


def _parse_triples(raw: str) -> list[dict]:
    out: list[dict] = []
    for line in raw.splitlines():
        line = line.strip().strip("`")
        if not line or line.startswith("{") is False:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("head") and obj.get("relation") and obj.get("tail"):
            out.append(obj)
    return out


# ==================== 社区检测（纯算法，不调模型） ====================

async def run_community_detect(task_id: int, payload: dict) -> str:
    knowledge_id = int(payload["knowledgeId"])
    label = kb_label(knowledge_id)
    await neo4j_client.ensure_available()

    async with SessionLocal() as db:
        await db.execute(
            text("UPDATE task SET progress = 5, message = :m, update_time = :ts WHERE id = :i"),
            {"m": "正在清理旧社区数据...", "ts": datetime.now(), "i": task_id},
        )
        await db.commit()

    # 1. 清理旧社区（幂等）
    await neo4j_client.run(
        f"MATCH (c:Community:{_q(label)}) DETACH DELETE c"
    )
    await neo4j_client.run(
        f"MATCH (n:Entity:{_q(label)}) WHERE n.community_id IS NOT NULL REMOVE n.community_id"
    )

    # 2. 加载实体与边
    entities = [
        r["name"]
        for r in await neo4j_client.query_read(
            f"MATCH (n:Entity:{_q(label)}) WHERE n.name IS NOT NULL RETURN n.name AS name"
        )
    ]
    if not entities:
        raise RuntimeError("图谱为空，没有可检测的实体，请先执行图谱抽取")

    edge_rows = await neo4j_client.query_read(
        f"MATCH (a:Entity:{_q(label)})-[r:RELATION]-(b:Entity:{_q(label)}) "
        "RETURN a.name AS s, b.name AS t LIMIT $maxEdges",
        {"maxEdges": COMMUNITY_MAX_EDGES},
    )
    edges = [(r["s"], r["t"]) for r in edge_rows if r["s"] and r["t"]]

    # 3. 并查集弱连通分量（成员 ≥2，按大小降序）
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in edges:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    groups: dict[str, list[str]] = {}
    for name in entities:
        groups.setdefault(find(name), []).append(name)
    communities = sorted(
        (sorted(m for m in members if members) for members in groups.values() if len(members) >= 2),
        key=len,
        reverse=True,
    )

    if not communities:
        msg = f"社区检测完成, knowledgeId={knowledge_id}, 未发现含关系的社区（孤立实体 {len(entities)} 个）"
        logger.warning("[CommunityDetect] %s", msg)
        return msg

    # 4. 写回 community_id（UNWIND 批量）
    for cid, members in enumerate(communities):
        await neo4j_client.run(
            f"UNWIND $names AS name "
            f"MATCH (n:Entity:{_q(label)} {{name: name}}) SET n.community_id = $cid",
            {"names": members, "cid": cid},
        )
        # 5. Community 元信息节点
        await neo4j_client.run(
            f"MERGE (c:Community:{_q(label)} {{community_id: $cid}}) "
            "SET c.member_count = $cnt, c.updated_at = datetime()",
            {"cid": cid, "cnt": len(members)},
        )

    covered = sum(len(m) for m in communities)
    largest = len(communities[0])
    msg = (
        f"社区检测完成, knowledgeId={knowledge_id}, 社区数={len(communities)}, "
        f"覆盖实体={covered}/{len(entities)}, 最大社区={largest}"
    )
    logger.info("[CommunityDetect] %s", msg)
    return msg


# ==================== 社区摘要（运行时调 LLM；测试不触发） ====================

async def run_community_summary(task_id: int, payload: dict) -> str:
    knowledge_id = int(payload["knowledgeId"])
    label = kb_label(knowledge_id)
    provider_id = payload.get("providerId")
    model_id = payload.get("modelId")
    max_entities = max(1, int(payload.get("maxEntities") or 30))

    await neo4j_client.ensure_available()
    rows = await neo4j_client.query_read(
        f"MATCH (c:Community:{_q(label)}) RETURN c.community_id AS cid, c.member_count AS cnt "
        "ORDER BY c.member_count DESC"
    )
    if not rows:
        raise RuntimeError("没有社区数据，请先执行社区检测")

    from app.services.rag_eval_engine import call_llm

    summarized = 0
    for idx, row in enumerate(rows):
        cid = int(row["cid"])
        members = [
            r["name"]
            for r in await neo4j_client.query_read(
                f"MATCH (n:Entity:{_q(label)} {{community_id: $cid}}) "
                "RETURN n.name AS name LIMIT $limit",
                {"cid": cid, "limit": max_entities},
            )
        ]
        rels = [
            f"{r['s']} -[{r['rel']}]-> {r['t']}"
            for r in await neo4j_client.query_read(
                f"MATCH (a:Entity:{_q(label)} {{community_id: $cid}})"
                f"-[r:RELATION]-(b:Entity:{_q(label)}) "
                "RETURN a.name AS s, r.relation_type AS rel, b.name AS t LIMIT 40",
                {"cid": cid},
            )
        ]
        prompt = (
            "以下是知识图谱中同一社区的实体和关系，请用 100 字以内概括这个社区共同围绕的主题：\n"
            f"实体：{', '.join(members)}\n关系：{'; '.join(rels) or '（无）'}\n只输出概括文本。"
        )
        summary = await call_llm(provider_id, model_id, prompt)
        if summary:
            await neo4j_client.run(
                f"MATCH (c:Community:{_q(label)} {{community_id: $cid}}) "
                "SET c.summary = $s, c.updated_at = datetime()",
                {"cid": cid, "s": summary.strip()[:500]},
            )
            summarized += 1
        from app.core.db import SessionLocal
        from sqlalchemy import text as _text

        async with SessionLocal() as db:
            await db.execute(
                _text("UPDATE task SET progress = :p, message = :m, update_time = :ts WHERE id = :i"),
                {"p": 10 + int((idx + 1) / len(rows) * 85),
                 "m": f"正在生成社区摘要 ({idx + 1}/{len(rows)})...",
                 "ts": datetime.now(), "i": task_id},
            )
            await db.commit()

    return f"社区摘要完成, knowledgeId={knowledge_id}, 摘要社区={summarized}/{len(rows)}"


from app.core.db import SessionLocal  # noqa: E402
from sqlalchemy import text  # noqa: E402
from typing import Any  # noqa: E402
