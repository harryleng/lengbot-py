"""Dashboard 聚合统计。

对照 Java `DashboardServiceImpl`：它只是编排层，真正查数在各域的 Port 实现里。
Python 没有 Port 抽象（各表实体都在同一个 `entities.py`），直接查即可。

两个必须照抄的行为：

1. **`/basic` 走 Redis 缓存**（Java `@Cacheable(CACHE_DASHBOARD, key='basic')`）。
   Python 这边没有接 Redis 缓存层，直接查库 —— 8 个 count 在 PG 上是毫秒级，
   不加缓存不会有问题，但如果以后数据量上来，这是第一个该补缓存的地方。
2. **趋势补齐空洞**：`messagesPerDay` 要把区间内没有消息的日期补成 0，
   否则前端折线图会把 3 号、4 号直接连起来，看着像"每天都在涨"。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import date, datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entities import (
    Agent,
    ChatSession,
    Chunk,
    Document,
    Knowledge,
    Message,
    ModelProvider,
    ModelRecord,
)
from app.schemas.misc import (
    AgentRecentItem,
    AgentStatusItem,
    DashboardAgent,
    DashboardBasic,
    DashboardChat,
    DashboardKnowledge,
    DocRecentItem,
    MessagePerDay,
)

logger = logging.getLogger(__name__)

MAX_TREND_DAYS = 90
DEFAULT_TREND_DAYS = 7

# Java AgentStatus：DRAFT/PUBLISHED/PUBLISHED_EDITING/ARCHIVED
AGENT_STATUSES: list[tuple[str, str]] = [
    ("draft", "草稿"),
    ("published", "已发布"),
    ("published_editing", "编辑中"),
    ("archived", "已归档"),
]

# Java DocumentStatus
DOC_STATUSES: list[str] = [
    "uploading",
    "uploaded",
    "pending",
    "processing",
    "completed",
    "failed",
]


class DashboardService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def _count(self, model: type, **filters: object) -> int:
        stmt = select(func.count()).select_from(model)
        # Java 侧这些实体的 `deleted` 由 MyBatis-Plus 逻辑删除自动带上；
        # Python 是手写 SQL，漏了就会把软删记录算进统计。
        if hasattr(model, "deleted"):
            stmt = stmt.where(model.deleted == 0)
        for key, value in filters.items():
            stmt = stmt.where(getattr(model, key) == value)
        result = await self.db.execute(stmt)
        return int(result.scalar() or 0)

    async def basic(self) -> DashboardBasic:
        return DashboardBasic(
            agent_count=await self._count(Agent),
            knowledge_count=await self._count(Knowledge),
            session_count=await self._count(ChatSession),
            message_count=await self._count(Message),
            provider_count=await self._count(ModelProvider),
            model_count=await self._count(ModelRecord),
            document_count=await self._count(Document),
            chunk_count=await self._count(Chunk),
        )

    async def agents(self) -> DashboardAgent:
        total = await self._count(Agent)
        status_list: list[AgentStatusItem] = []
        for code, label in AGENT_STATUSES:
            status_list.append(
                AgentStatusItem(code=code, label=label, count=await self._count(Agent, status=code))
            )
        status_counts = {item.code: item.count for item in status_list}

        stmt = (
            select(Agent)
            .where(or_(Agent.deleted == 0, Agent.deleted.is_(None)))
            .order_by(Agent.create_time.desc())
            .limit(5)
        )
        rows = (await self.db.execute(stmt)).scalars().all()
        recent = [
            AgentRecentItem(
                id=str(a.id),
                name=a.name,
                status=a.status or "draft",
                status_label=_status_label(a.status),
                create_time=a.create_time,
            )
            for a in rows
        ]
        return DashboardAgent(
            total=total,
            status_list=status_list,
            status_counts=status_counts,
            recent=recent,
        )

    async def knowledge(self) -> DashboardKnowledge:
        doc_status_counts = {
            code: await self._count(Document, status=code) for code in DOC_STATUSES
        }
        stmt = (
            select(Document)
            .where(or_(Document.deleted == 0, Document.deleted.is_(None)))
            .order_by(Document.create_time.desc())
            .limit(3)
        )
        rows = (await self.db.execute(stmt)).scalars().all()
        return DashboardKnowledge(
            total_knowledge=await self._count(Knowledge),
            total_documents=await self._count(Document),
            total_chunks=await self._count(Chunk),
            document_status_counts=doc_status_counts,
            recent_documents=[
                DocRecentItem(
                    id=str(d.id),
                    name=d.name,
                    knowledge_id=str(d.knowledge_id) if d.knowledge_id else None,
                    create_time=d.create_time,
                )
                for d in rows
            ],
        )

    async def chat(
        self,
        days: int | None,
        start_date: str | None,
        end_date: str | None,
    ) -> DashboardChat:
        trend_days: int | None = None
        if start_date and end_date:
            range_start, range_end = _parse_date(start_date), _parse_date(end_date)
            if range_end < range_start:
                range_start, range_end = range_end, range_start
        else:
            trend_days = min(days, MAX_TREND_DAYS) if days and days > 0 else DEFAULT_TREND_DAYS
            range_end = date.today()
            range_start = range_end - timedelta(days=trend_days - 1)

        span = (range_end - range_start).days + 1
        if span > MAX_TREND_DAYS:
            range_start = range_end - timedelta(days=MAX_TREND_DAYS - 1)

        stmt = (
            select(func.date(Message.create_time).label("d"), func.count())
            .where(func.date(Message.create_time) >= range_start)
            .where(func.date(Message.create_time) <= range_end)
            .group_by(func.date(Message.create_time))
        )
        rows: Sequence[tuple] = (await self.db.execute(stmt)).all()
        count_map = {str(row[0]): int(row[1]) for row in rows}

        per_day: list[MessagePerDay] = []
        cursor = range_start
        while cursor <= range_end:
            key = cursor.isoformat()
            per_day.append(MessagePerDay(date=key, count=count_map.get(key, 0)))
            cursor += timedelta(days=1)

        # 会话按 create_time 过滤，注意 Message 用的是 create_time，ChatSession 也是
        return DashboardChat(
            total_sessions=await self._date_range_count(ChatSession, range_start, range_end),
            total_messages=await self._date_range_count(Message, range_start, range_end),
            messages_per_day=per_day,
            trend_start_date=range_start.isoformat(),
            trend_end_date=range_end.isoformat(),
            trend_days=trend_days,
        )

    async def _date_range_count(self, model: type, start: date, end: date) -> int:
        col = model.create_time
        stmt = select(func.count()).select_from(model)
        if hasattr(model, "deleted"):
            stmt = stmt.where(model.deleted == 0)
        stmt = stmt.where(func.date(col) >= start).where(func.date(col) <= end)
        return int((await self.db.execute(stmt)).scalar() or 0)


def _status_label(status: str | None) -> str | None:
    for code, label in AGENT_STATUSES:
        if code == status:
            return label
    return "草稿"


def _parse_date(value: str) -> date:
    """Java 用 `LocalDate.parse` + ISO 格式，非法输入抛 IllegalArgumentException。

    这里统一转成 `ValueError`，由 API 层的异常处理器翻成 400。
    """
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"日期格式无效，应为 yyyy-MM-dd: {value}") from exc
