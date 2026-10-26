"""会话文件树 4 接口（对照 Java `ChatSessionController` 的 `/files*` 段）。

所有端点先过会话归属校验（Java `ensureOwnedByUser`，防会话 IDOR）；
路径非法（`..`）/不在分区白名单统一 400。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.api.deps import CurrentUserId, DbSession
from app.schemas.common import Result
from app.services.session_file_service import SessionFileService

router = APIRouter(prefix="/api/chat/sessions", tags=["session-files"])


async def _svc(db: DbSession, user_id: CurrentUserId, session_id: int) -> SessionFileService:
    svc = SessionFileService(db, user_id)
    await svc._owned_session(session_id)
    return svc


def _translate(exc: Exception) -> HTTPException:
    if isinstance(exc, LookupError):
        return HTTPException(status_code=404, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


@router.get("/{session_id}/files/tree", response_model=Result[dict], summary="会话文件树（懒加载单层）")
async def file_tree(
    db: DbSession, user_id: CurrentUserId, session_id: int,
    path: str = Query(default=""),
) -> Result[dict]:
    try:
        return Result.ok(await (await _svc(db, user_id, session_id)).list_directory(session_id, path))
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc) from exc


@router.get("/{session_id}/files/content", response_model=Result[dict], summary="会话文件内容/预览")
async def file_content(
    db: DbSession, user_id: CurrentUserId, session_id: int,
    path: str = Query(...),
) -> Result[dict]:
    try:
        return Result.ok(await (await _svc(db, user_id, session_id)).read_content(session_id, path))
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc) from exc


@router.get("/{session_id}/files/download", response_model=Result[str], summary="会话文件下载 URL")
async def file_download(
    db: DbSession, user_id: CurrentUserId, session_id: int,
    path: str = Query(...),
) -> Result[str]:
    try:
        return Result.ok(await (await _svc(db, user_id, session_id)).get_download_url(session_id, path))
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc) from exc


@router.delete("/{session_id}/files", response_model=Result[None], summary="删除会话文件")
async def delete_file(
    db: DbSession, user_id: CurrentUserId, session_id: int,
    path: str = Query(...),
) -> Result[None]:
    try:
        await (await _svc(db, user_id, session_id)).delete_file(session_id, path)
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc) from exc
    return Result.ok(None)
