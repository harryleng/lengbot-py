"""API Key 管理（对照 Java `ApiKeyController`）。

所有接口都带 `user_id` 归属校验（Java 的 `getByIdAndCheckOwner`），
缺了就是 IDOR：A 用户能删掉 B 用户的 key。

`secret` 只在 create / regenerate 返回一次，list 永远不带明文。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.api.deps import CurrentUserId, DbSession
from app.schemas.api_key import ApiKeyCreateIn, ApiKeyOut, ApiKeyWithSecret
from app.schemas.common import Result
from app.services.api_key_service import ApiKeyService

router = APIRouter(prefix="/api/api-keys", tags=["api-keys"])


@router.get("", response_model=Result[list[ApiKeyOut]], summary="当前用户的 API Key 列表")
async def list_keys(db: DbSession, user_id: CurrentUserId) -> Result[list[ApiKeyOut]]:
    return Result.ok(await ApiKeyService(db).list_by_user(user_id))


@router.post("", response_model=Result[ApiKeyWithSecret], summary="创建 API Key")
async def create_key(
    dto: ApiKeyCreateIn, db: DbSession, user_id: CurrentUserId
) -> Result[ApiKeyWithSecret]:
    svc = ApiKeyService(db)
    try:
        created = await svc.create(
            user_id=user_id,
            name=dto.name,
            permissions=dto.permissions,
            expires_at=dto.expires_at,
            agent_ids=dto.agent_ids,
            rate_limit=dto.rate_limit,
            daily_quota=dto.daily_quota,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return Result.ok(created)


@router.patch("/{key_id}/toggle", response_model=Result[None], summary="启用/禁用 API Key")
async def toggle_key(db: DbSession, user_id: CurrentUserId, key_id: int) -> Result[None]:
    try:
        await ApiKeyService(db).toggle(key_id, user_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return Result.ok(None)


@router.post(
    "/{key_id}/regenerate",
    response_model=Result[ApiKeyWithSecret],
    summary="重新生成 API Key",
)
async def regenerate_key(
    db: DbSession, user_id: CurrentUserId, key_id: int
) -> Result[ApiKeyWithSecret]:
    try:
        return Result.ok(await ApiKeyService(db).regenerate(key_id, user_id))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/{key_id}", response_model=Result[None], summary="删除 API Key")
async def delete_key(db: DbSession, user_id: CurrentUserId, key_id: int) -> Result[None]:
    try:
        await ApiKeyService(db).delete(key_id, user_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return Result.ok(None)
