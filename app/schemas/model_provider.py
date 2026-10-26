"""模型供应商的请求/响应 schema。

三个**必须照抄 Java 的细节**，每一个错了前端都不报错、只是显示异常：

1. `apiKey` **只进不出**（Java: `@JsonProperty(access = WRITE_ONLY)`）。
   返回体里没这个字段，前端 `maskKey(p.apiKey)` 拿到 undefined 就走兜底显示。
   如果这里漏了、真把 key 返回了，等于把凭证敞开给任何能看列表的人。

2. `config` / `headersJson` / `extraJson` 要返回 **JSON 字符串**，不是对象。
   库里是 JSONB、SQLAlchemy 读出来是 dict，但前端是这样消费的：
       ```js
       function parseJsonObject(str) {
         if (!str || !str.trim()) return {}   // ← 传对象会 TypeError
         return JSON.parse(str)
       }
       ```
   `str.trim` 在 try 外面，传 dict 直接抛异常，整个管理页白屏。

3. `id` 返回**字符串**（Java: `@JsonSerialize(using = ToStringSerializer.class)`）。
   雪花 ID 是 19 位，超过 JS 的 `Number.MAX_SAFE_INTEGER`（2^53-1），
   用数字传过去末几位会被静默改写，再提交回来就更新错记录。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from pydantic import Field, field_validator

from app.schemas.common import CamelModel


def _to_json_text(value: Any) -> str:
    """JSONB 值 → JSON 字符串。dict/list 序列化，字符串原样返回。"""
    if value is None:
        return "{}"
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return "{}"


class ModelProviderIn(CamelModel):
    """创建 / 更新请求体（对齐 Java `ModelProviderDTO`）。

    `config` / `headersJson` / `extraJson` 前端传的是**字符串**（JSON 文本），
    这里保持 str 接收，落库前再解析成 JSONB —— 不要声明成 dict，
    否则 Pydantic 会尝试把 `"{}"` 转成 dict 而报校验错误。
    """

    # 更新时必传，创建时为空
    id: str | None = None
    name: str
    type: str
    api_key: str | None = None
    base_url: str | None = None
    default_model_id: str | None = None
    models_endpoint: str | None = None
    headers_json: str | None = None
    extra_json: str | None = None
    config: str | None = None

    @field_validator("id", mode="before")
    @classmethod
    def _id_to_str(cls, v: Any) -> str | None:
        return None if v is None else str(v)


class ModelProviderOut(CamelModel):
    """供应商响应体。**故意没有 api_key 字段** —— 见模块 docstring 第 1 条。"""

    id: str
    name: str | None = None
    type: str | None = None
    base_url: str | None = None
    models_endpoint: str | None = None
    headers_json: str = "{}"
    extra_json: str = "{}"
    config: str = "{}"
    status: str | None = None
    create_time: datetime | None = None
    update_time: datetime | None = None

    @field_validator("id", mode="before")
    @classmethod
    def _id_to_str(cls, v: Any) -> str:
        return str(v)

    @field_validator("headers_json", "extra_json", "config", mode="before")
    @classmethod
    def _jsonb_to_text(cls, v: Any) -> str:
        """JSONB(dict/list) → JSON 字符串。

        **必须用 mode="before" 的 validator，不能用 field_serializer**：
        `field_serializer` 只管"输出成 JSON"那一步，而 `model_validate(orm_obj)`
        这一步的**校验**发生在它之前 —— 字段声明成 `str`，喂进去一个 dict
        就直接 ValidationError（实测报 "Input should be a valid string"）。
        mode="before" 在校验之前把值转好，校验和输出就都对了。
        """
        return _to_json_text(v)


class ProviderModelOut(CamelModel):
    """`with-models` 里挂的模型条目（对齐 Java `Model` 实体的前端可用子集）。"""

    id: str
    model_id: str | None = None
    name: str | None = None
    type: str | None = None
    status: str | None = None
    dimension: int | None = None

    @field_validator("id", mode="before")
    @classmethod
    def _id_to_str(cls, v: Any) -> str:
        return str(v)


class ProviderWithModelsOut(CamelModel):
    id: str
    name: str | None = None
    type: str | None = None
    models: list[ProviderModelOut] = Field(default_factory=list)

    @field_validator("id", mode="before")
    @classmethod
    def _id_to_str(cls, v: Any) -> str:
        return str(v)


class FetchedModelOut(CamelModel):
    """`fetch-models` 的返回项（对齐 Java `FetchedModel`）。"""

    model_id: str
    type: str


class ModelProviderCheckIn(CamelModel):
    """表单态连通性检查（对齐 Java `ModelProviderCheckDTO`）。"""

    type: str
    api_key: str | None = None
    base_url: str | None = None
    model_id: str | None = None
    completions_path: str | None = None
