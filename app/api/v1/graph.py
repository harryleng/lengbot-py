"""图谱 26 接口（对照 Java `StandaloneGraphController` + `KnowledgeGraphController`）。

路由顺序：`/api/graph` 下的静态段（import/search/stats/...）先于
`DELETE /api/graph`（方法不同无冲突）。

权限（knowledge 侧，Java 同款）：
- 查询（subgraph/search/stats/existing-docs）→ 成员即可
- 抽取/社区/导入/节点边增删 → developer+
- 清空整库图谱 → manager+
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Body, File, HTTPException, Query, UploadFile
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from sqlalchemy import select

from app.api.deps import CurrentUserId, DbSession
from app.core import neo4j_client
from app.core.db import SessionLocal
from app.models.entities import Knowledge
from app.schemas.common import Result
from app.services import graph_executors
from app.services.graph_service import GraphService, kb_label

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/graph", tags=["graph"])
kb_router = APIRouter(prefix="/api/knowledge", tags=["knowledge-graph"])

MAX_JSONL_SIZE = 5 * 1024 * 1024  # Java 同值


class GraphImportIn(BaseModel):
    """Java `GraphImportDTO`：triples 数组，字段 camelCase。"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    triples: list[dict[str, Any]]


class GraphExtractIn(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    document_ids: list[str] | None = None
    provider_id: str | None = None
    model_id: str | None = None
    # Java DTO 字段叫 schema，但 pydantic BaseModel 有同名方法 —— 改名 + alias 保持线上兼容
    schema_hint: str | None = Field(default=None, alias="schema")
    concurrency: int | None = None
    model_params: dict | None = None


class CommunitySummaryIn(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    provider_id: str | None = None
    model_id: str | None = None
    concurrency: int | None = None
    max_triples: int | None = None
    max_entities: int | None = None
    model_params: dict | None = None


# ======================================================================
# 独立图谱 /api/graph/*
# ======================================================================


def _svc_standalone() -> GraphService:
    return GraphService("standalone")


def _translate(exc: Exception) -> HTTPException:
    if isinstance(exc, neo4j_client.Neo4jUnavailable):
        return HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, HTTPException):
        return exc
    return HTTPException(status_code=400, detail=str(exc))


@router.post("/import/jsonl", response_model=Result[dict], summary="从 JSONL 文件导入三元组")
async def import_jsonl(
    file: UploadFile = File(...),
    provider_id: str | None = Query(default=None, alias="providerId"),
) -> Result[dict]:
    if not (file.filename or "").lower().endswith(".jsonl"):
        return Result.fail("仅支持 .jsonl 文件", code=400)
    data = await file.read()
    if len(data) > MAX_JSONL_SIZE:
        return Result.fail("文件超过 5MB 上限", code=400)

    triples: list[dict] = []
    for line in data.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            return Result.fail("JSONL 格式错误", code=400)
        if not (raw.get("head") and raw.get("relation") and raw.get("tail")):
            return Result.fail("JSONL 行缺少 head/relation/tail", code=400)
        triples.append(raw)
    if not triples:
        return Result.fail("JSONL 内容为空", code=400)

    try:
        return Result.ok(await _svc_standalone().import_triples(triples))
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc) from exc


@router.post("/import", response_model=Result[dict], summary="手动批量导入三元组")
async def import_triples(
    body: GraphImportIn, provider_id: str | None = Query(default=None, alias="providerId")
) -> Result[dict]:
    try:
        return Result.ok(await _svc_standalone().import_triples(body.triples))
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc) from exc


@router.get("/subgraph", response_model=Result[dict], summary="获取子图数据")
async def subgraph(
    keyword: str | None = Query(default=None),
    max_depth: int = Query(default=2, alias="maxDepth"),
    max_nodes: int = Query(default=50, alias="maxNodes"),
) -> Result[dict]:
    try:
        return Result.ok(await _svc_standalone().get_subgraph(keyword, max_depth, max_nodes))
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc) from exc


