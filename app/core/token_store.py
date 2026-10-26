"""Token 会话存储（Redis，对照 Java Sa-Token 的"服务端可吊销"语义）。

选**不透明随机串 + Redis** 而不是 JWT：

- logout 有真实语义（删 key 即吊销），JWT 得靠黑名单打补丁；
- Java Sa-Token 本身就是"服务端会话"模型，语义对齐，混跑期心智一致；
- 单机部署，Redis 已经是依赖（workflow 挂起快照也用它）。

key：`lengbot-py:token:{token}` → userId（string），TTL 30 天（Java 同值）。
前缀独立于 Java 的 `sa-token:` / `lengbot:`，互不干扰。
"""

from __future__ import annotations

import logging
import uuid

import redis.asyncio as aioredis

from app.core.config import settings

logger = logging.getLogger(__name__)

_PREFIX = "lengbot-py:token:"

_pool: aioredis.Redis | None = None


def _client() -> aioredis.Redis:
    global _pool
    if _pool is None:
        _pool = aioredis.from_url(
            settings.redis_url, decode_responses=True, socket_timeout=5
        )
    return _pool


def new_token() -> str:
    return "lbt_" + uuid.uuid4().hex


async def save(token: str, user_id: int) -> None:
    """写入会话。Redis 挂了只 log 不抛 —— 认证开关开着时 save 失败
    会让这个 token 校验不到（登录等效失败），但不该把整个登录请求炸掉。"""
    try:
        await _client().set(_PREFIX + token, str(user_id), ex=settings.token_ttl_seconds)
    except Exception as exc:  # noqa: BLE001
        logger.error("[TokenStore] 写入失败（该 token 将无法通过校验）: %s", exc)


async def resolve(token: str) -> int | None:
    try:
        raw = await _client().get(_PREFIX + token)
    except Exception as exc:  # noqa: BLE001
        logger.error("[TokenStore] 读取失败: %s", exc)
        return None
    return int(raw) if raw else None


async def revoke(token: str) -> None:
    try:
        await _client().delete(_PREFIX + token)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[TokenStore] 吊销失败: %s", exc)


def shared_client() -> aioredis.Redis:
    """给其他模块（如 token 预算计数器）复用同一个连接池。

    各模块自建 `aioredis.from_url` 也能跑，但每份 pool 自带连接数，
    叠多了会撞 Redis maxclients —— 共享一份池是本分。
    """
    return _client()
