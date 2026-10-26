"""任务持久化验证（task_recovery_check）—— 假 runner，不触发真执行体。

直连 DB 测四块：

1. 僵尸收割：running 超 10min 无心跳 → 强制失败；fresh / 有心跳 / 终态 → 跳过；
   pending 超 5min → 强制失败（CAS 不误改终态）；
2. 启动恢复：可恢复类型（假 runner 接管）→ 重置 pending + 重跑成功；
   不可恢复类型 → markFailed；
3. tracked 心跳：执行期间 ACTIVE_TASK_IDS 含 id，结束后移除；
4. 幂等：重复 reap 不再动终态行。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from unittest.mock import patch

from sqlalchemy import text

from app.core.db import SessionLocal
from app.core.snowflake import new_id
from app.models.entities import TaskRecord
from app.services import task_recovery

_STAMP = str(int(datetime.now().timestamp()))
_UID = 2090273750973657090
_FAILED: list[str] = []


def ok(name: str, cond: bool, detail: str = "") -> None:
    mark = "✅" if cond else "❌"
    print(f"  {mark} {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        _FAILED.append(name)


async def seed(status: str, ttype: str, age_seconds: int, message: str = "") -> int:
    tid = new_id()
    now = datetime.now()
    async with SessionLocal() as db:
        db.add(
            TaskRecord(
                id=tid, name=f"zz-recovery-{_STAMP}", type=ttype, status=status,
                progress=0, payload={"knowledgeId": 1}, cancel_requested=0,
                user_id=_UID, ref_id=1, create_time=now,
                update_time=now - timedelta(seconds=age_seconds),
                started_at=now - timedelta(seconds=age_seconds),
            )
        )
        await db.commit()
    return tid


async def get_status(tid: int) -> str | None:
    async with SessionLocal() as db:
        row = await db.get(TaskRecord, tid)
        return row.status if row else None


async def cleanup() -> None:
    async with SessionLocal() as db:
        await db.execute(text("DELETE FROM task WHERE name LIKE 'zz-recovery-%'"))
        await db.commit()


async def check_zombie() -> None:
    print("\n[1] 僵尸收割")
    # 预收割：清掉库里陈年僵尸，计数断言才可用绝对数（基线教训第 3 次）
    await task_recovery.reap_zombies_once()
    stale_running = await seed("running", "rag_evaluation", age_seconds=15 * 60)
    fresh_running = await seed("running", "rag_evaluation", age_seconds=30)
    heartbeated = await seed("running", "rag_evaluation", age_seconds=30 * 60)
    stale_pending = await seed("pending", "benchmark_generate", age_seconds=10 * 60)
    terminal = await seed("success", "rag_evaluation", age_seconds=30 * 60)

    task_recovery.ACTIVE_TASK_IDS.add(heartbeated)
    try:
        killed = await task_recovery.reap_zombies_once()
        ok(f"本轮强制失败 2 个（实际 {killed}）", killed == 2)
        ok("stale running → failed", await get_status(stale_running) == "failed")
        ok("stale pending → failed", await get_status(stale_pending) == "failed")
        ok("fresh running 不动", await get_status(fresh_running) == "running")
        ok("有心跳的超时任务不动（PEL 等价）", await get_status(heartbeated) == "running")
        ok("终态任务 CAS 不改", await get_status(terminal) == "success")

        killed2 = await task_recovery.reap_zombies_once()
        ok("重复 reap 幂等（0 个新杀）", killed2 == 0)
    finally:
        task_recovery.ACTIVE_TASK_IDS.discard(heartbeated)


async def check_recover() -> None:
    print("\n[2] 启动恢复（假 runner）")
    ran: list[int] = []

    async def fake_runner(tid: int, payload: dict) -> str:
        ran.append(tid)
        return f"fake-done-{tid}"

    with patch.object(task_recovery, "_runners", lambda: {"graph_extraction": fake_runner}):
        recoverable = await seed("running", "graph_extraction", age_seconds=60 * 60)
        unknown = await seed("running", "document_upload", age_seconds=60 * 60)
        stats = await task_recovery.recover_unfinished_tasks()

        ok(f"恢复 1 个（实际 {stats['recovered']}）/ 失败 1 个（实际 {stats['failed']}）",
           stats["recovered"] >= 1 and stats["failed"] >= 1, str(stats))
        ok("不可恢复类型 → failed", await get_status(unknown) == "failed")

        # 等 spawn 的假 runner 跑完
        for _ in range(50):
            if ran:
                break
            await asyncio.sleep(0.1)
        ok("可恢复类型被重跑（假 runner 被调）", recoverable in ran)
        status = await get_status(recoverable)
        ok("重跑后终态 success", status == "success", str(status))


async def check_tracked() -> None:
    print("\n[3] tracked 心跳")

    async def work() -> None:
        ok("执行期间心跳在册", 999001 in task_recovery.ACTIVE_TASK_IDS)

    task_recovery.ACTIVE_TASK_IDS.discard(999001)
    await asyncio.wait_for(task_recovery.tracked(999001, work()), timeout=5)
    ok("结束后心跳移除", 999001 not in task_recovery.ACTIVE_TASK_IDS)

    async def boom() -> None:
        raise RuntimeError("boom")

    try:
        await task_recovery.tracked(999002, boom())
    except RuntimeError:
        pass
    ok("异常路径心跳也移除", 999002 not in task_recovery.ACTIVE_TASK_IDS)


async def main() -> int:
    try:
        await check_zombie()
        await check_recover()
        await check_tracked()
    finally:
        await cleanup()

    print(f"\n{'=' * 60}")
    if _FAILED:
        print(f"❌ 失败 {len(_FAILED)} 项: {_FAILED}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
