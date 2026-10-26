"""LLM 调用链可观测性（对照 Java `LlmTraceController`）。

注意 `GET /traces` 是 **GET + query 参数**（Java 用 `LlmTraceRequestDTO` 平铺绑定），
不是 POST + body；`DELETE /traces` 才是 body 裸数组 `[1,2]`。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.api.deps import DbSession
from app.schemas.common import Result
from app.schemas.misc import LlmTraceDetail, TraceOverview, TracePage
from app.services.llm_trace_service import LlmTraceService

router = APIRouter(prefix="/api/observability", tags=["observability"])


@router.get("/traces", response_model=Result[TracePage], summary="调用链列表")
async def list_traces(
    db: DbSession,
    page_num: int = Query(default=1, alias="pageNum", ge=1),
    page_size: int = Query(default=20, alias="pageSize", ge=1, le=200),
    session_id: int | None = Query(default=None, alias="sessionId"),
    request_id: str | None = Query(default=None, alias="requestId"),
    agent_id: int | None = Query(default=None, alias="agentId"),
    status: str | None = Query(default=None),
    start_time: str | None = Query(default=None, alias="startTime"),
    end_time: str | None = Query(default=None, alias="endTime"),
    trace_source: str | None = Query(default=None, alias="traceSource"),
) -> Result[TracePage]:
    svc = LlmTraceService(db)
    try:
        page = await svc.page_list(
            page_num=page_num,
            page_size=page_size,
            session_id=session_id,
            request_id=request_id,
            agent_id=agent_id,
            status=status,
            start_time=start_time,
            end_time=end_time,
            trace_source=trace_source,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return Result.ok(page)


@router.delete("/traces", response_model=Result[None], summary="批量删除调用链")
async def delete_traces(db: DbSession, ids: list[int]) -> Result[None]:
    await LlmTraceService(db).delete(ids)
    return Result.ok(None)


@router.get("/overview", response_model=Result[TraceOverview], summary="调用链汇总统计")
async def overview(
    db: DbSession,
    trace_source: str | None = Query(default=None, alias="traceSource"),
) -> Result[TraceOverview]:
    return Result.ok(await LlmTraceService(db).overview(trace_source))


@router.get("/traces/{trace_id}", response_model=Result[LlmTraceDetail], summary="调用链详情")
async def trace_detail(db: DbSession, trace_id: int) -> Result[LlmTraceDetail]:
    try:
        return Result.ok(await LlmTraceService(db).detail(trace_id))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
