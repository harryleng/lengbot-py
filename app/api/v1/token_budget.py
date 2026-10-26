"""Token 预算接口（对照 Java `SystemConfigController` 的 `/token-budget/*` 5 个端点）。

PUT config 与 GET ranking **仅管理员**（ranking 是他人的消耗数据，
普通用户可查就是越权看别人账单）；my-usage 的 userId 强制从登录态取。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.api.deps import CurrentUserId, DbSession
from app.models.entities import User
from app.schemas.common import Result
from app.services.token_budget_service import TokenBudgetService

router = APIRouter(prefix="/api/system-config", tags=["token-budget"])


async def _admin_svc(db: DbSession, user_id: CurrentUserId) -> TokenBudgetService:
    row = await db.get(User, user_id)
    if row is None or row.role != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return TokenBudgetService(db)


@router.get("/token-budget/config", response_model=Result[dict], summary="获取 Token 限额配置")
async def get_config(db: DbSession) -> Result[dict]:
    return Result.ok(await TokenBudgetService(db).get_config())


@router.put("/token-budget/config", response_model=Result[None], summary="更新 Token 限额配置")
async def update_config(
    config: dict, db: DbSession, user_id: CurrentUserId
) -> Result[None]:
    svc = await _admin_svc(db, user_id)
    try:
        await svc.update_config(config)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return Result.ok(None)


@router.get("/token-budget/stats", response_model=Result[dict], summary="全局 Token 使用统计")
async def stats(db: DbSession) -> Result[dict]:
    return Result.ok(await TokenBudgetService(db).get_global_stats())


@router.get(
    "/token-budget/ranking",
    response_model=Result[list[dict]],
    summary="用户 Token 消耗排行（仅管理员）",
)
async def ranking(
    db: DbSession,
    user_id: CurrentUserId,
    range: str = Query(default="today"),
    limit: int = Query(default=20, ge=1, le=100),
) -> Result[list[dict]]:
    svc = await _admin_svc(db, user_id)
    try:
        return Result.ok(await svc.get_user_ranking(range, limit))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/token-budget/my-usage", response_model=Result[dict], summary="本人 Token 用量")
async def my_usage(db: DbSession, user_id: CurrentUserId) -> Result[dict]:
    # userId 强制从登录态取（Java 注释原文："前端不可传，杜绝越权"）
    return Result.ok(await TokenBudgetService(db).get_my_usage(user_id))
