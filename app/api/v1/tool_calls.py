"""工具调用记录（对照 Java `ToolCallController`）。

`sessionId` 参数在 Java 里被当成 `messageId` 用（源码注释承认是简化），
前端就是这么传的，照做 —— 详见 `services/tool_call_service.py` 的说明。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.api.deps import DbSession
from app.schemas.common import Result
from app.schemas.misc import ToolCallPage
from app.services.tool_call_service import ToolCallService

router = APIRouter(prefix="/api/tool-calls", tags=["tool-calls"])


@router.get("", response_model=Result[ToolCallPage], summary="分页查询工具调用记录")
async def page_list(
    db: DbSession,
    page_num: int = Query(default=1, alias="pageNum", ge=1),
    page_size: int = Query(default=20, alias="pageSize", ge=1, le=200),
    tool_name: str | None = Query(default=None, alias="toolName"),
    status: str | None = Query(default=None),
    session_id: int | None = Query(default=None, alias="sessionId"),
    start_time: str | None = Query(default=None, alias="startTime"),
    end_time: str | None = Query(default=None, alias="endTime"),
) -> Result[ToolCallPage]:
    try:
        page = await ToolCallService(db).page_list(
            page_num=page_num,
            page_size=page_size,
            tool_name=tool_name,
            status=status,
            session_id=session_id,
            start_time=start_time,
            end_time=end_time,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return Result.ok(page)


@router.delete("", response_model=Result[None], summary="批量删除工具调用记录")
async def delete_calls(db: DbSession, ids: list[int]) -> Result[None]:
    await ToolCallService(db).delete(ids)
    return Result.ok(None)
