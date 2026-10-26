"""工具管理的 schema。

和 `model_provider.py` 一样，`inputSchema` / `outputSchema` / `config` / `tags`
这些 JSONB 字段在**接口层都是 JSON 字符串**还是对象？

这里答案和 model-provider **不同**，别照抄那边：

- 工具页的 `inputSchema` 是用户直接编辑的 JSON 文本（`JsonInput` 组件），
  前端提交前自己 `JSON.stringify`，读回来也自己 `JSON.parse`
- 但 `tags` 是数组，前端直接遍历（`tool.tags.map`），必须是数组

所以本文件按字段分别处理：schema 类给字符串，tags 给数组。
判断依据是**前端源码里怎么消费**，不是"看起来统一"。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from pydantic import Field, field_validator

from app.schemas.common import CamelModel


def _to_json_text(value: Any, default: str = "{}") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return default


def _to_tags(value: Any) -> list[str]:
    """tags 一律归一成字符串数组。

    历史数据里可能有 null、`"[]"`（字符串）、甚至单个字符串 —— 前端
    `tool.tags.map(...)` 遇到非数组直接崩，所以这里兜住。
    """
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return [text]
        return _to_tags(parsed)
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if v is not None]
    return [str(value)]


class ToolIn(CamelModel):
    """创建 / 更新请求体（对齐 Java `ToolRequestDTO`）。"""

    id: str | None = None
    name: str
    display_name: str | None = None
    description: str | None = None
    tool_type: str | None = "api"
    input_schema: str | None = None
    output_schema: str | None = None
    output_example: str | None = None
    config: str | None = None
    endpoint_url: str | None = None
    auth_type: str | None = None
    auth_config: str | None = None
    tags: list[str] | None = None
    icon: str | None = None
    status: str | None = None
    rate_limit_enabled: bool | None = False
    rate_limit_config: str | None = None

    @field_validator("id", mode="before")
    @classmethod
    def _id_to_str(cls, v: Any) -> str | None:
        return None if v is None else str(v)

    @field_validator("tags", mode="before")
    @classmethod
    def _tags(cls, v: Any) -> list[str]:
        return _to_tags(v)


class ToolOut(CamelModel):
    """工具响应体。

    `inputSchema` 等给**字符串**（前端 JsonInput 自己 parse），
    `tags` 给**数组**（前端直接 map）。见模块 docstring。
    """

    id: str
    user_id: str | None = None
    name: str | None = None
    display_name: str | None = None
    description: str | None = None
    tool_type: str | None = None
    input_schema: str = "{}"
    output_schema: str = "{}"
    output_example: str = "{}"
    config: str = "{}"
    endpoint_url: str | None = None
    auth_type: str | None = None
    auth_config: str = "{}"
    tags: list[str] = Field(default_factory=list)
    status: str | None = None
    icon: str | None = None
    rate_limit_enabled: bool = False
    rate_limit_config: str | None = None
    create_time: datetime | None = None
    update_time: datetime | None = None
    # 这个工具在当前 Python 实现里**是否真的可执行**。
    # Java 侧没有这个字段，但 Python 只实现了 32 个工具里的十几个，
    # 前端「测试」按钮点了会 400。把状态告诉它，比让用户点一次错一次好。
    implemented: bool = False

    @field_validator("id", "user_id", mode="before")
    @classmethod
    def _id_to_str(cls, v: Any) -> str | None:
        return None if v is None else str(v)

    @field_validator("input_schema", "output_schema", "output_example", "config",
                     "auth_config", mode="before")
    @classmethod
    def _jsonb_to_text(cls, v: Any) -> str:
        return _to_json_text(v)

    @field_validator("tags", mode="before")
    @classmethod
    def _tags(cls, v: Any) -> list[str]:
        return _to_tags(v)

    @field_validator("rate_limit_config", mode="before")
    @classmethod
    def _rate_limit(cls, v: Any) -> str | None:
        return None if v is None else _to_json_text(v)
