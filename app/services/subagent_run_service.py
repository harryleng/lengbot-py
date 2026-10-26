"""子代理运行记录：查询 + 取消标记（只读为主）。

对应 Java `SubAgentTaskServiceImpl` 里**不涉及真正执行**的那批方法。

**Python 版没有子代理执行器**（`delegate` 工具没实现），所以这些接口
读的是 Java 侧留下的历史数据。这不影响它们有用：前端会话侧栏的
"子智能体协作"面板就靠这批接口渲染。

### 三个必须照抄的契约细节

1. **返回字段是 snake_case，不是驼峰。**
   Java 这几处返回的是 `Map.of("task_id", ...)` 手拼的 Map，Jackson 不会
   转驼峰。所以这里是 `task_id` / `batch_id` / `next_cursor` 而不是 `taskId`。
   唯一例外是 `GET /runs`（返回 `Page<SubAgentRun>` 实体，走驼峰）。

2. **`taskId` 是 `subagent_run.request_id`，不是主键 `id`。**
   拿主键去查必然 404。

3. **取消只是写标记。** Python 侧没有消费者（Java 的执行器会读
   `cancel_requested` 然后自己停），所以这里如实把 `affected` 返回给前端 ——
   写着"已取消"但任务其实还在跑，比报错更难排查。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entities import (
    ChatSession,
    SubAgent,
    SubAgentRun,
    SubAgentTaskBatch,
    SubAgentTaskEvent,
)

logger = logging.getLogger(__name__)

# 与 Java `statusLabel` 一致
STATUS_LABELS = {
    "pending": "等待调度",
    "submitted": "等待调度",
    "running": "运行中",
    "completed": "已完成",
    "failed": "执行失败",
    "cancel_requested": "取消中",
    "cancelled": "已取消",
}

# 与 Java `latestSummary` 一致：状态没有终态时，用最新事件类型猜一句人话
EVENT_SUMMARIES = {
    "subagent_task_start": "正在执行",
    "subagent_tool_call": "正在调用工具",
    "subagent_tool_result": "工具执行完成",
    "subagent_task_done": "任务已完成",
    "subagent_error": "任务执行异常",
    "subagent_error_retry": "正在重试",
}


def _forbidden(msg: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=msg)


class SubAgentRunService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # 权限：会话必须属于当前用户
    # ------------------------------------------------------------------
    async def _ensure_session_owned(self, session_id: int, user_id: int) -> None:
        """对齐 Java `chatSessionService.ensureOwnedByUser`。

        为什么每个 runs 接口都要校验：`sessionId` 是前端传的 query 参数，
        换个 id 就能看到别人的子代理任务内容和回复 —— 这是实打实的越权。
        """
        row = (
            await self.db.execute(
                select(ChatSession.id).where(
                    ChatSession.id == session_id,
                    ChatSession.user_id == user_id,
                    ChatSession.deleted == 0,
                )
            )
        ).first()
        if not row:
            raise _forbidden("会话不存在或无权访问")

    # ------------------------------------------------------------------
    # 列表
    # ------------------------------------------------------------------
    async def page_runs(
        self,
        session_id: int,
        user_id: int,
        batch_id: str | None,
        parent_request_id: str | None,
        page_num: int,
        page_size: int,
    ) -> tuple[list[dict[str, Any]], int]:
        await self._ensure_session_owned(session_id, user_id)
        size = min(max(page_size, 1), 100)

        conditions = [SubAgentRun.parent_session_id == session_id]
        if batch_id and batch_id.strip():
            conditions.append(SubAgentRun.batch_id == batch_id.strip())
        if parent_request_id and parent_request_id.strip():
            conditions.append(SubAgentRun.parent_request_id == parent_request_id.strip())

        total = int(
            (
                await self.db.execute(
                    select(func.count()).select_from(SubAgentRun).where(*conditions)
                )
            ).scalar()
            or 0
        )
        rows = (
            await self.db.execute(
                select(SubAgentRun)
                .where(*conditions)
                .order_by(SubAgentRun.create_time.desc().nulls_last(), SubAgentRun.id.desc())
                .offset((max(page_num, 1) - 1) * size)
                .limit(size)
            )
        ).scalars().all()
        return [self._run_to_camel(r) for r in rows], total

    async def list_summaries(
        self, session_id: int, user_id: int, parent_request_id: str | None, limit: int
    ) -> list[dict[str, Any]]:
        """会话侧栏的紧凑视图：字段是 `taskResult` 那套（snake_case）。"""
        await self._ensure_session_owned(session_id, user_id)
        size = min(max(limit, 1), 100)

        conditions = [SubAgentRun.parent_session_id == session_id]
        if parent_request_id and parent_request_id.strip():
            conditions.append(SubAgentRun.parent_request_id == parent_request_id.strip())

        rows = (
            await self.db.execute(
                select(SubAgentRun)
                .where(*conditions)
                .order_by(SubAgentRun.create_time.desc().nulls_last(), SubAgentRun.id.desc())
                .limit(size)
            )
        ).scalars().all()

        names = await self._display_names()
        return [self._task_result(r, names) for r in rows]

    # ------------------------------------------------------------------
    # 详情
    # ------------------------------------------------------------------
    async def _find_task(self, task_id: str, session_id: int) -> SubAgentRun:
        row = (
            await self.db.execute(
                select(SubAgentRun).where(
                    SubAgentRun.request_id == task_id,
                    SubAgentRun.parent_session_id == session_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise _forbidden("SubAgent 任务不存在或无权访问")
        return row

    async def get_task(self, task_id: str, session_id: int, user_id: int) -> dict[str, Any]:
        await self._ensure_session_owned(session_id, user_id)
        row = await self._find_task(task_id, session_id)
        return self._task_result(row, await self._display_names())

    async def get_task_thread(
        self, task_id: str, session_id: int, user_id: int
    ) -> dict[str, Any]:
        """任务对应的独立子线程。

        Java 从 `threadManager` 取内存里的消息快照。Python 没有子代理执行器，
        那些线程根本不存在 —— 所以 `available` 恒为 **False**、`messages` 是空数组。
        这比返回一堆假消息诚实：前端看到 `available=false` 会显示"线程已释放"。
        """
        await self._ensure_session_owned(session_id, user_id)
        row = await self._find_task(task_id, session_id)
        return {
            "task": self._task_result(row, await self._display_names()),
            "thread_id": row.thread_id,
            "available": False,
            "messages": [],
        }

    async def get_task_events(
        self,
        task_id: str,
        session_id: int,
        user_id: int,
        cursor: int | None,
        limit: int,
    ) -> dict[str, Any]:
        """按游标增量拉事件。游标就是事件表自增主键（单调递增，适合做游标）。"""
        await self._ensure_session_owned(session_id, user_id)
        await self._find_task(task_id, session_id)

        size = min(max(limit, 1), 200)
        conditions = [SubAgentTaskEvent.task_id == task_id]
        if cursor is not None:
            conditions.append(SubAgentTaskEvent.id > cursor)

        rows = (
            await self.db.execute(
                select(SubAgentTaskEvent)
                .where(*conditions)
                .order_by(SubAgentTaskEvent.id.asc())
                .limit(size)
            )
        ).scalars().all()

        items = [
            {
                "cursor": str(e.id),
                "type": e.event_type,
                "payload": e.payload or {},
                "create_time": e.create_time,
            }
            for e in rows
        ]
        next_cursor = (
            str(rows[-1].id) if rows else (str(cursor) if cursor is not None else "")
        )
        return {"task_id": task_id, "events": items, "next_cursor": next_cursor}

    async def get_batch(
        self, batch_id: str, session_id: int, user_id: int
    ) -> dict[str, Any]:
        await self._ensure_session_owned(session_id, user_id)
        batch = (
            await self.db.execute(
                select(SubAgentTaskBatch).where(
                    SubAgentTaskBatch.batch_id == batch_id,
                    SubAgentTaskBatch.parent_session_id == session_id,
                )
            )
        ).scalar_one_or_none()
        if batch is None:
            # Java 这里返回 status=not_found 而不是抛错（batchResult(null)）
            return {"status": "not_found", "results": []}

        names = await self._display_names()
        tasks = (
            await self.db.execute(
                select(SubAgentRun)
                .where(SubAgentRun.batch_id == batch_id)
                .order_by(SubAgentRun.create_time.asc().nulls_last(), SubAgentRun.id.asc())
            )
        ).scalars().all()

        return {
            "batch_id": batch.batch_id,
            "status": batch.status,
            "mode": batch.mode,
            "aggregation": batch.aggregation,
            "total_count": batch.total_count,
            "completed_count": batch.completed_count,
            "failed_count": batch.failed_count,
            "cancelled_count": batch.cancelled_count,
            "cancel_requested": batch.cancel_requested == 1,
            "results": [self._task_result(t, names) for t in tasks],
        }

    # ------------------------------------------------------------------
    # 取消（只写标记）
    # ------------------------------------------------------------------
    async def cancel_task(self, task_id: str, session_id: int, user_id: int) -> dict[str, Any]:
        await self._ensure_session_owned(session_id, user_id)
        await self._find_task(task_id, session_id)
        # 只对**未终结**的任务写标记：已完成的任务标成"取消中"会让前端显示错状态
        result = await self.db.execute(
            update(SubAgentRun)
            .where(
                SubAgentRun.request_id == task_id,
                SubAgentRun.parent_session_id == session_id,
                SubAgentRun.status.in_(("pending", "submitted", "running")),
            )
            .values(cancel_requested=1, status="cancel_requested", update_time=datetime.now())
        )
        affected = int(result.rowcount or 0)
        await self.db.commit()
        logger.info("请求取消子代理任务 %s，影响 %d 行", task_id, affected)
        return {
            "task_id": task_id,
            "status": "cancel_requested" if affected else "not_found",
            "affected": affected,
        }

    async def cancel_batch(self, batch_id: str, session_id: int, user_id: int) -> dict[str, Any]:
        await self._ensure_session_owned(session_id, user_id)
        result = await self.db.execute(
            update(SubAgentRun)
            .where(
                SubAgentRun.batch_id == batch_id,
                SubAgentRun.parent_session_id == session_id,
                SubAgentRun.status.in_(("pending", "submitted", "running")),
            )
            .values(cancel_requested=1, status="cancel_requested", update_time=datetime.now())
        )
        affected = int(result.rowcount or 0)
        await self.db.execute(
            update(SubAgentTaskBatch)
            .where(
                SubAgentTaskBatch.batch_id == batch_id,
                SubAgentTaskBatch.parent_session_id == session_id,
            )
            .values(cancel_requested=1, update_time=datetime.now())
        )
        await self.db.commit()
        logger.info("请求取消子代理批次 %s，影响 %d 个任务", batch_id, affected)
        return {
            "batch_id": batch_id,
            "status": "cancel_requested" if affected else "not_found",
            "affected": affected,
        }

    # ------------------------------------------------------------------
    # 组装
    # ------------------------------------------------------------------
    async def _display_names(self) -> dict[str, str]:
        rows = (
            await self.db.execute(
                select(SubAgent.name, SubAgent.display_name).where(SubAgent.deleted == 0)
            )
        ).all()
        return {
            name: (display or name) for name, display in rows if name
        }

    async def _latest_summary(self, request_id: str | None) -> str | None:
        if not request_id:
            return None
        row = (
            await self.db.execute(
                select(SubAgentTaskEvent.event_type)
                .where(SubAgentTaskEvent.task_id == request_id)
                .order_by(SubAgentTaskEvent.id.desc())
                .limit(1)
            )
        ).scalar()
        return EVENT_SUMMARIES.get(str(row or ""), "已更新运行状态" if row else None)

    @staticmethod
    def _run_to_camel(row: SubAgentRun) -> dict[str, Any]:
        """`GET /runs` 返回的是实体分页，前端按**驼峰**读（对齐 MyBatis-Plus）。"""
        return {
            "id": str(row.id),
            "threadId": row.thread_id,
            "parentThreadId": row.parent_thread_id,
            "subagentName": row.subagent_name,
            "task": row.task,
            "status": row.status,
            "requestId": row.request_id,
            "batchId": row.batch_id,
            "parentRequestId": row.parent_request_id,
            "parentSessionId": str(row.parent_session_id) if row.parent_session_id else None,
            "mode": row.mode,
            "cancelRequested": row.cancel_requested == 1,
            "reply": row.reply,
            "toolCallCount": row.tool_call_count,
            "startTime": row.start_time,
            "endTime": row.end_time,
            "errorMessage": row.error_message,
            "createTime": row.create_time,
            "updateTime": row.update_time,
        }

    def _task_result(self, row: SubAgentRun, names: dict[str, str]) -> dict[str, Any]:
        """对齐 Java `taskResult`：**snake_case**，字段名逐个抄。"""
        return {
            "task_id": row.request_id,
            "batch_id": row.batch_id,
            "status": row.status,
            "status_label": STATUS_LABELS.get(str(row.status or ""), "未知状态"),
            "subagent_name": row.subagent_name,
            "display_name": names.get(str(row.subagent_name or ""), row.subagent_name),
            "task": row.task,
            "thread_id": row.thread_id,
            "mode": row.mode,
            "reply": row.reply,
            "error": row.error_message,
            "cancel_requested": row.cancel_requested == 1,
            "start_time": row.start_time,
            "end_time": row.end_time,
            "update_time": row.update_time,
            "progress_summary": self._sync_progress(row),
        }

    @staticmethod
    def _sync_progress(row: SubAgentRun) -> str:
        """状态能直接翻译就用固定文案；否则留给调用方补事件摘要。

        这里是**同步**版本（`_task_result` 不能 await）。`running` 状态下
        前端最想看的是"正在调用工具"这类实时摘要，可以用
        `list_summaries_async` 后再覆盖 —— 目前先给"运行中"，
        宁可少一句也不阻塞整个列表。
        """
        status_value = str(row.status or "")
        if status_value in ("pending", "submitted"):
            return "等待调度"
        if status_value == "completed":
            return "任务已完成"
        if status_value == "failed":
            return "任务执行失败"
        if status_value == "cancel_requested":
            return "正在取消任务"
        if status_value == "cancelled":
            return "任务已取消"
        return "已更新运行状态"
