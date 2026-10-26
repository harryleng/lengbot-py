"""MCP 服务器管理的 schema。

序列化规则和 `model_provider.py` 一致（两者都是 JsonbTypeHandler 那一套）：
`deployConfig` / `detailConfig` / `headers` / `disabledTools` 出参都是**字符串**。

`disabledTools` 特殊：它本质是数组，但 Java 侧字段是 String（JSONB handler），
所以接口给的是 `"[\\"tool_a\\"]"` 这种文本。前端 `listTools` 那边才 parse。

`isBuiltin` 是 smallint（1/0），前端 `Number(x) === 1` 判断。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from pydantic import field_validator

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


class McpServerIn(CamelModel):
    id: str | None = None
    name: str
    description: str | None = None
    icon: str | None = None
    install_type: str | None = "npx"
    deploy_config: str | None = None
    detail_config: str | None = None
    host: str | None = None
    transport: str | None = "stdio"
    headers: str | None = None
    disabled_tools: str | None = None

    @field_validator("id", mode="before")
    @classmethod
    def _id_to_str(cls, v: Any) -> str | None:
        return None if v is None else str(v)


class McpServerOut(CamelModel):
    id: str
    name: str | None = None
    description: str | None = None
    icon: str | None = None
    install_type: str | None = None
    deploy_config: str = "{}"
    detail_config: str = "{}"
    host: str | None = None
    status: str | None = None
    transport: str | None = None
    headers: str = "{}"
    disabled_tools: str = "[]"
    is_builtin: int = 0
    create_time: datetime | None = None
    update_time: datetime | None = None
    last_sync_time: datetime | None = None

    @field_validator("id", mode="before")
    @classmethod
    def _id_to_str(cls, v: Any) -> str:
        return str(v)

    @field_validator("deploy_config", "detail_config", "headers", mode="before")
    @classmethod
    def _jsonb_to_text(cls, v: Any) -> str:
        return _to_json_text(v)

    @field_validator("disabled_tools", mode="before")
    @classmethod
    def _disabled(cls, v: Any) -> str:
        return _to_json_text(v, default="[]")
