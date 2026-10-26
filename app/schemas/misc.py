"""零散模块的出参/入参：枚举、Landing、日志、Dashboard、可观测性、工具调用记录。

这一批的共同点：**没有复杂业务**，主要是「把库里的数按前端认的字段名吐出去」。
字段命名全部照抄 Java VO（camelCase 由 `CamelModel` 自动转换），
尤其是 Dashboard 那几个 Map 的 key —— 前端 `dashboard.js` 直接按 key 取，改一个就空一块面板。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.schemas.common import CamelModel


# ---------------- 枚举 ----------------
class EnumVO(CamelModel):
    """Java `EnumController.EnumVO`：`value` 是 code，`label` 是中文 desc。"""

    value: str
    label: str


# ---------------- Landing ----------------
class LandingValidation(BaseModel):
    """校验结果（超长字段会被点名，方便前端定位）。"""

    valid: bool
    errors: list[str] = Field(default_factory=list)


# ---------------- 日志 ----------------
class LogEvent(CamelModel):
    """Java `MemoryLogAppender.LogEvent` 的字段集。

    Python 没有 Logback Appender，这里用进程内的 deque 环形缓冲顶上：
    容量 2000，`GET /logs/recent` 返回的条数上限和 Java 一致。
    """

    timestamp: str
    level: str
    logger: str | None = None
    thread: str | None = None
    message: str
    stacktrace: str | None = None


# ---------------- Dashboard ----------------
class DashboardBasic(CamelModel):
    """`/dashboard/basic`：8 个计数，key 严格对齐 Java `getBasicStats`。"""

    agent_count: int = 0
    knowledge_count: int = 0
    session_count: int = 0
    message_count: int = 0
    provider_count: int = 0
    model_count: int = 0
    document_count: int = 0
    chunk_count: int = 0


class AgentStatusItem(CamelModel):
    code: str
    label: str
    count: int = 0


class AgentRecentItem(CamelModel):
    id: str
    name: str | None = None
    status: str | None = None
    status_label: str | None = None
    create_time: datetime | None = None


class DashboardAgent(CamelModel):
    total: int = 0
    status_list: list[AgentStatusItem] = Field(default_factory=list)
    status_counts: dict[str, int] = Field(default_factory=dict)
    recent: list[AgentRecentItem] = Field(default_factory=list)


class DocRecentItem(CamelModel):
    id: str
    name: str | None = None
    knowledge_id: str | None = None
    create_time: datetime | None = None


class DashboardKnowledge(CamelModel):
    total_knowledge: int = 0
    total_documents: int = 0
    total_chunks: int = 0
    document_status_counts: dict[str, int] = Field(default_factory=dict)
    recent_documents: list[DocRecentItem] = Field(default_factory=list)


class MessagePerDay(CamelModel):
    date: str
    count: int = 0


class DashboardChat(CamelModel):
    """Java 只在「没传 startDate/endDate」时才放 `trendDays`，Python 照做。"""

    total_sessions: int = 0
    total_messages: int = 0
    messages_per_day: list[MessagePerDay] = Field(default_factory=list)
    trend_start_date: str | None = None
    trend_end_date: str | None = None
    trend_days: int | None = None


# ---------------- 可观测性 ----------------
class LlmTraceOut(CamelModel):
    id: str
    request_id: str | None = None
    session_id: str | None = None
    user_id: str | None = None
    agent_id: str | None = None
    agent_name: str | None = None
    model: str | None = None
    status: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    tool_call_count: int = 0
    total_duration_ms: int = 0
    first_token_ms: int = 0
    prompt_cache_hit_tokens: int = 0
    error_message: str | None = None
    reply_content: str | None = None
    display_content: str | None = None
    trace_source: str | None = None
    create_time: datetime | None = None


class LlmTraceDetail(LlmTraceOut):
    """详情 = 列表字段 + spans。

    `spans` 在 Java 里是 TEXT 存的 JSON 字符串，解析失败返回空数组而不是报错；
    Python 直接存 JSONB，但仍保留「解析不出来就给空列表」的兜底语义。
    """

    spans: list[Any] = Field(default_factory=list)


class TraceOverview(CamelModel):
    total_count: int = 0
    success_count: int = 0
    failed_count: int = 0
    total_tokens: int = 0
    avg_duration_ms: int = 0
    total_tool_calls: int = 0
    # Java 只在 totalCount > 0 时才塞这两个 key，Python 用 None 表达"没有"
    avg_first_token_ms: int | None = None
    max_first_token_ms: int | None = None


class TracePage(CamelModel):
    """Java 返回的是裸 Map（records/total/pageNum/pageSize），不是 MP 的 Page 对象。"""

    records: list[LlmTraceOut] = Field(default_factory=list)
    total: int = 0
    page_num: int = 1
    page_size: int = 20


# ---------------- 工具调用记录 ----------------
class ToolCallOut(CamelModel):
    id: str
    message_id: str | None = None
    tool_name: str | None = None
    tool_input: Any | None = None
    tool_output: str | None = None
    status: str | None = None
    error_message: str | None = None
    created_at: datetime | None = None


class ToolCallPage(CamelModel):
    records: list[ToolCallOut] = Field(default_factory=list)
    total: int = 0
    page_num: int = 1
    page_size: int = 20
