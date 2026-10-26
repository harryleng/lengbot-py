"""文档在线编辑 5 接口（对照 Java `DocumentEditController`）。

冲突（60014）走 **HTTP 409**、不支持类型（60015）走 400 —— Java 用
`HttpStatus.CONFLICT`，前端按 body.code 区分"刷新重试"和"提示不可编辑"。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from app.api.deps import CurrentUserId, DbSession
from app.schemas.common import CamelModel, Result
from app.services.document_edit_service import (
    ERR_CONFLICT,
    ERR_UNSUPPORTED,
    DocumentEditService,
    DocumentVersionService,
)

router = APIRouter(prefix="/api/documents", tags=["document-edit"])


class DocumentEditIn(CamelModel):
    content: str
    edit_mode: str | None = None
    expected_hash: str | None = None


def _translate(exc: Exception) -> HTTPException | Result:
    """业务异常 → 前端统一格式。

    ⚠️ 冲突（60014）/不支持（60015）必须保留 **Java 的业务码**，
    不能让全局 handler 把它们洗成 code=409/400 —— 前端按 code 区分
    "刷新重试"（60014）和"提示不可编辑"（60015）。HTTP 状态恒 200
    与全站约定一致。
    """
    code = getattr(exc, "code", None)
    if code == ERR_CONFLICT:
        return Result.fail(str(exc), code=ERR_CONFLICT)
    unsupported = ERR_UNSUPPORTED
    if code == unsupported:
        return Result.fail(str(exc), code=unsupported)
    if isinstance(exc, LookupError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, PermissionError):
        return HTTPException(status_code=403, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


@router.get(
    "/{document_id}/editable-content",
    response_model=Result[dict[str, Any]],
    summary="获取文档可编辑内容",
)
async def get_editable_content(
    db: DbSession, user_id: CurrentUserId, document_id: int
) -> Result[dict[str, Any]]:
    try:
        return Result.ok(await DocumentEditService(db, user_id).get_editable_content(document_id))
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc) from exc


@router.put(
    "/{document_id}/content",
    response_model=Result[dict[str, Any]],
    summary="保存文档编辑内容（触发全量重建）",
)
async def save_content(
    body: DocumentEditIn, db: DbSession, user_id: CurrentUserId, document_id: int
) -> Result[dict[str, Any]]:
    try:
        out = await DocumentEditService(db, user_id).save_content(
            document_id, body.content, body.edit_mode or "", body.expected_hash
        )
    except Exception as exc:  # noqa: BLE001
        translated = _translate(exc)
        if isinstance(translated, Result):
            return translated
        raise translated
    return Result.ok(out)


@router.get(
    "/{document_id}/versions",
    response_model=Result[list[dict[str, Any]]],
    summary="获取文档版本列表",
)
async def list_versions(
    db: DbSession, user_id: CurrentUserId, document_id: int
) -> Result[list[dict[str, Any]]]:
    try:
        return Result.ok(await DocumentVersionService(db, user_id).list_versions(document_id))
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc) from exc


@router.get(
    "/{document_id}/versions/{version_id}",
    response_model=Result[str],
    summary="获取指定版本内容",
)
async def get_version_content(
    db: DbSession, user_id: CurrentUserId, document_id: int, version_id: int
) -> Result[str]:
    try:
        return Result.ok(
            await DocumentVersionService(db, user_id).get_content(document_id, version_id)
        )
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc) from exc


@router.post(
    "/{document_id}/versions/{version_id}/rollback",
    response_model=Result[None],
    summary="回滚到指定版本",
)
async def rollback(
    db: DbSession, user_id: CurrentUserId, document_id: int, version_id: int
) -> Result[None]:
    try:
        await DocumentVersionService(db, user_id).rollback(document_id, version_id)
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc) from exc
    return Result.ok(None)
