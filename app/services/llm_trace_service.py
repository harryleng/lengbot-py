"""LLM 调用链查询（`/api/observability/*`，对照 Java `LlmTraceServiceImpl`）。

两个容易写错的点：

1. **默认来源过滤**：未指定 `traceSource` 时，Java 只取 `chat` / `workflow` / NULL，
   把 subagent 等辅助调用排除。Python 照做，否则列表会被内部调用刷屏。
2. **spans 解析失败给空列表**，不要抛错。这是一张"审计日志表"，
   一条脏数据不该让整条链路详情打不开。
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entities import LlmTrace
from app.schemas.misc import LlmTraceDetail, LlmTraceOut, TraceOverview, TracePage

logger = logging.getLogger(__name__)

DEFAULT_SOURCES = ("chat", "workflow")


class LlmTraceService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def page_list(
        self,
        *,
        page_num: int = 1,
        page_size: int = 20,
        session_id: int | None = None,
        request_id: str | None = None,
        agent_id: int | None = None,
        status: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
        trace_source: str | None = None,
    ) -> TracePage:
        stmt = select(LlmTrace)
        if status:
            stmt = stmt.where(LlmTrace.status == status)
        if session_id is not None:
            stmt = stmt.where(LlmTrace.session_id == session_id)
        if request_id:
            stmt = stmt.where(LlmTrace.request_id == request_id)
        if agent_id is not None:
            stmt = stmt.where(LlmTrace.agent_id == agent_id)
        if start_time:
            stmt = stmt.where(LlmTrace.create_time >= _parse_dt(start_time))
        if end_time:
            stmt = stmt.where(LlmTrace.create_time <= _parse_dt(end_time))
        stmt = stmt.where(_source_filter(trace_source))
        stmt = stmt.order_by(LlmTrace.create_time.desc())

        total = int(
            (
                await self.db.execute(
                    select(func.count()).select_from(stmt.subquery())
                )
            ).scalar()
            or 0
        )
        rows = (
            (await self.db.execute(stmt.offset((page_num - 1) * page_size).limit(page_size)))
            .scalars()
            .all()
        )
        return TracePage(
            records=[_to_out(r) for r in rows],
            total=total,
            page_num=page_num,
            page_size=page_size,
        )

    async def detail(self, trace_id: int) -> LlmTraceDetail:
        row = await self.db.get(LlmTrace, trace_id)
        if row is None:
            raise LookupError("调用链不存在")
        out = LlmTraceDetail(**_to_out(row).model_dump(by_alias=False), spans=[])
        spans = row.spans
        if isinstance(spans, list):
            out.spans = spans
        elif isinstance(spans, str):
            try:
                import json

                parsed = json.loads(spans)
                out.spans = parsed if isinstance(parsed, list) else []
            except Exception:  # noqa: BLE001
                logger.warning("[LlmTrace] spans 解析失败 id=%s", trace_id)
        return out

    async def overview(self, trace_source: str | None) -> TraceOverview:
        base = select(LlmTrace).where(_source_filter(trace_source))
        stmt = (
            select(
                func.count(),
                func.coalesce(func.sum(LlmTrace.total_tokens), 0),
                func.coalesce(func.avg(LlmTrace.total_duration_ms), 0),
                func.coalesce(func.sum(LlmTrace.tool_call_count), 0),
                func.coalesce(func.avg(LlmTrace.first_token_ms), 0),
                func.coalesce(func.max(LlmTrace.first_token_ms), 0),
            ).select_from(base.subquery())
        )
        row = (await self.db.execute(stmt)).one()
        total_count = int(row[0] or 0)
        success = int(
            (
                await self.db.execute(
                    select(func.count())
                    .select_from(LlmTrace)
                    .where(_source_filter(trace_source))
                    .where(LlmTrace.status == "completed")
                )
            ).scalar()
            or 0
        )
        failed = int(
            (
                await self.db.execute(
                    select(func.count())
                    .select_from(LlmTrace)
                    .where(_source_filter(trace_source))
                    .where(LlmTrace.status == "failed")
                )
            ).scalar()
            or 0
        )
        return TraceOverview(
            total_count=total_count,
            success_count=success,
            failed_count=failed,
            total_tokens=int(row[1] or 0),
            avg_duration_ms=int(row[2] or 0) if total_count else 0,
            total_tool_calls=int(row[3] or 0),
            # Java 只在有记录时才返回这两个 key，避免 0 被读成"0ms 响应"
            avg_first_token_ms=int(row[4] or 0) if total_count else None,
            max_first_token_ms=int(row[5] or 0) if total_count else None,
        )

    async def delete(self, ids: list[int]) -> None:
        if not ids:
            return
        for trace_id in ids:
            row = await self.db.get(LlmTrace, trace_id)
            if row is not None:
                await self.db.delete(row)
        await self.db.commit()


def _source_filter(trace_source: str | None):
    if trace_source:
        return LlmTrace.trace_source == trace_source
    return or_(
        LlmTrace.trace_source.in_(DEFAULT_SOURCES),
        LlmTrace.trace_source.is_(None),
    )


def _parse_dt(value: str) -> datetime:
    return datetime.strptime(value.strip(), "%Y-%m-%d %H:%M:%S")


def _to_out(row: LlmTrace) -> LlmTraceOut:
    return LlmTraceOut(
        id=str(row.id),
        request_id=row.request_id,
        session_id=str(row.session_id) if row.session_id else None,
        user_id=str(row.user_id) if row.user_id else None,
        agent_id=str(row.agent_id) if row.agent_id else None,
        agent_name=row.agent_name,
        model=row.model,
        status=row.status,
        input_tokens=row.input_tokens,
        output_tokens=row.output_tokens,
        total_tokens=row.total_tokens,
        tool_call_count=row.tool_call_count,
        total_duration_ms=row.total_duration_ms,
        first_token_ms=row.first_token_ms,
        prompt_cache_hit_tokens=row.prompt_cache_hit_tokens,
        error_message=row.error_message,
        reply_content=row.reply_content,
        display_content=row.display_content,
        trace_source=row.trace_source,
        create_time=row.create_time,
    )