@router.get("/search", response_model=Result[list[dict]], summary="语义搜索节点")
async def search(
    query: str = Query(...),
    top_k: int = Query(default=10, alias="topK"),
    min_score: float | None = Query(default=None, alias="minScore"),
    provider_id: str | None = Query(default=None, alias="providerId"),
) -> Result[list[dict]]:
    try:
        return Result.ok(
            await _svc_standalone().semantic_search(query, top_k, min_score, standalone_only=True)
        )
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc) from exc


@router.get("/stats", response_model=Result[dict], summary="获取图谱统计")
async def stats() -> Result[dict]:
    try:
        return Result.ok(await _svc_standalone().get_stats())
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc) from exc


@router.delete("", response_model=Result[None], summary="清空独立图谱")
async def delete_all() -> Result[None]:
    try:
        await _svc_standalone().delete_all()
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc) from exc
    return Result.ok(None)


@router.post("/nodes", response_model=Result[dict], summary="创建节点")
async def create_node(
    name: str = Query(...),
    entity_type: str = Query(default="其他", alias="entityType"),
    description: str | None = Query(default=None),
    provider_id: str | None = Query(default=None, alias="providerId"),
) -> Result[dict]:
    try:
        return Result.ok(await _svc_standalone().create_node(name, entity_type, description))
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc) from exc


@router.put("/nodes/{element_id}", response_model=Result[dict], summary="更新节点")
async def update_node(
    element_id: str,
    name: str = Query(...),
    entity_type: str = Query(default="其他", alias="entityType"),
    description: str | None = Query(default=None),
) -> Result[dict]:
    try:
        return Result.ok(
            await _svc_standalone().update_node(element_id, name, entity_type, description)
        )
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc) from exc


@router.delete("/nodes/{element_id}", response_model=Result[None], summary="删除节点")
async def delete_node(element_id: str) -> Result[None]:
    try:
        await _svc_standalone().delete_node(element_id)
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc) from exc
    return Result.ok(None)


@router.post("/edges", response_model=Result[dict], summary="创建关系")
async def create_edge(
    head_name: str = Query(..., alias="headName"),
    relation_type: str = Query(..., alias="relationType"),
    tail_name: str = Query(..., alias="tailName"),
    description: str | None = Query(default=None),
) -> Result[dict]:
    try:
        return Result.ok(
            await _svc_standalone().create_edge(head_name, relation_type, tail_name, description)
        )
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc) from exc


@router.put("/edges/{element_id}", response_model=Result[dict], summary="更新关系")
async def update_edge(
    element_id: str,
    relation_type: str | None = Query(default=None, alias="relationType"),
    description: str | None = Query(default=None),
    weight: float | None = Query(default=None),
) -> Result[dict]:
    try:
        return Result.ok(
            await _svc_standalone().update_edge(element_id, relation_type, description, weight)
        )
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc) from exc


@router.delete("/edges/{element_id}", response_model=Result[None], summary="删除关系")
async def delete_edge(element_id: str) -> Result[None]:
    try:
        await _svc_standalone().delete_edge(element_id)
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc) from exc
    return Result.ok(None)


@router.get("/node-names", response_model=Result[list[str]], summary="所有节点名称")
async def node_names() -> Result[list[str]]:
    try:
        return Result.ok(await _svc_standalone().list_node_names())
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc) from exc


@router.post("/rebuild-index", response_model=Result[int], summary="重建向量索引")
async def rebuild_index() -> Result[int]:
    try:
        return Result.ok(await _svc_standalone().rebuild_vector_index())
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc) from exc


# ======================================================================
# 知识库图谱 /api/knowledge/{kid}/graph/*
# ======================================================================


def _svc_kb(kid: int) -> GraphService:
    return GraphService(kb_label(kid))


async def _perm(db: DbSession, kid: int, uid: int, role: str) -> None:
    from app.services.knowledge_service import KnowledgeService

    await KnowledgeService(db, uid).ensure_role(kid, role)




