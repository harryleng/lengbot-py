"""用户个人配置（对照 Java `UserPreferenceController`）。

数据落在 `users.config` JSONB 里，和头像框/等级共用一列。
服务层已保证「只改这 6 个 key、整列写回」，API 层不再碰 config 结构。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.api.deps import CurrentUserId, DbSession
from app.schemas.common import Result
from app.schemas.user_preference import UserPreferenceOut, UserPreferenceUpdateIn
from app.services.user_preference_service import UserPreferenceService

router = APIRouter(prefix="/api/user/preferences", tags=["user-preferences"])


@router.get("", response_model=Result[UserPreferenceOut], summary="获取当前用户个人配置")
async def get_preferences(
    db: DbSession, user_id: CurrentUserId
) -> Result[UserPreferenceOut]:
    try:
        return Result.ok(await UserPreferenceService(db).get(user_id))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.put("", response_model=Result[UserPreferenceOut], summary="更新当前用户个人配置")
async def update_preferences(
    dto: UserPreferenceUpdateIn, db: DbSession, user_id: CurrentUserId
) -> Result[UserPreferenceOut]:
    try:
        return Result.ok(await UserPreferenceService(db).update(user_id, dto))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
