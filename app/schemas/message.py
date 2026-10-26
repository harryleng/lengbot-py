from datetime import datetime
from typing import Any

from pydantic import AliasChoices, Field, field_validator

from app.schemas.common import CamelModel


class MessageOut(CamelModel):
    """消息输出。字段对齐 Java `Message` 实体。

    `metadata` 这个字段名要特别小心：
    1. SQLAlchemy 的 DeclarativeBase 把 `metadata` 占作保留属性（存的是表定义），
       所以 ORM 侧属性叫 `msg_metadata`（通过 mapped_column("metadata") 映射到真实列）。
    2. 接口输出必须还原成 `metadata`，否则前端读不到。

    因此用 AliasChoices 做「读的时候找 ORM 的 property，写的时候输出 metadata」。
    """

    id: str
    session_id: str
    role: str
    content: str | None = None
    content_type: str | None = None
    token_count: int | None = None
    # 注意：这两个 jsonb 字段的真实结构不固定 —— 实测 tool_events 是**数组**
    # （[{"args": ..., "contentOffset": 55}]），metadata 也可能是数组或对象。
    # 所以用 Any，不要标 dict：标了 dict 会在读到数组时直接 ValidationError。
    metadata: Any = Field(
        default=None,
        validation_alias=AliasChoices("metadata_for_api", "metadata"),
        serialization_alias="metadata",
    )
    tool_events: Any = None
    parent_id: str | None = None
    create_time: datetime
    message_type: str | None = None
    reply_to_message_id: str | None = None
    starred: bool | None = None

    @field_validator("id", "session_id", "parent_id", "reply_to_message_id", mode="before")
    @classmethod
    def _bigint_to_str(cls, v: int | str | None) -> str | None:
        return None if v is None else str(v)
