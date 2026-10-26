"""用户个人配置（`/api/user/preferences`）。

⚠️ 这张配置**没有独立表**，全部塞在 `users.config` 这个 JSONB 里，
和头像框、等级等个人资料字段共存。

所以更新时必须「先读出整个 config → 只改这 6 个 key → 整体写回」，
直接覆盖会把用户的头像框/等级抹掉（Java 注释原文：
"保留头像框/等级等不相关配置"）。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from app.schemas.common import CamelModel

# 与 Java ConfigKeys.User 完全一致的 key
K_LONG_MEMORY_ENABLED = "longMemoryEnabled"
K_LONG_MEMORY_AUTO_EXTRACT = "longMemoryAutoExtract"
K_LONG_MEMORY_INJECT_LIMIT = "longMemoryInjectLimit"
K_LONG_MEMORY_SCOPE = "longMemoryScope"
K_LONG_MEMORY_LLM_EXTRACT = "longMemoryLlmExtract"
K_MEMORY_EXTRACT_PROVIDER_ID = "memoryExtractProviderId"

DEFAULT_ENABLED = False
DEFAULT_AUTO_EXTRACT = False
DEFAULT_INJECT_LIMIT = 6
DEFAULT_SCOPE = "user"
DEFAULT_LLM_EXTRACT = True
INJECT_LIMIT_MIN = 1
INJECT_LIMIT_MAX = 15


class UserPreferenceOut(CamelModel):
    long_memory_enabled: bool = DEFAULT_ENABLED
    long_memory_auto_extract: bool = DEFAULT_AUTO_EXTRACT
    long_memory_inject_limit: int = DEFAULT_INJECT_LIMIT
    long_memory_scope: str = DEFAULT_SCOPE
    long_memory_llm_extract: bool = DEFAULT_LLM_EXTRACT
    memory_extract_provider_id: str | None = None


class UserPreferenceUpdateIn(BaseModel):
    """前端按 Java DTO 发 camelCase（`longMemoryInjectLimit`），
    这里两个方向都收 —— 少了 alias 的话请求体会被静默忽略，
    表现是"保存成功但值没变"，很难查。"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    long_memory_enabled: bool | None = None
    long_memory_auto_extract: bool | None = None
    long_memory_inject_limit: int | None = Field(default=None, ge=INJECT_LIMIT_MIN, le=INJECT_LIMIT_MAX)
    long_memory_scope: str | None = None
    long_memory_llm_extract: bool | None = None
    memory_extract_provider_id: str | None = None
