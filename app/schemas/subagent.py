"""SubAgent 的 DTO。

对齐 Java `SubAgentRequestDTO`（入参）与 `SubAgent` 实体（出参）。

**`toolIds` 的形态是不对称的，这是最容易写错的一处**：

| 方向 | 形态 | 出处 |
|---|---|---|
| 入参 | **数组** `["123","456"]` | Java DTO 是 `List<String>` |
| 出参 | **JSON 字符串** `'["123","456"]'` | 实体是 String + JsonbTypeHandler |

前端也是这么写的：提交时 `toolIds: form.toolIds`（数组），
展示时 `JSON.parse(toolIdsJson)`（字符串）。照着改会直接白屏。

`enabled` / `isBuiltin` 是 **smallint(0/1)**，出参给数字不给 bool ——
前端 `Number(x) === 1` 判断，给 `true` 会全部判成"未启用"。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field, field_validator

from app.schemas.common import CamelModel
from app.schemas.prompt import to_json_text


class SubAgentIn(CamelModel):
    id: str | None = None
    name: str = Field(..., min_length=1, max_length=30)
    display_name: str = Field(..., min_length=1, max_length=30)
    icon: str | None = Field(None, max_length=64)
    description: str = Field(..., min_length=1, max_length=50)
    system_prompt: str = Field(..., min_length=1, max_length=2000)
    # 入参是数组（见模块 docstring）
    tool_ids: list[str] | None = None
    # 两个字段都表示"用哪个供应商"，resolveProviderId 优先取 providerId
    provider_id: str | None = None
    model_id: str | None = None
    llm_model: str | None = None
    connect_timeout_seconds: int | None = None
    read_timeout_seconds: int | None = None
    model_retry_times: int | None = None
    enabled: bool | None = None

    @field_validator("id", "provider_id", "model_id", mode="before")
    @classmethod
    def _id_to_str(cls, v: Any) -> str | None:
        return None if v is None else str(v)

    def resolve_provider_id(self) -> int | None:
        """对齐 Java `resolveProviderId()`：providerId 优先，其次 modelId。"""
        raw = self.provider_id if self.provider_id is not None else self.model_id
        if raw is None:
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None


class SubAgentOut(CamelModel):
    id: str
    name: str
    display_name: str
    description: str
    system_prompt: str
    icon: str | None = None
    # 出参是 JSON 字符串（见模块 docstring）
    tool_ids: str = "[]"
    model_id: str | None = None
    llm_model: str | None = None
    connect_timeout_seconds: int = 10
    read_timeout_seconds: int = 60
    model_retry_times: int = 1
    enabled: int = 1
    is_builtin: int = 0
    user_id: str | None = None
    create_time: datetime | None = None
    update_time: datetime | None = None

    @field_validator("id", "model_id", "user_id", mode="before")
    @classmethod
    def _id_to_str(cls, v: Any) -> str | None:
        return None if v is None else str(v)

    @field_validator("tool_ids", mode="before")
    @classmethod
    def _tool_ids_to_text(cls, v: Any) -> str:
        # 空数组也要给 "[]"，前端 `JSON.parse('')` 会抛
        return to_json_text(v if v is not None else [])
