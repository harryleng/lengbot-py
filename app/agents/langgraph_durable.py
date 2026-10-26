"""LangGraph 的两个杀手锏：checkpointer（每步持久化）+ interrupt（HITL）。

这个文件和 `langgraph_agent.py` 的区别，就是"能跑"和"能停、能恢复、能回放"的区别。

## 1. Checkpointer：每一步都落库

```python
graph.compile(checkpointer=AsyncPostgresSaver(pool))
```

一行配置得到三件事：
- 每个 super-step 自动写进 Postgres（`checkpoints` / `checkpoint_blobs` / `checkpoint_writes`）
- 进程崩溃后，同一个 `thread_id` 能从最后一步恢复
- 任意历史步骤都能取出来回放（`get_state_history`）

lengbot 为这件事手写了 `workflow_checkpoint` 表 + `WorkflowCheckpointRecovery` 自愈扫描，
约 600 行，还带着"崩溃时正在执行的节点副作用可能重复"的边界风险。

## 2. interrupt：挂起等人

```python
decision = interrupt({"question": "确认要写文件吗？"})
```

调用 `interrupt` 时，LangGraph 会抛一个内部异常，把**当前状态写进 checkpoint 然后退出**。
之后用 `Command(resume=...)` 从同一个 `thread_id` 继续，`interrupt()` 的返回值就是你传进去的值。

关键差异：**挂起态在 Postgres 里，不会过期**。
lengbot 把挂起态放 Redis，TTL 24 小时，过期就丢。

## 3. 一个必须知道的坑

`AsyncPostgresSaver` 用的是 **psycopg3**，而我们项目主体用的是 **asyncpg**（SQLAlchemy 那套）。
两个驱动并存，连接串要转换：

    postgresql+asyncpg://...  →  postgresql://...
    （去掉 SQLAlchemy 的驱动后缀）

不能直接把 SQLAlchemy 的 engine 塞给它。

## 4. 第二个坑：Windows 的事件循环

在 Windows 上 Python 3.8+ 默认用 `ProactorEventLoop`，而 **psycopg 异步模式不支持它**，
会直接抛：

    Psycopg cannot use the 'ProactorEventLoop' to run in async mode

必须在建连接池**之前**切到 `SelectorEventLoop`：

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

注意时机：policy 必须在事件循环创建之前设置，在协程内部设置是无效的。
Linux 服务器没这个问题（默认就是 selector），但本地开发会撞上。
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.messages import ToolMessage
from langchain_core.tools import StructuredTool
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.types import interrupt
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.core.config import settings
from app.services.llm_client import LlmConfig
from app.tools.base import ToolSpec

logger = logging.getLogger(__name__)

# 哪些工具需要人工确认。有副作用的写操作必须拦一道 ——
# 这是 lengbot 里 CONFIRM 节点 / ask_user 工具的场景，这里用 interrupt 实现。
APPROVAL_REQUIRED = {"local_write_file", "local_append_file", "local_delete_file"}
APPROVAL_WORDS = {"approve", "yes", "y", "true", "ok", "确认", "同意", "可以", "批准"}


def _psycopg_dsn(sqlalchemy_url: str) -> str:
    """把 SQLAlchemy 的 URL 转成 psycopg 能用的 DSN。"""
    return sqlalchemy_url.replace("postgresql+asyncpg://", "postgresql://")


# ----------------------------------------------------------------------
# Checkpointer 单例
# ----------------------------------------------------------------------

_pool: AsyncConnectionPool | None = None
_saver: AsyncPostgresSaver | None = None


async def get_checkpointer() -> AsyncPostgresSaver:
    """懒加载 checkpointer（首次调用时建连接池 + 建表）。

    `saver.setup()` 是**幂等**的：表已存在就跳过，所以每次启动都可以安全调用。
    """
    global _pool, _saver
    if _saver is not None:
        return _saver

    dsn = _psycopg_dsn(settings.database_url)
    _pool = AsyncConnectionPool(
        conninfo=dsn,
        max_size=10,
        open=False,
        kwargs={
            # 官方要求：autocommit=True（saver 自己管事务边界）
            # row_factory=dict_row（它内部按列名取值）
            "autocommit": True,
            "row_factory": dict_row,
        },
    )
    await _pool.open()
    saver = AsyncPostgresSaver(_pool)
    await saver.setup()
    _saver = saver
    logger.info("checkpointer 就绪（表已确保存在）")
    return saver


async def close_checkpointer() -> None:
    global _pool, _saver
    if _pool is not None:
        await _pool.close()
    _pool = None
    _saver = None


# ----------------------------------------------------------------------
# 图
# ----------------------------------------------------------------------


def _to_tool(spec: ToolSpec) -> StructuredTool:
    return StructuredTool.from_function(
        coroutine=spec.handler,
        name=spec.name,
        description=spec.description,
        args_schema=spec.params_model,
    )


def _openai_base_url(url: str) -> str:
    u = url.rstrip("/")
    if u.endswith("/v1") or "/compatible-mode" in u:
        return u
    return f"{u}/v1"


def build_durable_graph(
    cfg: LlmConfig,
    specs: list[ToolSpec],
    checkpointer: AsyncPostgresSaver,
    *,
    max_steps: int = 5,
):
    """带 HITL 的图。

    START ─▶ agent ─(要写文件)─▶ approval ─(批准)─▶ tools ─┐
              │  │                                        │
              │  └─(只读工具)──────────────▶ tools ────────┤
              └─(不调工具)─▶ END                            │
              ▲                                            │
              └────────────────────────────────────────────┘
    """
    model = ChatOpenAI(
        model=cfg.model,
        api_key=cfg.api_key,  # type: ignore[arg-type]
        base_url=_openai_base_url(cfg.base_url),
        temperature=cfg.temperature if cfg.temperature is not None else 0.7,
        max_tokens=cfg.max_tokens,
        timeout=300,
    )
    lc_tools = [_to_tool(s) for s in specs]
    if lc_tools:
        model = model.bind_tools(lc_tools)

    async def call_model(state: MessagesState) -> dict[str, Any]:
        response = await model.ainvoke(state["messages"])
        return {"messages": [response]}

    def route(state: MessagesState) -> str:
        last = state["messages"][-1]
        calls = getattr(last, "tool_calls", None)
        if not calls:
            return END
        if any(c.get("name") in APPROVAL_REQUIRED for c in calls):
            return "approval"
        return "tools"

    async def approval(state: MessagesState) -> dict[str, Any]:
        """HITL 节点。执行到这里会挂起，等人给答复。"""
        last = state["messages"][-1]
        calls = getattr(last, "tool_calls", None) or []
        payload = {
            "type": "approval_required",
            "message": "以下操作会修改工作区文件，确认执行吗？",
            "tools": [{"name": c.get("name"), "args": c.get("args") or c.get("arguments")} for c in calls],
        }
        # 这一行就是"挂起"：状态写进 checkpoint，然后整个图退出
        decision = interrupt(payload)
        approved = str(decision).strip().lower() in APPROVAL_WORDS

        if approved:
            # 返回空更新 → 条件边继续走到 tools
            return {}
        # 拒绝必须给每个 tool_call 一条 ToolMessage，
        # 否则模型会一直等"工具结果"，下一轮报错说不出话
        return {
            "messages": [
                ToolMessage(content="用户拒绝执行该操作，请询问用户下一步意图。", tool_call_id=c["id"])
                for c in calls
            ]
        }

    def route_after_approval(state: MessagesState) -> str:
        last = state["messages"][-1]
        # 批准 → 最后一条还是 assistant(tool_calls)，去 tools
        if getattr(last, "tool_calls", None):
            return "tools"
        # 拒绝 → 已经插了 ToolMessage，回 agent 让模型重新措辞
        return "agent"

    graph = StateGraph(MessagesState)
    graph.add_node("agent", call_model)
    graph.add_node("approval", approval)
    if lc_tools:
        graph.add_node("tools", ToolNode(lc_tools, handle_tool_errors=True))

    graph.add_edge(START, "agent")
    if lc_tools:
        graph.add_conditional_edges("agent", route, {"approval": "approval", "tools": "tools", END: END})
        graph.add_conditional_edges("approval", route_after_approval, {"tools": "tools", "agent": "agent"})
        graph.add_edge("tools", "agent")
    else:
        graph.add_edge("agent", END)

    # recursion_limit：每次 agent→tools→agent 算 2 步
    return graph.compile(checkpointer=checkpointer), max(6, max_steps * 2 + 4)
