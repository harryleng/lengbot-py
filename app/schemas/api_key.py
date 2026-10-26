"""API Key 的出参/入参。

契约要点（照抄 Java `ApiKeyServiceImpl`）：

- 明文密钥只在 `create` / `regenerate` 的响应里出现一次，字段名是 **`secret`**，
  实体本身叫 `apiKey` —— 两个都在同一个 Map 里返回。
- `permissions` 出参是中文 desc（"仅对话"/"完全访问"），因为 Java 把
  `@JsonValue` 标在了 `desc` 上、`@EnumValue` 标在 `code` 上（落库存 code）。
- `keyPrefix` 是 `lbkey_` 开头的前 12 位 + `****`，列表页靠它认钥匙。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from app.schemas.common import CamelModel

PERMISSION_CODE_TO_DESC = {
    "chat": "仅对话",
    "full": "完全访问",
}


class ApiKeyCreateIn(BaseModel):
    """前端发 camelCase（expiresAt / agentIds / rateLimit / dailyQuota）。"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    name: str = Field(min_length=1, max_length=64)
    permissions: str | None = None
    expires_at: str | None = None
    agent_ids: list[str] | None = None
    rate_limit: int | None = None
    daily_quota: int | None = None


class ApiKeyOut(CamelModel):
    id: str
    user_id: str | None = None
    name: str | None = None
    key_prefix: str | None = None
    permissions: str | None = None
    is_enabled: int | None = None
    last_used_at: datetime | None = None
    expires_at: datetime | None = None
    create_time: datetime | None = None
    update_time: datetime | None = None
    agent_ids: list[str] | None = None
    rate_limit: int | None = None
    daily_quota: int | None = None
    used_tokens: int | None = None


class ApiKeyWithSecret(CamelModel):
    """创建/重新生成的响应：`{ apiKey, secret }`。"""

    api_key: ApiKeyOut | None = None
    secret: str | None = None
