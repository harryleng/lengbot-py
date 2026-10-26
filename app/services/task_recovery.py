"""异步任务持久化：启动恢复 + 僵尸收割（对照 Java `TaskConsumerConfig.recoverUnfinishedTasks`
+ `TaskZombieScheduler`）。

## 背景

Python 侧任务用 `asyncio.create_task` 进程内执行，**重启即丢**——task 行
永远停在 running，前端进度条卡死。Java 靠 Redis Stream 队列：启动时把
未完结任务重置 PENDING 重新入队，另有 60s 一轮的僵尸扫描兜底。

## Python 等价实现（无队列，语义对齐）

1. **启动恢复**：扫描 pending/pending_retry/running 的任务：
   - 类型在 `RUNNERS` 里（评测/基准/图谱/社区/实验）→ 重置 pending 后
     **重跑执行体**（Java 重新入队等价物）；
   - 不在映射里（document_upload 等依赖请求现场的任务）→ markFailed
     「服务重启，任务中断」（Java 能恢复是因为执行体全在队列侧；
     Python 的上传任务连文件字节都在请求里，恢复不了就大声失败）。
2. **僵尸收割**：每 60s 扫描 running 超 10min / pending 超 5min 的任务
   （阈值对齐 Java `TaskZombieProperties` 默认值），CAS 标记 failed。
   Java 用 Redis PEL 区分「还在跑」和「真死了」；Python 用进程内
   `ACTIVE_TASK_IDS` 心跳集合等价替代 —— 执行体启动时登记、结束时移除。

## 幂等性

markFailed 用状态 CAS（仅 running/pending → failed），重复扫描不会误改终态；
恢复重跑前先 CAS 抢占（pending → running 由执行体自身状态机完成），
重复触发恢复不会双跑。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

from sqlalchemy import select, update

from app.core.db import SessionLocal
from app.models.entities import TaskRecord

logger = logging.getLogger(__name__)

ACTIVE_STATUSES = ("pending", "pending_retry", "running")
RUNNING_TIMEOUT_SECONDS = 10 * 60   # Java defaultTimeoutMinutes=10
PENDING_TIMEOUT_SECONDS = 5 * 60    # Java pendingTimeoutMinutes=5
REAP_INTERVAL_SECONDS = 60          # Java intervalSeconds=60

# 进程内心跳：正在执行的 task_id 集合（PEL 的等价物）
ACTIVE_TASK_IDS: set[int] = set()

Runner = Callable[[int, dict], Awaitable[str]]


def _runners() -> dict[str, Callable]:
    """type → 恢复执行器。懒导入避免环。

    签名不齐（有 (task_id)、有 (task_id, payload)、有 (experiment_id, user_id)），
    用小适配函数抹平。
    """
    from app.services import eval_experiment_service, graph_executors, rag_eval_service

    async def run_experiment(task_id: int, payload: dict) -> str:
        experiment_id = int(payload.get("experimentId") or 0)
        user_id = 0
        async with SessionLocal() as db:
            row = await db.get(TaskRecord, task_id)
            if row is not None:
                user_id = row.user_id or 0
        return await eval_experiment_service._run_experiment(experiment_id, user_id)  # noqa: SLF001

    return {
        "rag_evaluation": lambda tid, _p: rag_eval_service._run_evaluation(tid),  # noqa: SLF001
        "benchmark_generate": lambda tid, _p: rag_eval_service._run_benchmark_generate(tid),  # noqa: SLF001
        "graph_extraction": graph_executors.run_graph_extraction,
        "community_detect": graph_executors.run_community_detect,
        "community_summary": graph_executors.run_community_summary,
        "experiment_run": run_experiment,
    }


async def _cas_finish(
    task_id: int, status: str, message: str | None, error: str | None
) -> bool:
    """终态 CAS：仅当任务仍处于未完结状态时写入。返回是否真的写了。"""
    async with SessionLocal() as db:
        result = await db.execute(
            update(TaskRecord)
            .where(TaskRecord.id == task_id)
            .where(TaskRecord.status.in_(ACTIVE_STATUSES))
            .values(
                status=status,
                message=(message or "")[:500],
                error=error[:1000] if error else None,
                progress=100 if status == "success" else TaskRecord.progress,
                completed_at=datetime.now(),
                update_time=datetime.now(),
            )
        )
        await db.commit()
        return (result.rowcount or 0) > 0


async def spawn_recovered_task(task_id: int, task_type: str, payload: dict) -> None:
    """恢复重跑：登记心跳 → 执行 → 心跳注销 + 终态落库。"""
    runner = _runners()[task_type]

    async def _runner() -> None:
        try:
            result = await runner(task_id, payload)
            await _cas_finish(task_id, "success", str(result)[:500], None)
            logger.info("[任务恢复] taskId=%s type=%s 重跑成功", task_id, task_type)
        except asyncio.CancelledError:
            await _cas_finish(task_id, "cancelled", "用户取消", None)
        except Exception as exc:  # noqa: BLE001
            logger.exception("[任务恢复] taskId=%s type=%s 重跑失败", task_id, task_type)
            await _cas_finish(task_id, "failed", str(exc)[:500], str(exc)[:1000])

    asyncio.create_task(tracked(task_id, _runner()))


async def recover_unfinished_tasks() -> dict[str, int]:
    """启动恢复。返回 {recovered, failed, skipped}。"""
    stats = {"recovered": 0, "failed": 0}
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(TaskRecord)
                .where(TaskRecord.status.in_(ACTIVE_STATUSES))
                .order_by(TaskRecord.create_time.asc())
            )
        ).scalars().all()

    if not rows:
        logger.info("[任务恢复] 启动时无未完结任务")
        return stats

    logger.warning("[任务恢复] 发现 %d 个未完结任务（重启遗留）", len(rows))
    runners = _runners()
    for row in rows:
        task_type = (row.type or "").lower()
        payload = row.payload if isinstance(row.payload, dict) else {}
        if task_type in runners:
            # 重置 pending 再重跑（执行体自己推进 running）
            async with SessionLocal() as db:
                await db.execute(
                    update(TaskRecord)
                    .where(TaskRecord.id == row.id)
                    .where(TaskRecord.status.in_(ACTIVE_STATUSES))
                    .values(status="pending", error=None, update_time=datetime.now())
                )
                await db.commit()
            try:
                await spawn_recovered_task(row.id, task_type, payload)
                stats["recovered"] += 1
                logger.info(
                    "[任务恢复] taskId=%s type=%s prev=%s 重跑已提交",
                    row.id, task_type, row.status,
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("[任务恢复] taskId=%s 重跑提交失败", row.id)
                await _cas_finish(row.id, "failed", None, str(exc))
                stats["failed"] += 1
        else:
            await _cas_finish(
                row.id, "failed", "服务重启，任务中断，请重新触发",
                f"任务类型 {task_type} 不支持自动恢复",
            )
            stats["failed"] += 1
            logger.warning(
                "[任务恢复] taskId=%s type=%s 无恢复执行器，已标记失败", row.id, task_type
            )
    return stats


async def reap_zombies_once() -> int:
    """僵尸收割一轮。返回强制失败数。"""
    now = datetime.now()
    running_deadline = now - timedelta(seconds=RUNNING_TIMEOUT_SECONDS)
    pending_deadline = now - timedelta(seconds=PENDING_TIMEOUT_SECONDS)

    async with SessionLocal() as db:
        candidates = (
            await db.execute(
                select(TaskRecord)
                .where(
                    TaskRecord.status.in_(ACTIVE_STATUSES),
                    TaskRecord.update_time.is_not(None),
                )
                .order_by(TaskRecord.update_time.asc())
                .limit(100)
            )
        ).scalars().all()

    killed = 0
    for row in candidates:
        if row.id in ACTIVE_TASK_IDS:
            continue  # 心跳还活着（PEL 等价判定）
        deadline = running_deadline if row.status == "running" else pending_deadline
        if row.update_time and row.update_time > deadline:
            continue
        ok = await _cas_finish(
            row.id, "failed", "任务超时无心跳，已被系统强制失败（僵尸回收）", None
        )
        if ok:
            killed += 1
            logger.warning(
                "[TaskZombie] taskId=%s type=%s status=%s update_time=%s 强制失败",
                row.id, row.type, row.status, row.update_time,
            )
    return killed


async def start_zombie_reaper() -> asyncio.Task:
    """启动后台收割循环（lifespan 持有句柄，shutdown 时取消）。"""

    async def _loop() -> None:
        while True:
            try:
                await reap_zombies_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("[TaskZombie] 扫描失败（本轮跳过）")
            await asyncio.sleep(REAP_INTERVAL_SECONDS)

    return asyncio.create_task(_loop(), name="task-zombie-reaper")


def tracked(task_id: int, coro) -> Awaitable:
    """给进程内执行体包一层心跳（add/discard ACTIVE_TASK_IDS）。

    所有「创建 TaskRecord 后 asyncio.create_task」的站点都应改用本包装，
    否则长静默任务（如 OCR 入库）会被僵尸收割误杀。
    """

    async def _wrapped() -> None:
        ACTIVE_TASK_IDS.add(task_id)
        try:
            await coro
        finally:
            ACTIVE_TASK_IDS.discard(task_id)

    return _wrapped()
