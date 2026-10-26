"""工作流调试运行记录（`workflow_test_run` 表）。

对应 Java `WorkflowTestRunServiceImpl`。

**为什么要有这个**：编排页的"调试"抽屉关掉再打开，用户还想看上一次每个节点
到底吐了什么。只把结果留在内存里，一刷新就没了，等于每次都重跑一遍（还烧 token）。

保留策略照抄 Java：**每个 agent 只留最近 50 条**，超出物理删最旧的。
调试记录是消耗品，不是审计日志 —— 真要审计得另外建表。

`node_events` 存的是**前端格式**（`workflow_node_complete` / `workflow_complete`），
不是执行器的内部格式（`workflow_node_end` / `workflow_done`）。这样前端从
历史记录读出来的结构，和实时 SSE 流推的结构完全一致，不用写两套渲染逻辑。
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.snowflake import new_id
from app.models.entities import WorkflowTestRun

logger = logging.getLogger(__name__)

MAX_RETAIN_PER_AGENT = 50
SUMMARY_MAX_LEN = 120


def new_run_id() -> str:
    """32 位无横线 UUID（对齐 Java `UUID.randomUUID().toString().replace("-","")`）。"""
    return uuid.uuid4().hex


def _summarize(value: str | None) -> str | None:
    if not value:
        return None
    text = value.strip().replace("\n", " ")
    return text if len(text) <= SUMMARY_MAX_LEN else text[:SUMMARY_MAX_LEN] + "…"


class WorkflowTestRunService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # -- 写 -------------------------------------------------------------

    async def start(
        self,
        *,
        agent_id: int,
        user_id: int,
        run_id: str,
        user_input: str | None,
        test_mode: str,
        used_draft: bool,
        graph: dict[str, Any] | None,
    ) -> None:
        now = datetime.now()
        self.db.add(
            WorkflowTestRun(
                id=new_id(),
                run_id=run_id,
                agent_id=agent_id,
                user_id=user_id,
                test_mode=test_mode or "generation",
                used_draft=1 if used_draft else 0,
                status="running",
                user_input=user_input,
                node_events=[],
                workflow_graph=graph,
                start_time=now,
                create_time=now,
                update_time=now,
                deleted=0,
            )
        )
        await self.db.commit()
        await self._trim(agent_id)

    async def finish(
        self,
        *,
        run_id: str,
        output: str | None,
        node_events: list[dict[str, Any]] | None,
        variables: dict[str, Any] | None,
        error: str | None,
        suspended: bool,
        duration_ms: int,
    ) -> None:
        """收尾。用一条 UPDATE 写完，不做 read-modify-write。

        （`chat_session` 的 token 统计被覆盖那次就是 read-modify-write 的锅：
        读出来的实体是缓存里的旧值，改完整体回写，把别处并发累加的值抹掉。）
        """
        if error:
            status = "failed"
        elif suspended:
            status = "suspended"
        else:
            status = "completed"

        values: dict[str, Any] = {
            "status": status,
            "duration_ms": duration_ms,
            "end_time": datetime.now(),
            "update_time": datetime.now(),
        }
        if output is not None:
            values["output"] = output
        if node_events is not None:
            values["node_events"] = node_events
        if variables is not None:
            values["variables"] = variables
        if error:
            values["error_info"] = error

        await self.db.execute(
            update(WorkflowTestRun)
            .where(WorkflowTestRun.run_id == run_id)
            .values(**values)
        )
        await self.db.commit()

    async def _trim(self, agent_id: int) -> None:
        """超出 50 条就物理删最旧的。"""
        ids = (
            await self.db.execute(
                select(WorkflowTestRun.id)
                .where(WorkflowTestRun.agent_id == agent_id)
                .order_by(WorkflowTestRun.start_time.desc(), WorkflowTestRun.id.desc())
            )
        ).scalars().all()
        if len(ids) <= MAX_RETAIN_PER_AGENT:
            return
        await self.db.execute(
            delete(WorkflowTestRun).where(WorkflowTestRun.id.in_(ids[MAX_RETAIN_PER_AGENT:]))
        )
        await self.db.commit()

    # -- 读 -------------------------------------------------------------

    async def list_runs(self, agent_id: int, limit: int = MAX_RETAIN_PER_AGENT) -> list[dict]:
        size = min(limit, MAX_RETAIN_PER_AGENT) if limit > 0 else MAX_RETAIN_PER_AGENT
        rows = (
            await self.db.execute(
                select(WorkflowTestRun)
                .where(WorkflowTestRun.agent_id == agent_id)
                .order_by(WorkflowTestRun.start_time.desc(), WorkflowTestRun.id.desc())
                .limit(size)
            )
        ).scalars().all()
        return [
            {
                "id": str(r.id),
                "runId": r.run_id,
                "status": r.status,
                "testMode": r.test_mode,
                "usedDraft": r.used_draft == 1,
                "userInputSummary": _summarize(r.user_input),
                "outputSummary": _summarize(r.output),
                "durationMs": r.duration_ms,
                "startTime": r.start_time,
                "endTime": r.end_time,
            }
            for r in rows
        ]

    async def _require(self, agent_id: int, run_id: str) -> WorkflowTestRun:
        from fastapi import HTTPException, status

        row = (
            await self.db.execute(
                select(WorkflowTestRun).where(
                    WorkflowTestRun.agent_id == agent_id,
                    WorkflowTestRun.run_id == run_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="测试记录不存在"
            )
        return row

    async def get_detail(self, agent_id: int, run_id: str) -> dict[str, Any]:
        r = await self._require(agent_id, run_id)
        return {
            "id": str(r.id),
            "runId": r.run_id,
            "status": r.status,
            "testMode": r.test_mode,
            "usedDraft": r.used_draft == 1,
            "userInput": r.user_input,
            "output": r.output,
            "nodeEvents": r.node_events or [],
            "variables": r.variables or {},
            "workflowGraph": r.workflow_graph or {},
            "errorInfo": r.error_info,
            "durationMs": r.duration_ms,
            "startTime": r.start_time,
            "endTime": r.end_time,
        }

    async def delete_run(self, agent_id: int, run_id: str) -> None:
        row = await self._require(agent_id, run_id)
        await self.db.execute(delete(WorkflowTestRun).where(WorkflowTestRun.id == row.id))
        await self.db.commit()

    async def clear(self, agent_id: int) -> None:
        await self.db.execute(
            delete(WorkflowTestRun).where(WorkflowTestRun.agent_id == agent_id)
        )
        await self.db.commit()

    async def count(self, agent_id: int) -> int:
        return int(
            (
                await self.db.execute(
                    select(func.count())
                    .select_from(WorkflowTestRun)
                    .where(WorkflowTestRun.agent_id == agent_id)
                )
            ).scalar()
            or 0
        )
