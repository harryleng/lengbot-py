"""日志查看（对照 Java `LogController`）。

只做 `GET /logs/recent` —— 覆盖率清单里也只有这一个。
Java 另有 `/logs/stream` 的 SSE 实时推送，Python 侧暂不实现：
前端日志页在拿不到流时会退化成轮询 recent，不会白屏。
"""

from __future__ import annotations

from fastapi import APIRouter, Query

from app.schemas.common import Result
from app.schemas.misc import LogEvent
from app.services import log_buffer

router = APIRouter(prefix="/api/logs", tags=["logs"])

# Java：`Math.min(limit, 2000)`
MAX_LIMIT = 2000


@router.get("/recent", response_model=Result[list[LogEvent]], summary="获取最近日志")
async def recent(
    limit: int = Query(default=200, ge=1, le=MAX_LIMIT),
) -> Result[list[LogEvent]]:
    return Result.ok(log_buffer.recent(limit))
