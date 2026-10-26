"""Prompt 管理 / 版本 / 构建模板的 DTO（对齐 Java `Application/dto` 那层）。

**三个 key 的形态必须记清楚**（容易写错，错了前端静默白屏）：

| 字段 | 入参 | 出参 |
|---|---|---|
| `id` | query / path | **字符串**（19 位雪花超 JS 安全整数） |
| `variables` / `modelConfig` / `toolConfig` | **JSON 字符串** | **JSON 字符串** |
| `status` | `"release"` / `"pre"` | 同 |

Java 实体用 `JsonbTypeHandler` 把 jsonb 列当 String 读写，所以出入参都是字符串。
前端两种都兼容（`typeof x === 'string' ? JSON.parse(x) : x`），
这里保持一致 —— 前端从 Java 切到 Python 不用改一行。

`PromptBuildTemplate.variables` 是**例外**：那列是 varchar，本来就是字符串，
不需要 JSON 解析（虽然内容长得像 JSON）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import AliasChoices, Field, field_validator, model_validator

from app.schemas.common import CamelModel

# ----------------------------------------------------------------------
# 工具函数
# ----------------------------------------------------------------------


def to_json_text(value: Any) -> str:
    """jsonb 值 → 出参字符串。None 给 `"{}"`（前端 JSON.parse 不会崩）。

    为什么 None 不给 `null`：前端 `JSON.parse(versionData.modelConfig)` 只在
    `if (versionData.modelConfig)` 为真时才执行，null 本来也不会进去。
    但列表页有直接 `JSON.parse(x || '{}')` 的写法，给 `"{}"` 最稳。
    """
    import json as _json

    if value is None:
        return "{}"
    if isinstance(value, str):
        return value
    return _json.dumps(value, ensure_ascii=False)


def parse_json_value(raw: Any, *, default: Any = None) -> Any:
    """入参字符串 → 可直接写 jsonb 的值。

    三种情况都要接住：
    - `None` / `""` → default（表示"没传"）
    - 合法 JSON → 解析后写库（dict / list）
    - **非法 JSON**（用户手滑删了个引号）→ 原样存成字符串？

    最后这条要想清楚：直接报 400 更诚实，但会让用户丢掉整段配置；
    原样存字符串则写不进 jsonb 列（PG 会拒绝）。
    所以这里选择**抛错**，让调用方转成 400 并说明是哪个字段。
    """
    import json as _json

    if raw is None:
        return default
    if not isinstance(raw, str):
        return raw
    text = raw.strip()
    if not text:
        return default
    try:
        return _json.loads(text)
    except _json.JSONDecodeError as exc:
        raise ValueError(f"不是合法的 JSON：{exc.msg}（位置 {exc.pos}）") from exc


# ----------------------------------------------------------------------
# Prompt 主表
# ----------------------------------------------------------------------
class PromptCreateIn(CamelModel):
    prompt_key: str = Field(..., min_length=1, max_length=100)
    description: str | None = Field(None, max_length=500)
    tags: str | None = Field(None, max_length=500)


class PromptOut(CamelModel):
    id: str
    prompt_key: str
    description: str | None = None
    latest_version: str | None = None
    tags: str | None = None
    user_id: str | None = None
    create_time: datetime | None = None
    update_time: datetime | None = None

    @field_validator("id", "user_id", mode="before")
    @classmethod
    def _id_to_str(cls, v: Any) -> str | None:
        return None if v is None else str(v)


# ----------------------------------------------------------------------
# Prompt 版本
# ----------------------------------------------------------------------
class PromptVersionIn(CamelModel):
    prompt_key: str = Field(..., min_length=1)
    version: str = Field(..., min_length=1, max_length=50)
    version_desc: str | None = Field(None, max_length=500)
    template: str = Field(..., min_length=1)
    # 三个都是 JSON 字符串
    variables: str | None = None
    # `model_config` 是 Pydantic v2 的保留属性名，不能直接做字段名。
    # 用 `model_config_json` + 显式 alias，对前端仍然是 `modelConfig`。
    model_config_json: str | None = Field(None, alias="modelConfig")
    tool_config: str | None = None
    # release / pre，默认 pre
    status: str | None = None


class PromptVersionOut(CamelModel):
    id: str
    prompt_key: str
    version: str
    version_desc: str | None = None
    template: str
    variables: str = "{}"
    model_config_json: str = Field(
        "{}",
        alias="modelConfig",
        # `from_attributes` 时 Pydantic 会按 alias 去找 ORM 属性，而 ORM 上的
        # 属性名是 `model_config`。只给 alias 会导致**永远取到默认值 "{}"**
        # （不报错，静默丢字段）。validation_alias 两个都认才正确。
        validation_alias=AliasChoices("modelConfig", "model_config"),
    )
    tool_config: str = "{}"
    status: str
    user_id: str | None = None
    create_time: datetime | None = None
    update_time: datetime | None = None

    @field_validator("id", "user_id", mode="before")
    @classmethod
    def _id_to_str(cls, v: Any) -> str | None:
        return None if v is None else str(v)

    @field_validator("variables", "model_config_json", "tool_config", mode="before")
    @classmethod
    def _jsonb_to_text(cls, v: Any) -> str:
        return to_json_text(v)


# ----------------------------------------------------------------------
# Prompt 构建模板
# ----------------------------------------------------------------------
class PromptTemplateIn(CamelModel):
    prompt_template_key: str = Field(..., min_length=1, max_length=100)
    template_desc: str | None = Field(None, max_length=500)
    template: str = Field(..., min_length=1)
    # 这列是 varchar，原样存
    variables: str | None = None
    # `model_config` 是 Pydantic v2 的保留属性名，不能直接做字段名。
    # 用 `model_config_json` + 显式 alias，对前端仍然是 `modelConfig`。
    model_config_json: str | None = Field(None, alias="modelConfig")
    tool_config: str | None = None
    tags: str | None = Field(None, max_length=500)


class PromptTemplateOut(CamelModel):
    id: str
    prompt_template_key: str
    tags: str | None = None
    template_desc: str | None = None
    template: str
    variables: str | None = None
    model_config_json: str = Field(
        "{}",
        alias="modelConfig",
        # `from_attributes` 时 Pydantic 会按 alias 去找 ORM 属性，而 ORM 上的
        # 属性名是 `model_config`。只给 alias 会导致**永远取到默认值 "{}"**
        # （不报错，静默丢字段）。validation_alias 两个都认才正确。
        validation_alias=AliasChoices("modelConfig", "model_config"),
    )
    tool_config: str = "{}"
    create_time: datetime | None = None
    update_time: datetime | None = None

    @field_validator("id", mode="before")
    @classmethod
    def _id_to_str(cls, v: Any) -> str:
        return str(v)

    @field_validator("model_config_json", "tool_config", mode="before")
    @classmethod
    def _jsonb_to_text(cls, v: Any) -> str:
        return to_json_text(v)


class PromptRunIn(CamelModel):
    """调试运行入参（对齐 Java `PromptRunDTO`）。

    `template` 为空时按 `promptKey + version` 去库里的版本取；
    两者都空直接 400。
    """

    prompt_key: str | None = None
    version: str | None = None
    template: str | None = None
    # `model_config` 是 Pydantic v2 的保留属性名，不能直接做字段名。
    # 用 `model_config_json` + 显式 alias，对前端仍然是 `modelConfig`。
    model_config_json: str | None = Field(None, alias="modelConfig")
    variables: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _need_source(self) -> PromptRunIn:
        if not (self.template or "").strip() and not (
            (self.prompt_key or "").strip() and (self.version or "").strip()
        ):
            raise ValueError("要么传 template，要么同时传 promptKey 和 version")
        return self
