"""工具上下文：把"当前这次对话属于哪个 Agent / 哪些知识库"传给工具。

为什么需要：`ToolSpec.handler` 的签名只有业务参数（比如 `query_knowledge(question)`），
但知识库检索必须知道"检索哪几个库"——那个信息在 `agent.config.knowledges` 里。

三个可选方案：
    1. 把 knowledgeId 做成工具参数，让模型显式传  ← 模型会瞎编 ID
    2. 全局单例存当前 agent                        ← 并发请求会串
    3. **ContextVar**（asyncio 的 ThreadLocal）     ← 正确

ContextVar 的关键特性：`asyncio.create_task` 会给新 task **复制**当前的 context，
所以每个对话的 producer task 有自己独立的一份，并发不串。

对照 Java：这就是 `ThreadLocal` / `RequestContextHolder`，
只不过 asyncio 里"线程"换成了"task"，ThreadLocal 会失效（因为多个 task 跑在一个线程上），
所以必须用 ContextVar。
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass, field


@dataclass(slots=True)
class AgentToolContext:
    """一次对话运行时的工具上下文。

    注意这个对象是**同一个对话内共享的可变状态**（ContextVar 存的是引用），
    所以可以在这里累积"已经检索过什么"，用来做跨轮去重和次数限制。
    """

    agent_id: int | None = None
    user_id: int | None = None
    # 该 Agent 绑定的知识库 ID 列表
    knowledge_ids: list[int] = field(default_factory=list)
    # 本对话已经做过几次知识库检索（防模型无限换角度重搜）
    query_count: int = 0
    # 本对话已经返回过给模型的 chunk_id（跨轮去重，避免同一段内容反复进上下文）
    seen_chunk_ids: set[str] = field(default_factory=set)
    # 累计的引用，最终会写进 assistant 消息的 metadata 和 [DONE] 事件。
    # 前端靠这个渲染"引用来源"卡片（结构对齐 Java 的 ragReferences）。
    rag_references: list[dict] = field(default_factory=list)
    # 引用去重（同一 chunk 被多次检索命中时只记一次）
    _ref_chunk_ids: set[str] = field(default_factory=set)

    def add_references(self, refs: list[dict]) -> None:
        """追加引用，按 chunkId 去重。"""
        for ref in refs:
            key = str(ref.get("chunkId") or "")
            if key and key in self._ref_chunk_ids:
                continue
            if key:
                self._ref_chunk_ids.add(key)
            self.rag_references.append(ref)


_current: ContextVar[AgentToolContext | None] = ContextVar("agent_tool_context", default=None)


def set_context(ctx: AgentToolContext) -> Token:
    return _current.set(ctx)


def reset_context(token: Token) -> None:
    _current.reset(token)


def current_context() -> AgentToolContext | None:
    return _current.get()


def current_knowledge_ids() -> list[int]:
    ctx = _current.get()
    return list(ctx.knowledge_ids) if ctx else []
