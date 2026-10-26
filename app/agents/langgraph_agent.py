"""LangGraph 版工具循环 —— 和手写版做对照。

**目的不是替代，是理解框架替你做了什么。**

手写版（`chat_stream_service._produce`）里这些事你要自己管：
    1. 拼 tools schema
    2. 解析流式 tool_calls（分片累积）
    3. 把 assistant 的 tool_calls 回灌
    4. 把每个工具结果作为 role=tool 回灌
    5. 判断"还要不要继续调工具"
    6. 记录已经调了几轮（防死循环）

LangGraph 版里，这六件事分别由 ToolNode、add_conditional_edges、递归上限接管。
你只需要声明"有 agent 节点和 tools 节点，agent 决定下一步去哪"。

**代码量对比**：手写约 120 行（含协议细节和注释），这里约 40 行。
但代价是：所有细节都进了黑盒，出问题时你得读框架源码才知道发生了什么。

复用关系：工具定义仍然用 `ToolSpec`（`app/tools/base.py`），
只是转成 LangChain 的 `StructuredTool`。**工具定义只有一处**，这点很重要。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

from langchain_core.tools import StructuredTool
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode

from app.services.llm_client import LlmConfig
from app.tools.base import ToolSpec

logger = logging.getLogger(__name__)


def _openai_base_url(url: str) -> str:
    """ChatOpenAI 需要以 /v1 结尾的 base_url（它自己拼 /chat/completions）。

    注意和我们手写版 `_chat_completions_url()` 的区别：
    手写版拼的是完整路径，这里给的是前缀。两处规则要保持一致，
    否则会出现"手写版能连、LangGraph 版 404"这种诡异现象。
    """
    u = url.rstrip("/")
    if u.endswith("/v1") or "/compatible-mode" in u:
        return u
    return f"{u}/v1"


def to_langchain_tool(spec: ToolSpec) -> StructuredTool:
    """ToolSpec → StructuredTool。

    直接复用 `spec.params_model` 作为 args_schema，
    所以 schema 依然来自函数类型注解，不存在第二份定义。
    """
    return StructuredTool.from_function(
        coroutine=spec.handler,
        name=spec.name,
        description=spec.description,
        args_schema=spec.params_model,
    )


def build_model(cfg: LlmConfig) -> ChatOpenAI:
    return ChatOpenAI(
        model=cfg.model,
        api_key=cfg.api_key,  # type: ignore[arg-type]
        base_url=_openai_base_url(cfg.base_url),
        temperature=cfg.temperature if cfg.temperature is not None else 0.7,
        max_tokens=cfg.max_tokens,
        timeout=300,
    )


def build_graph(cfg: LlmConfig, specs: list[ToolSpec], *, max_steps: int = 5):
    """编译一张最小可用的 ReAct 图。

    图结构（就这么简单）：

        START ──▶ agent ──(有 tool_calls)──▶ tools ──┐
                    │                                │
                    └──(没有 tool_calls)──▶ END      │
                    ▲                                │
                    └────────────────────────────────┘
    """
    model = build_model(cfg)
    if specs:
        model = model.bind_tools([to_langchain_tool(s) for s in specs])

    async def call_model(state: MessagesState) -> dict[str, Any]:
        response = await model.ainvoke(state["messages"])
        return {"messages": [response]}

    def should_continue(state: MessagesState) -> str:
        last = state["messages"][-1]
        # 模型想调工具 → 去 tools 节点；否则结束
        return "tools" if getattr(last, "tool_calls", None) else END

    graph = StateGraph(MessagesState)
    graph.add_node("agent", call_model)
    if specs:
        graph.add_node("tools", ToolNode([to_langchain_tool(s) for s in specs], handle_tool_errors=True))

    graph.add_edge(START, "agent")
    if specs:
        graph.add_conditional_edges("agent", should_continue, {"tools": "tools", END: END})
        graph.add_edge("tools", "agent")
    else:
        graph.add_edge("agent", END)

    # recursion_limit 是 LangGraph 的防死循环机制：
    # 默认 25，每次「agent → tools → agent」算 2 步，所以 5 轮工具约需 12。
    # 这里按 max_steps 换算，和手写版的 maxExecutionSteps 语义对齐。
    return graph.compile(), max(4, max_steps * 2 + 2)


async def astream_events(
    cfg: LlmConfig,
    specs: list[ToolSpec],
    messages: list[dict[str, Any]],
    *,
    max_steps: int = 5,
) -> AsyncIterator[dict[str, Any]]:
    """跑一次对话，把 LangGraph 的原始事件流抛给调用方。

    刻意不在这里翻译成 SSE 帧 —— 先看清楚框架到底给了什么事件，
    再决定怎么映射，比一上来就套一层转换更清楚。
    """
    graph, recursion_limit = build_graph(cfg, specs, max_steps=max_steps)
    config = {"recursion_limit": recursion_limit}
    async for event in graph.astream_events({"messages": messages}, config=config, version="v2"):
        yield event
