"""消息反馈 + 会话扩展（搜索 / 收藏 / 归档 / 附件）的 DTO。

对照 Java：`MessageFeedbackRequestDTO` / `MessageFeedbackVO` /
`ConversationSearchResultVO` / `SessionAttachmentVO`。

**输出一律驼峰**，且 `id` 类字段给**字符串**（19 位雪花超 JS 安全整数）。

一个容易忽略的点：`batchGetFeedbacks` 返回的是 `Map<Long, MessageFeedback>`，
Jackson 序列化后 **key 是字符串**（JSON 的 key 只能是字符串），value 是实体。
前端 `feedbacks[String(messageId)]` 这样取，所以不要返回数组。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field, field_validator

from app.schemas.common import CamelModel

RATING_LIKE = "like"
RATING_DISLIKE = "dislike"
VALID_RATINGS = {RATING_LIKE, RATING_DISLIKE}


class FeedbackIn(CamelModel):
    rating: str = Field(..., min_length=1, max_length=20)
    reason: str | None = Field(None, max_length=1000)

    @field_validator("rating")
    @classmethod
    def _check_rating(cls, v: str) -> str:
        value = (v or "").strip().lower()
        if value not in VALID_RATINGS:
            raise ValueError("rating 只能是 like 或 dislike")
        return value


class FeedbackOut(CamelModel):
    id: str
    message_id: str
    user_id: str
    rating: str
    reason: str | None = None
    agent_id: str | None = None
    agent_version: int | None = None
    create_time: datetime | None = None

    @field_validator("id", "message_id", "user_id", "agent_id", mode="before")
    @classmethod
    def _id_to_str(cls, v: Any) -> str | None:
        return None if v is None else str(v)


class FeedbackListOut(CamelModel):
    """`GET /chat/feedbacks` 的行：反馈本身 + 消息内容 + Agent 名。

    额外带上 `messageContent` / `sessionId` / `agentName` 是为了让
    「反馈管理」页面不用再逐条查消息（Java 也是这么拼的）。
    """

    id: str
    message_id: str
    rating: str
    reason: str | None = None
    create_time: datetime | None = None
    message_content: str | None = None
    session_id: str | None = None
    agent_id: str | None = None
    agent_version: int | None = None
    agent_name: str | None = None

    @field_validator("id", "message_id", "session_id", "agent_id", mode="before")
    @classmethod
    def _id_to_str(cls, v: Any) -> str | None:
        return None if v is None else str(v)


class ConversationSearchOut(CamelModel):
    """跨会话搜索结果（`GET /chat/sessions/search`）。"""

    message_id: str
    message_role: str | None = None
    snippet: str = ""
    message_create_time: datetime | None = None
    session_id: str
    session_title: str | None = None
    agent_id: str | None = None
    pinned: bool = False
    session_last_message_at: datetime | None = None

    @field_validator("message_id", "session_id", "agent_id", mode="before")
    @classmethod
    def _id_to_str(cls, v: Any) -> str | None:
        return None if v is None else str(v)


class SessionAttachmentOut(CamelModel):
    """会话附件（存在 `chat_session.attachments` 这个 jsonb 数组里）。

    **没有独立的附件表**，也没有 MinIO 依赖 —— 所以这批接口能完整跑。
    `previewUrl` 由 Java 侧按需刷新（要打 MinIO），这里原样透传存量值。
    """

    id: str
    type: str | None = None
    mime_type: str | None = None
    object_key: str | None = None
    preview_url: str | None = None
    file_name: str | None = None
    source: str | None = None
    relative_path: str | None = None
    tool_name: str | None = None
    message_id: str | None = None
    created_at: str | None = None

    @field_validator("id", "message_id", mode="before")
    @classmethod
    def _id_to_str(cls, v: Any) -> str | None:
        return None if v is None else str(v)


class RagReferenceOut(CamelModel):
    """RAG 引用（`GET /chat/rag-references`）。

    Java 有两种 `sourceType`：`chunk`（文档块）和 `qa_pair`（问答对）。
    Python 版只做 chunk —— 库里没有 qa_pair 数据。
    """

    document_name: str | None = None
    content_preview: str | None = None
    score: float | None = None
    knowledge_id: str | None = None
    document_id: str | None = None
    chunk_id: str | None = None
    source_type: str = "chunk"
    qa_pair_id: str | None = None

    @field_validator("knowledge_id", "document_id", "chunk_id", "qa_pair_id", mode="before")
    @classmethod
    def _id_to_str(cls, v: Any) -> str | None:
        return None if v is None else str(v)
