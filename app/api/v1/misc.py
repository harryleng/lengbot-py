"""零碎模块 API：模型管理 4 + 会话待办 2 + 对话附件 3。

路由前缀交错（models / sessions/{id}/todos / chat/attachments），
用一个 router 分段声明，prefix 只取第一段。
"""

from __future__ import annotations

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile

from app.api.deps import CurrentUserId, DbSession
from app.models.entities import ChatSession
from app.schemas.common import Result
from app.schemas.model import ModelCreateIn
from app.services.chat_attachment_service import ChatAttachmentService
from app.services.model_service import ModelService
from app.services.session_todo_service import SessionTodoService

router = APIRouter(tags=["misc"])


def _translate(exc: Exception) -> HTTPException:
    if isinstance(exc, LookupError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, PermissionError):
        return HTTPException(status_code=403, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


# ==================== 模型管理 /api/models ====================
@router.get("/api/models/by-provider/{provider_id}", response_model=Result[list[dict]],
            summary="按供应商列模型")
async def models_by_provider(db: DbSession, provider_id: int) -> Result[list[dict]]:
    return Result.ok(await ModelService(db).list_by_provider(provider_id))


@router.get("/api/models/by-type/{type_name}", response_model=Result[list[dict]],
            summary="按类型列模型（仅 active）")
async def models_by_type(db: DbSession, type_name: str) -> Result[list[dict]]:
    return Result.ok(await ModelService(db).list_by_type(type_name))


@router.post("/api/models", response_model=Result[dict], summary="创建模型")
async def create_model(body: ModelCreateIn, db: DbSession) -> Result[dict]:
    try:
        out = await ModelService(db).create(
            int(body.provider_id), body.model_id, body.name, body.type, body.dimension
        )
    except ValueError as exc:
        raise _translate(exc) from exc
    return Result.ok(out)


@router.delete("/api/models/{model_row_id}", response_model=Result[None], summary="删除模型")
async def delete_model(db: DbSession, model_row_id: int) -> Result[None]:
    try:
        await ModelService(db).delete(model_row_id)
    except LookupError as exc:
        raise _translate(exc) from exc
    return Result.ok(None)


# ==================== 会话待办 /api/sessions/{id}/todos ====================
async def _owned_session(db: DbSession, session_id: int, user_id: int) -> None:
    """归属校验（Java ensureOwnedByUser）：不是自己的会话按 404 防 ID 枚举。"""
    row = await db.get(ChatSession, session_id)
    if row is None or row.deleted or row.user_id != user_id:
        raise HTTPException(status_code=404, detail="会话不存在")


@router.get("/api/sessions/{session_id}/todos", response_model=Result[list[dict]],
            summary="按请求获取待办（流式期间轮询兜底）")
async def session_todos(
    db: DbSession, user_id: CurrentUserId, session_id: int,
    parent_request_id: str | None = Query(default=None, alias="parentRequestId"),
) -> Result[list[dict]]:
    await _owned_session(db, session_id, user_id)
    return Result.ok(await SessionTodoService(db).list_by_request(session_id, parent_request_id))


@router.get("/api/sessions/{session_id}/todos/latest", response_model=Result[list[dict]],
            summary="获取会话最新待办")
async def session_todos_latest(
    db: DbSession, user_id: CurrentUserId, session_id: int
) -> Result[list[dict]]:
    await _owned_session(db, session_id, user_id)
    return Result.ok(await SessionTodoService(db).list_latest_by_session(session_id))


# ==================== 对话附件 /api/chat/attachments ====================
@router.post("/api/chat/attachments", response_model=Result[dict], summary="上传对话附件")
async def upload_attachment(
    db: DbSession,
    user_id: CurrentUserId,
    agent_id: int = Form(..., alias="agentId"),
    session_id: int | None = Form(default=None, alias="sessionId"),
    file: UploadFile = File(...),
) -> Result[dict]:
    data = await file.read()
    svc = ChatAttachmentService(db, user_id)
    try:
        out = await svc.upload(agent_id, session_id, file.filename or "",
                               file.content_type or "", data)
    except LookupError as exc:
        raise _translate(exc) from exc
    except PermissionError as exc:
        raise _translate(exc) from exc
    except ValueError as exc:
        raise _translate(exc) from exc
    return Result.ok(out)


@router.post("/api/chat/attachments/refresh-preview", response_model=Result[list[dict]],
             summary="刷新对话附件预览 URL")
async def refresh_attachment_previews(
    attachments: list[dict], db: DbSession, user_id: CurrentUserId
) -> Result[list[dict]]:
    return Result.ok(await ChatAttachmentService(db, user_id).refresh_previews(attachments))


@router.delete("/api/chat/attachments", response_model=Result[None], summary="删除未发送的缓冲区附件")
async def delete_attachment(
    db: DbSession,
    user_id: CurrentUserId,
    agent_id: int = Query(..., alias="agentId"),
    session_id: int | None = Query(default=None, alias="sessionId"),
    attachment: dict = None,
) -> Result[None]:
    svc = ChatAttachmentService(db, user_id)
    try:
        await svc.delete(agent_id, session_id, attachment)
    except ValueError as exc:
        raise _translate(exc) from exc
    return Result.ok(None)
