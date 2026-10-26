"""工作流挂起态注册表（HITL 的"存档点"）。

对照 Java：`WorkflowRunStateUtil` 把 `WorkflowSuspendedRun` 存 Redis（TTL 24h）。
这里放在进程内内存，理由和 `chat_runs.py` 一样：单实例够用，多副本要换 Redis。

**快照里必须有什么才能"从断点继续"**：
| 字段 | 为什么必须 |
|---|---|
| graph | 恢复时要重建图（不存图就得重新查库，而库里的版本可能已经变了） |
| variables | 前面节点算出来的值（`{{query}}`、`{{retrievalResult}}` 全在里面） |
| node_outputs | 调试和 `{{node.x}}` 形式取值 |
| next_node_id | 从哪继续（**不是挂起节点本身** —— 那个已经执行完了） |
| step | 步数计数要接着算，否则恢复后又能跑 50 步 |

**和 LangGraph 的区别**（实测过）：LangGraph 的 checkpointer 是每个 super-step 都持久化，
能回放到任意历史步骤；这里只存"挂起那一刻"，只能前向续跑。
lengbot 的 Redis 方案也是这个语义。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class SuspendedRun:
    run_id: str
    agent_id: int | None
    graph_raw: dict[str, Any]
    variables: dict[str, Any]
    node_outputs: dict[str, Any]
    suspend_node_id: str
    next_node_id: str | None
    step: int
    ctx: dict[str, Any]  # llm 配置 / knowledge_ids 等
    user_id: int | None = None
    session_id: int | None = None
    created_at: float = field(default_factory=time.time)

    def age(self) -> float:
        return time.time() - self.created_at


class SuspendedRunRegistry:
    TTL_SECONDS = 1800  # 30 分钟没确认就丢弃（比 Java 的 24h 短，练手够用）

    def __init__(self) -> None:
        self._runs: dict[str, SuspendedRun] = {}

    def create(self, *, run_id: str | None = None, **kwargs: Any) -> SuspendedRun:
        """登记一个挂起快照。

        `run_id` 可以由调用方指定。为什么要能指定：编排页的「调试运行」会把
        runId 写进 `workflow_test_run` 表，用户点确认后前端带着这个 runId 回来
        恢复，后端要能靠它找到**同一条**测试记录把状态改成 completed。
        两个 id 各生成各的，就对不上了。
        """
        self._gc()
        run = SuspendedRun(run_id=run_id or uuid.uuid4().hex, **kwargs)
        self._runs[run.run_id] = run
        return run

    def get(self, run_id: str) -> SuspendedRun | None:
        return self._runs.get(run_id)

    def pop(self, run_id: str) -> SuspendedRun | None:
        """取出并删除（恢复后这个存档点就作废了）。"""
        return self._runs.pop(run_id, None)

    def _gc(self) -> None:
        now = time.time()
        for key, run in list(self._runs.items()):
            if now - run.created_at > self.TTL_SECONDS:
                self._runs.pop(key, None)


suspended_runs = SuspendedRunRegistry()
