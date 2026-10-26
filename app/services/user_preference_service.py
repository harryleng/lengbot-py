"""用户个人偏好（存 `users.config` JSONB，没有独立表）。

⚠️ 最关键的约束：**写回必须整列覆盖，但只能改这 6 个 key**。
`users.config` 里还躺着头像框、等级等个人资料字段，
直接 `config = {...新值}` 会把它们抹掉。

所以流程固定是「读整列 → 按 key 覆盖 → 整列写回」。
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entities import User
from app.schemas.user_preference import (
    DEFAULT_AUTO_EXTRACT,
    DEFAULT_ENABLED,
    DEFAULT_INJECT_LIMIT,
    DEFAULT_LLM_EXTRACT,
    DEFAULT_SCOPE,
    INJECT_LIMIT_MAX,
    INJECT_LIMIT_MIN,
    K_LONG_MEMORY_AUTO_EXTRACT,
    K_LONG_MEMORY_ENABLED,
    K_LONG_MEMORY_INJECT_LIMIT,
    K_LONG_MEMORY_LLM_EXTRACT,
    K_LONG_MEMORY_SCOPE,
    K_MEMORY_EXTRACT_PROVIDER_ID,
    UserPreferenceOut,
    UserPreferenceUpdateIn,
)

logger = logging.getLogger(__name__)


class UserPreferenceService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get(self, user_id: int) -> UserPreferenceOut:
        return _to_out(await self._load_config(user_id))

    async def update(self, user_id: int, req: UserPreferenceUpdateIn) -> UserPreferenceOut:
        user, config = await self._load_config(user_id, need_user=True)
        if req.long_memory_enabled is not None:
            config[K_LONG_MEMORY_ENABLED] = req.long_memory_enabled
        if req.long_memory_auto_extract is not None:
            config[K_LONG_MEMORY_AUTO_EXTRACT] = req.long_memory_auto_extract
        if req.long_memory_inject_limit is not None:
            config[K_LONG_MEMORY_INJECT_LIMIT] = max(
                INJECT_LIMIT_MIN, min(INJECT_LIMIT_MAX, req.long_memory_inject_limit)
            )
        if req.long_memory_scope is not None:
            config[K_LONG_MEMORY_SCOPE] = _normalize_scope(req.long_memory_scope)
        if req.long_memory_llm_extract is not None:
            config[K_LONG_MEMORY_LLM_EXTRACT] = req.long_memory_llm_extract
        if req.memory_extract_provider_id is not None:
            config[K_MEMORY_EXTRACT_PROVIDER_ID] = req.memory_extract_provider_id

        # SQLAlchemy 对 JSONB 的原地修改不会被脏检查捕获，必须显式再赋值一次
        user.config = dict(config)
        await self.db.commit()
        return _to_out(config)

    async def _load_config(self, user_id: int, need_user: bool = False):
        stmt = select(User).where(User.id == user_id)
        user = (await self.db.execute(stmt)).scalars().first()
        if user is None:
            raise LookupError("用户不存在")
        config = dict(user.config or {})
        return (user, config) if need_user else config


def _to_out(config: dict) -> UserPreferenceOut:
    return UserPreferenceOut(
        long_memory_enabled=_bool(config.get(K_LONG_MEMORY_ENABLED), DEFAULT_ENABLED),
        long_memory_auto_extract=_bool(
            config.get(K_LONG_MEMORY_AUTO_EXTRACT), DEFAULT_AUTO_EXTRACT
        ),
        long_memory_inject_limit=_int(
            config.get(K_LONG_MEMORY_INJECT_LIMIT),
            DEFAULT_INJECT_LIMIT,
            INJECT_LIMIT_MIN,
            INJECT_LIMIT_MAX,
        ),
        long_memory_scope=_normalize_scope(
            str(config.get(K_LONG_MEMORY_SCOPE, DEFAULT_SCOPE))
        ),
        long_memory_llm_extract=_bool(
            config.get(K_LONG_MEMORY_LLM_EXTRACT), DEFAULT_LLM_EXTRACT
        ),
        memory_extract_provider_id=_str_or_none(config.get(K_MEMORY_EXTRACT_PROVIDER_ID)),
    )


def _bool(value, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() == "true"
    return default


def _int(value, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


def _str_or_none(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalize_scope(scope) -> str:
    """Java：`"agent".equalsIgnoreCase(scope) ? "agent" : "user"` —— 非 agent 一律回落 user。"""
    return "agent" if str(scope).lower() == "agent" else DEFAULT_SCOPE
