"""任务中心（对照 Java `TaskController` + `TaskEventController` 的 HTTP 兜底）。

两个必须照抄的口径：

1. **`type` 存的是 TaskType 的 code（小写下划线）**，如 `rag_evaluation`；
   但 `type-counts` 的分组键是 **desc（中文）**——"RAG评估"。
   前端徽标按中文分组渲染，别统一成 code。
2. **`status=active` 是聚合筛选**：不是真实状态值，查 `pending/pending_retry/running`
   三种未完结状态。传其它值才是精确匹配。

Python 侧没有 Redis 进度快照（Java `TaskQueueService.getProgress`），
`/progress` 直接读 task 行的 progress/message —— 进度粒度到秒级，够用；
真要毫秒级再说。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entities import TaskRecord

# 与 Java TaskType 对齐的 code → desc（type-counts 的分组键）
TASK_TYPE_DESC: dict[str, str] = {
    "document_upload": "文档上传",
    "document_ingest": "文档入库",
    "experiment_run": "实验执行",
    "benchmark_generate": "基准生成",
    "benchmark_import": "基准导入",
    "rag_evaluation": "RAG评估",
    "graph_extraction": "图谱抽取",
    "qa_pair_generate": "问答对生成",
    "community_detect": "社区检测",
    "community_summary": "社区摘要",
}

# 未完结状态（Java TaskStatus.PENDING / PENDING_RETRY / RUNNING）
ACTIVE_STATUSES = ("pending", "pending_retry", "running")
# 终态（允许删除的）
TERMINAL_STATUSES = ("success", "failed", "cancelled")


def task_out(row: TaskRecord) -> dict:
    """Task 实体直出的 camelCase 形态（`generate`/`run` 复用）。"""
    payload = row.payload
    if isinstance(payload, str) and payload.strip():
        import json

        try:
            parsed = json.loads(payload)
            payload = parsed if isinstance(parsed, dict) else payload
        except (json.JSONDecodeError, TypeError):
            pass
    return {
        "id": str(row.id),
        "name": row.name,
        "type": row.type,
        "status": row.status,
        "progress": row.progress,
        "message": row.message,
        "payload": payload,
        "result": row.result,
        "error": row.error,
        "userId": str(row.user_id) if row.user_id else None,
        "refId": str(row.ref_id) if row.ref_id else None,
        "createTime": row.create_time,
        "updateTime": row.update_time,
        "startedAt": row.started_at,
        "completedAt": row.completed_at,
    }


class TaskService:
    def __init__(self, db: AsyncSession, user_id: int) -> None:
        self.db = db
        self.user_id = user_id

    async def list_page(
        self,
        page_num: int,
        page_size: int,
        name: str | None,
        status: str | None,
        type_: str | None,
    ) -> tuple[list[TaskRecord], int]:
        stmt = select(TaskRecord).where(TaskRecord.user_id == self.user_id)
        if name:
            stmt = stmt.where(TaskRecord.name.like(f"%{name}%"))
        if status:
            if status == "active":
                stmt = stmt.where(TaskRecord.status.in_(ACTIVE_STATUSES))
            else:
                stmt = stmt.where(TaskRecord.status == status)
        if type_:
            # 历史数据里 type 大小写混过（EXPERIMENT_RUN），lower 比对兜底
            stmt = stmt.where(func.lower(TaskRecord.type) == type_.lower())
        stmt = stmt.order_by(TaskRecord.create_time.desc())

        count_stmt = select(func.count()).select_from(stmt.subquery())
        total = int((await self.db.execute(count_stmt)).scalar() or 0)
        rows = (
            (await self.db.execute(stmt.offset((page_num - 1) * page_size).limit(page_size)))
            .scalars()
            .all()
        )
        return list(rows), total

    async def get_owned(self, task_id: int) -> TaskRecord:
        return await self._get_owned(task_id)

    async def _get_owned(self, task_id: int) -> TaskRecord:
        """归属校验：不是自己的任务按 404 处理（防 ID 枚举，Java `getTaskById` 同）。"""
        row = await self.db.get(TaskRecord, task_id)
        if row is None or row.user_id != self.user_id:
            raise LookupError("任务不存在")
        return row

    async def running_count(self) -> int:
        stmt = (
            select(func.count())
            .select_from(TaskRecord)
            .where(TaskRecord.user_id == self.user_id)
            .where(TaskRecord.status.in_(ACTIVE_STATUSES))
        )
        return int((await self.db.execute(stmt)).scalar() or 0)

    async def counts(self) -> dict[str, int]:
        """`/count`：SSE 的 HTTP 兜底，导航角标数据源。"""
        stmt = (
            select(TaskRecord.status, func.count())
            .where(TaskRecord.user_id == self.user_id)
            .where(TaskRecord.status.in_(ACTIVE_STATUSES))
            .group_by(TaskRecord.status)
        )
        rows = (await self.db.execute(stmt)).all()
        by_status = {r[0]: int(r[1]) for r in rows}
        pending = by_status.get("pending", 0)
        running = by_status.get("running", 0)
        pending_retry = by_status.get("pending_retry", 0)
        return {
            "active": pending + running + pending_retry,
            "pending": pending,
            "running": running,
            "pendingRetry": pending_retry,
        }

    async def type_counts(self) -> dict[str, int]:
        """进行中任务按 **type 的中文 desc** 分组（前端按中文渲染徽标）。"""
        stmt = (
            select(TaskRecord)
            .where(TaskRecord.user_id == self.user_id)
            .where(TaskRecord.status.in_(ACTIVE_STATUSES))
        )
        rows = (await self.db.execute(stmt)).scalars().all()
        counts: dict[str, int] = {}
        for r in rows:
            label = TASK_TYPE_DESC.get((r.type or "").lower(), r.type or "unknown")
            counts[label] = counts.get(label, 0) + 1
        return counts

    async def progress(self, task_id: int) -> dict:
        row = await self._get_owned(task_id)
        # Java 返回 Redis 快照；Python 用 task 行兜底，ts 用 update_time 毫秒
        ts = int(row.update_time.timestamp() * 1000) if row.update_time else 0
        return {
            "progress": row.progress or 0,
            "message": row.message,
            "ts": ts,
        }

    async def cancel(self, task_id: int) -> bool:
        """置 cancel_requested=1（执行体轮询检查）。
        已终态的任务返回 False，API 层翻成 code=61002。"""
        row = await self._get_owned(task_id)
        if row.status not in ACTIVE_STATUSES:
            return False
        row.cancel_requested = 1
        row.update_time = datetime.now()
        await self.db.commit()
        return True

    async def delete(self, task_id: int) -> None:
        row = await self._get_owned(task_id)
        # 仅终态可删：运行中删掉会让执行体的进度更新变成"写给不存在的行"
        if row.status not in TERMINAL_STATUSES:
            raise PermissionError("运行中的任务不能删除")
        await self.db.execute(sa_delete(TaskRecord).where(TaskRecord.id == task_id))
        await self.db.commit()
