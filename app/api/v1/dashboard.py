"""Dashboard 统计（对照 Java `DashboardController`）。

出参字段名全部照抄：前端 `dashboard.js` 按 key 取值，少一个 key 面板就空一格。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.api.deps import DbSession
from app.schemas.common import Result
from app.schemas.misc import DashboardAgent, DashboardBasic, DashboardChat, DashboardKnowledge
from app.services.dashboard_service import DashboardService

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("/basic", response_model=Result[DashboardBasic], summary="基础统计概览")
async def basic(db: DbSession) -> Result[DashboardBasic]:
    return Result.ok(await DashboardService(db).basic())


@router.get("/agents", response_model=Result[DashboardAgent], summary="Agent 统计详情")
async def agents(db: DbSession) -> Result[DashboardAgent]:
    return Result.ok(await DashboardService(db).agents())


@router.get(
    "/knowledge", response_model=Result[DashboardKnowledge], summary="知识库统计详情"
)
async def knowledge(db: DbSession) -> Result[DashboardKnowledge]:
    return Result.ok(await DashboardService(db).knowledge())


@router.get("/chat", response_model=Result[DashboardChat], summary="对话统计详情")
async def chat(
    db: DbSession,
    days: int | None = Query(default=None),
    start_date: str | None = Query(default=None, alias="startDate"),
    end_date: str | None = Query(default=None, alias="endDate"),
) -> Result[DashboardChat]:
    try:
        stats = await DashboardService(db).chat(days, start_date, end_date)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return Result.ok(stats)