# ======================================================================
# 知识库图谱 /api/knowledge/{kid}/graph/*
# ======================================================================


def _svc_kb(kid: int) -> GraphService:
    return GraphService(kb_label(kid))


async def _perm(db: DbSession, kid: int, uid: int, role: str) -> None:
    from app.services.knowledge_service import KnowledgeService

    await KnowledgeService(db, uid).ensure_role(kid, role)


async def _create_graph_task(
    db: DbSession, *, task_type: str, name: str, user_id: int, ref_id: int, payload: dict
) -> dict:
    """建 task 行（JSONB 直接收 dict，别手动 dumps —— 双重编码的老坑），
    并 spawn 进程内执行体。返回 task_out 形态。"""
    import asyncio
    from datetime import datetime

    from app.core.db import SessionLocal
    from app.core.snowflake import new_id
    from app.models.entities import TaskRecord
    from app.services.task_service import task_out

    now = datetime.now()
    row = TaskRecord(
        id=new_id(), name=name, type=task_type, status="running", progress=0,
        payload=payload, cancel_requested=0, user_id=user_id, ref_id=ref_id,
        create_time=now, update_time=now, started_at=now,
    )
    db.add(row)
    await db.commit()
    tid = row.id

    runner = {
        "graph_extraction": graph_executors.run_graph_extraction,
        "community_detect": graph_executors.run_community_detect,
        "community_summary": graph_executors.run_community_summary,
    }[task_type]

    async def _finish(status: str, message: str | None, error: str | None) -> None:
        async with SessionLocal() as tdb:
            r = await tdb.get(TaskRecord, tid)
            if r is None:
                return
            r.status = status
            r.progress = 100 if status == "success" else (r.progress or 0)
            r.message = message
            r.error = error
            r.completed_at = datetime.now()
            r.update_time = datetime.now()
            await tdb.commit()

    async def _runner() -> None:
        try:
            result = await runner(tid, payload)
            await _finish("success", str(result)[:500], None)
        except asyncio.CancelledError:
            await _finish("cancelled", "用户取消", None)
        except Exception as exc:  # noqa: BLE001
            logger.exception("[Graph] 任务失败 taskId=%s", tid)
            await _finish("failed", str(exc)[:500], str(exc)[:1000])

    from app.services import task_recovery

    asyncio.create_task(task_recovery.tracked(tid, _runner()))

    # 直接构 out（行刚提交，重新 get 也行，但内存对象字段已齐）
    row.completed_at = None
    row.message = None
    row.error = None
    return task_out(row)


def _check_neo4j_or_503() -> None:
    # 查询端点在 Neo4j 不可用时降级空结果（service 内处理）；
    # 这里只拦写端点共用异常翻译
    pass


@kb_router.post("/{kid}/graph/extract", response_model=Result[dict], summary="触发图谱抽取（异步任务）")
async def extract(
    kid: int, db: DbSession, user_id: CurrentUserId,
    body: GraphExtractIn | None = Body(default=None),
) -> Result[dict]:
    try:
        await _perm(db, kid, user_id, "developer")
        await neo4j_client.ensure_available()
        cfg = body or GraphExtractIn()
        payload: dict[str, Any] = {"knowledgeId": kid}
        if cfg.document_ids:
            payload["documentIds"] = [int(x) for x in cfg.document_ids]
        if cfg.provider_id:
            payload["providerId"] = int(cfg.provider_id)
        if cfg.model_id:
            payload["modelId"] = cfg.model_id.strip()
        if cfg.schema_hint:
            payload["schema"] = cfg.schema_hint
        if cfg.concurrency:
            payload["concurrency"] = max(1, min(cfg.concurrency, 8))
        return Result.ok(await _create_graph_task(
            db, task_type="graph_extraction", name="图谱抽取",
            user_id=user_id, ref_id=kid, payload=payload,
        ))
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc) from exc


