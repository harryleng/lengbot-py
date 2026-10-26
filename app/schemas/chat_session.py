from datetime import datetime
from typing import Any

from pydantic import Field, field_validator

from app.schemas.common import CamelModel


class ChatSessionOut(CamelModel):
    """会话详情/列表项。字段与 Java `ChatSession` 实体逐一对齐。

    最要紧的一条：**所有 bigint 主键必须序列化成字符串**。
    snowflake ID 是 19 位整数（如 2111827758993965057），
    超过 JS 的 Number.MAX_SAFE_INTEGER（9007199254740991），
    直接给数字前端会精度丢失、拿到的 id 变样，后续详情/删除全 404。
    """

    id: str
    agent_id: str | None = None
    agent_version_id: str | None = None
    user_id: str
    title: str | None = None
    status: str
    context: dict[str, Any] | None = None
    message_count: int = 0
    total_tokens: int = 0
    last_message_at: datetime | None = None
    pinned: bool = False
    # 库里存的是 jsonb 数组（[{id,type,...}]），前端按数组渲染。
    # 之前误标成 dict[str, Any]，真实 list 数据校验失败。
    attachments: list[Any] | None = None
    create_time: datetime
    update_time: datetime
    deleted: int = 0

    @field_validator("id", "agent_id", "agent_version_id", "user_id", mode="before")
    @classmethod
    def _bigint_to_str(cls, v: int | str | None) -> str | None:
        return None if v is None else str(v)


class ChatSessionCreateIn(CamelModel):
    """创建会话入参（阶段 0 暂未实现，先定义契约）。"""

    agent_id: str = Field(description="绑定的 Agent ID，bigint 用字符串传，避免前端精度问题")
