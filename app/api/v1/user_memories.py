"""用户长期记忆 5 接口（对照 Java `UserMemoryController`）。

`memoryType` / `status` 的入参三口径都收（code / 枚举名 / 中文 desc），
未命中回落默认 —— 前端下拉、LLM 抽取、手工输入三种历史写法都在库里。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from app.api.deps import CurrentUserId, DbSession
from app.schemas.common import Result
from app.services.user_memory_service import UserMemoryService

router = APIRouter(prefix="/api/user/memories", tags=["user-memories"])


class MemoryIn(BaseModel):
    """Java `UserMemoryRequestDTO`：content 必填 ≤1000 字，其余可选。"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    content: str = Field(min_length=1, max_length=1000)
    memory_type: str | None = None
    keywords: list[str] | None = None
    confidence: float | None = None
    agent_id: str | None = None


class MemoryStatusIn(BaseModel):
    status: str | None = None


def _svc(db: DbSession, user_id: CurrentUserId) -> UserMemoryService:
    return UserMemoryService(db, user_id)


@router.get("", response_model=Result[list[dict]], summary="查询当前用户长期记忆")
async def list_memories(
    db: DbSession,
    user_id: CurrentUserId,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> Result[list[dict]]:
    return Result.ok(await _svc(db, user_id).list_memories(keyword, status))


@router.post("", response_model=Result[dict], summary="新增当前用户长期记忆")
async def create_memory(body: MemoryIn, db: DbSession, user_id: CurrentUserId) -> Result[dict]:
    try:
        out = await _svc(db, user_id).create(
            body.content, body.memory_type, body.keywords, body.confidence,
            int(body.agent_id) if body.agent_id else None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return Result.ok(out)


@router.put("/{memory_id}", response_model=Result[dict], summary="更新当前用户长期记忆")
async def update_memory(
    body: MemoryIn, db: DbSession, user_id: CurrentUserId, memory_id: int
) -> Result[dict]:
    try:
        out = await _svc(db, user_id).update(
            memory_id, body.content, body.memory_type, body.keywords, body.confidence,
            int(body.agent_id) if body.agent_id else None,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return Result.ok(out)


@router.put("/{memory_id}/status", response_model=Result[dict], summary="更新记忆状态")
async def update_status(
    body: MemoryStatusIn, db: DbSession, user_id: CurrentUserId, memory_id: int
) -> Result[dict]:
    try:
        return Result.ok(await _svc(db, user_id).update_status(memory_id, body.status))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/{memory_id}", response_model=Result[None], summary="删除当前用户长期记忆")
async def delete_memory(
    db: DbSession, user_id: CurrentUserId, memory_id: int
) -> Result[None]:
    try:
        await _svc(db, user_id).delete(memory_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return Result.ok(None)
