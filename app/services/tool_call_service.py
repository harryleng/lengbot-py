"""工具调用记录查询（`/api/tool-calls`，对照 Java `ToolCallServiceImpl`）。

⚠️ 有一个 Java 的"历史包袱"要原样保留：

`sessionId` 参数实际被当成 `messageId` 用（Java 源码注释：
"通过 message_id 关联查询…这里简化为直接按 message_id 查询"）。
这不是笔误复现的需求，而是**前端就是这么传的** ——
改成真正的会话关联查询会让「按会话筛选工具调用」永远筛不出东西。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entities import ToolCall
from app.schemas.misc import ToolCallOut, ToolCallPage


class ToolCallService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def page_list(
        self,
        *,
        page_num: int = 1,
        page_size: int = 20,
        tool_name: str | None = None,
        status: str | None = None,
        session_id: int | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
    ) -> ToolCallPage:
        stmt = select(ToolCall)
        if tool_name:
            stmt = stmt.where(ToolCall.tool_name.like(f"%{tool_name}%"))
        if status:
            stmt = stmt.where(ToolCall.status == status)
        if session_id is not None:
            stmt = stmt.where(ToolCall.message_id == session_id)
        if start_time:
            stmt = stmt.where(ToolCall.created_at >= _parse_dt(start_time))
        if end_time:
            stmt = stmt.where(ToolCall.created_at <= _parse_dt(end_time))
        stmt = stmt.order_by(ToolCall.created_at.desc())

        total = int(
            (await self.db.execute(select(func.count()).select_from(stmt.subquery()))).scalar()
            or 0
        )
        rows = (
            (await self.db.execute(stmt.offset((page_num - 1) * page_size).limit(page_size)))
            .scalars()
            .all()
        )
        return ToolCallPage(
            records=[_to_out(r) for r in rows],
            total=total,
            page_num=page_num,
            page_size=page_size,
        )

    async def delete(self, ids: list[int]) -> None:
        """Java 是 `removeByIds` —— 物理删除（这张表没有 deleted 列，也没有软删需求）。"""
        if not ids:
            return
        for call_id in ids:
            row = await self.db.get(ToolCall, call_id)
            if row is not None:
                await self.db.delete(row)
        await self.db.commit()


def _parse_dt(value: str) -> datetime:
    """Java 直接把字符串丢给 SQL 比较；这里兼容 `yyyy-MM-dd` 和完整时间戳两种写法。"""
    text = value.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise ValueError(f"时间格式无效: {value}")


def _to_out(row: ToolCall) -> ToolCallOut:
    return ToolCallOut(
        id=str(row.id),
        message_id=str(row.message_id) if row.message_id else None,
        tool_name=row.tool_name,
        tool_input=row.tool_input,
        tool_output=row.tool_output,
        status=row.status,
        error_message=row.error_message,
        created_at=row.created_at,
    )