@kb_router.post(
    "/{kid}/graph/community/detect",
    response_model=Result[dict],
    summary="触发社区检测（异步任务）",
)
async def community_detect(kid: int, db: DbSession, user_id: CurrentUserId) -> Result[dict]:
    try:
        await _perm(db, kid, user_id, "developer")
        kb = await db.get(Knowledge, kid)
        if kb is None:
            raise HTTPException(status_code=404, detail="知识库不存在")
        await neo4j_client.ensure_available()
        return Result.ok(await _create_graph_task(
            db, task_type="community_detect", name="社区检测",
            user_id=user_id, ref_id=kid, payload={"knowledgeId": kid},
        ))
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc) from exc


@kb_router.post(
    "/{kid}/graph/community/summarize",
    response_model=Result[dict],
    summary="触发社区摘要（异步任务）",
)
async def community_summarize(
    kid: int, db: DbSession, user_id: CurrentUserId,
    body: CommunitySummaryIn | None = Body(default=None),
) -> Result[dict]:
    try:
        await _perm(db, kid, user_id, "developer")
        kb = await db.get(Knowledge, kid)
        if kb is None:
            raise HTTPException(status_code=404, detail="知识库不存在")
        await neo4j_client.ensure_available()
        payload: dict[str, Any] = {"knowledgeId": kid}
        if body:
            if body.provider_id:
                payload["providerId"] = int(body.provider_id)
            if body.model_id:
                payload["modelId"] = body.model_id.strip()
            if body.concurrency:
                payload["concurrency"] = max(1, min(body.concurrency, 32))
            if body.max_entities:
                payload["maxEntities"] = max(1, body.max_entities)
        return Result.ok(await _create_graph_task(
            db, task_type="community_summary", name="社区摘要",
            user_id=user_id, ref_id=kid, payload=payload,
        ))
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc) from exc


@kb_router.post("/{kid}/graph/import", response_model=Result[dict], summary="批量导入三元组（developer+）")
async def kb_import_triples(
    kid: int, body: GraphImportIn, db: DbSession, user_id: CurrentUserId,
    provider_id: str | None = Query(default=None, alias="providerId"),
) -> Result[dict]:
    try:
        await _perm(db, kid, user_id, "developer")
        return Result.ok(await _svc_kb(kid).import_triples(
            body.triples, knowledge_id=kid, source="import", graph_source="import",
        ))
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc) from exc


@kb_router.get("/{kid}/graph/subgraph", response_model=Result[dict], summary="知识库子图（可视化）")
async def kb_subgraph(
    kid: int,
    document_id: str | None = Query(default=None, alias="documentId"),
    keyword: str | None = Query(default=None),
    max_depth: int = Query(default=2, alias="maxDepth"),
    max_nodes: int = Query(default=50, alias="maxNodes"),
) -> Result[dict]:
    try:
        return Result.ok(await _svc_kb(kid).get_subgraph(
            keyword, max_depth, max_nodes,
            int(document_id) if document_id else None,
        ))
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc) from exc


@kb_router.get("/{kid}/graph/search", response_model=Result[list[dict]], summary="语义搜索图谱节点")
async def kb_search(
    kid: int,
    query: str = Query(...),
    top_k: int = Query(default=10, alias="topK"),
    min_score: float | None = Query(default=None, alias="minScore"),
    document_id: str | None = Query(default=None, alias="documentId"),
    provider_id: str | None = Query(default=None, alias="providerId"),
) -> Result[list[dict]]:
    try:
        return Result.ok(await _svc_kb(kid).semantic_search(
            query, top_k, min_score, int(document_id) if document_id else None,
        ))
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc) from exc


@kb_router.get("/{kid}/graph/stats", response_model=Result[dict], summary="知识库图谱统计")
async def kb_stats(
    kid: int, document_id: str | None = Query(default=None, alias="documentId")
) -> Result[dict]:
    try:
        return Result.ok(await _svc_kb(kid).get_stats(int(document_id) if document_id else None))
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc) from exc


