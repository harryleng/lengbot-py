"""Agent 扩展 4 接口（对照 Java `AgentController` 的 generate-prompt /
generate-questions / avatar / publish）。归属校验与不存在同响应防 ID 枚举。
"""

from __future__ import annotations

from fastapi import APIRouter, File, HTTPException, UploadFile

from app.api.deps import CurrentUserId, DbSession
from app.schemas.common import CamelModel, Result
from app.services.agent_ext_service import AgentExtService

router = APIRouter(prefix="/api/agents", tags=["agent-ext"])


class AgentPublishIn(CamelModel):
    description: str | None = None


def _translate(exc: Exception) -> HTTPException:
    if isinstance(exc, LookupError):
        return HTTPException(status_code=404, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


@router.post("/{agent_id}/generate-prompt", response_model=Result[str], summary="AI 生成系统提示词")
async def generate_prompt(
    db: DbSession, user_id: CurrentUserId, agent_id: int
) -> Result[str]:
    try:
        return Result.ok(await AgentExtService(db, user_id).generate_system_prompt(agent_id))
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc) from exc


@router.post(
    "/{agent_id}/generate-questions",
    response_model=Result[str],
    summary="AI 生成推荐问题（JSON 数组字符串）",
)
async def generate_questions(
    db: DbSession, user_id: CurrentUserId, agent_id: int
) -> Result[str]:
    try:
        return Result.ok(
            await AgentExtService(db, user_id).generate_recommended_questions(agent_id)
        )
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc) from exc


@router.post("/{agent_id}/avatar", response_model=Result[str], summary="上传 Agent 头像")
async def upload_avatar(
    db: DbSession,
    user_id: CurrentUserId,
    agent_id: int,
    file: UploadFile = File(...),
) -> Result[str]:
    data = await file.read()
    try:
        url = await AgentExtService(db, user_id).upload_avatar(
            agent_id, file.filename or "", data
        )
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc) from exc
    return Result.ok(url)


@router.post("/{agent_id}/publish", response_model=Result[dict], summary="发布 Agent")
async def publish(
    body: AgentPublishIn | None,
    db: DbSession,
    user_id: CurrentUserId,
    agent_id: int,
) -> Result[dict]:
    description = body.description if body else None
    try:
        return Result.ok(await AgentExtService(db, user_id).publish(agent_id, description))
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc) from exc