@kb_router.delete("/{kid}/graph", response_model=Result[None], summary="清空知识库图谱（manager+）")
async def kb_delete_graph(kid: int, db: DbSession, user_id: CurrentUserId) -> Result[None]:
    try:
        await _perm(db, kid, user_id, "manager")
        await _svc_kb(kid).delete_all()
        async with SessionLocal() as tdb:
            from app.models.entities import KnowledgeGraph

            kg = (
                await tdb.execute(
                    select(KnowledgeGraph).where(
                        KnowledgeGraph.knowledge_id == kid, KnowledgeGraph.deleted == 0
                    )
                )
            ).scalars().first()
            if kg is not None:
                kg.node_count = 0
                kg.edge_count = 0
                kg.status = "completed"
                kg.update_time = datetime.now()
                await tdb.commit()
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc) from exc
    return Result.ok(None)


@kb_router.delete(
    "/{kid}/graph/documents/{document_id}",
    response_model=Result[None],
    summary="删除单文档图谱（developer+）",
)
async def kb_delete_doc_graph(
    kid: int, document_id: int, db: DbSession, user_id: CurrentUserId
) -> Result[None]:
    try:
        await _perm(db, kid, user_id, "developer")
        await _svc_kb(kid).delete_by_document(kid, document_id)
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc) from exc
    return Result.ok(None)


@kb_router.get(
    "/{kid}/graph/existing-docs",
    response_model=Result[list[int]],
    summary="批量检查已有图谱的文档",
)
async def kb_existing_docs(
    kid: int, document_ids: list[int] = Query(default=[], alias="documentIds"),
) -> Result[list[int]]:
    try:
        return Result.ok(await _svc_kb(kid).get_existing_doc_ids(kid, document_ids))
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc) from exc


@kb_router.post("/{kid}/graph/nodes", response_model=Result[dict], summary="手动创建图谱节点（developer+）")
async def kb_create_node(
    kid: int, db: DbSession, user_id: CurrentUserId,
    name: str = Query(...),
    entity_type: str = Query(default="其他", alias="entityType"),
    description: str | None = Query(default=None),
) -> Result[dict]:
    try:
        await _perm(db, kid, user_id, "developer")
        return Result.ok(await _svc_kb(kid).create_node(name, entity_type, description))
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc) from exc


@kb_router.delete(
    "/{kid}/graph/nodes/{element_id}",
    response_model=Result[None],
    summary="删除图谱节点（developer+）",
)
async def kb_delete_node(
    kid: int, element_id: str, db: DbSession, user_id: CurrentUserId
) -> Result[None]:
    try:
        await _perm(db, kid, user_id, "developer")
        await _svc_kb(kid).delete_node(element_id)
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc) from exc
    return Result.ok(None)


@kb_router.post("/{kid}/graph/edges", response_model=Result[dict], summary="手动创建图谱关系（developer+）")
async def kb_create_edge(
    kid: int, db: DbSession, user_id: CurrentUserId,
    head_name: str = Query(..., alias="headName"),
    relation_type: str = Query(..., alias="relationType"),
    tail_name: str = Query(..., alias="tailName"),
    description: str | None = Query(default=None),
) -> Result[dict]:
    try:
        await _perm(db, kid, user_id, "developer")
        return Result.ok(await _svc_kb(kid).create_edge(head_name, relation_type, tail_name, description))
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc) from exc


@kb_router.delete(
    "/{kid}/graph/edges/{element_id}",
    response_model=Result[None],
    summary="删除图谱关系（developer+）",
)
async def kb_delete_edge(
    kid: int, element_id: str, db: DbSession, user_id: CurrentUserId
) -> Result[None]:
    try:
        await _perm(db, kid, user_id, "developer")
        await _svc_kb(kid).delete_edge(element_id)
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc) from exc
    return Result.ok(None)
