"""认证收口（auth gate）：替换阶段 0 的"dev_user_id 写死"替身。

## 开关语义

`settings.auth_enabled`（env `AUTH_ENABLED`）：

- **False（开发态，默认）**：与收口前完全一致 —— login 不校验密码、
  身份恒为 dev_user_id。33 项回归脚本零改动。
- **True（上线态）**：login 真校验 BCrypt；请求必须携带有效凭据，否则 401；
  `dev-<uid>` 占位 token 一律无效。

两个状态共用同一套端点，**切换不改前端**（前端本来就把 token 塞
`Authorization` 头原样发上来）。

## 凭据解析顺序（auth_enabled=True 时）

1. `lbkey_` 开头 → API Key（sha256 反查 `api_key` 表，校验启用/过期）
2. 其他 → Redis token（`token_store.resolve`）

`Authorization` 头是**裸 token**（前端 `request.js` 的写法：`config.headers['Authorization'] = token`，
没有 Bearer 前缀）—— 兼容带 Bearer 前缀的写法但不强求。
"""

from __future__ import annotations

import contextvars
import hashlib
import logging
from datetime import datetime

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import token_store
from app.core.config import settings
from app.models.entities import ApiKey

logger = logging.getLogger(__name__)


class Unauthorized(Exception):
    """auth gate 拦截（HTTP 401）。"""


class RateLimited(Exception):
    """API Key 每分钟请求数超限（HTTP 429）。"""


class AgentForbidden(Exception):
    """API Key 无权访问该 Agent（HTTP 403，agent_ids 白名单）。"""


# API Key 请求期间把 key id 挂在请求上下文上，对话收尾凭它扣日配额
# （ContextVar 会随 asyncio.create_task 复制 —— 流式对话在子任务里也能读到）
current_api_key_id: "contextvars.ContextVar[int | None]" = contextvars.ContextVar(
    "current_api_key_id", default=None
)


async def resolve_user_id(request: Request, db: AsyncSession) -> int:
    """`get_current_user_id` 的真实现。

    异常路径只在本函数抛，调用方（deps）负责转 401。
    """
    if not settings.auth_enabled:
        # 开发态：行为与收口前逐字节一致
        return settings.dev_user_id

    raw = request.headers.get("Authorization") or ""
    token = raw.strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if not token:
        raise Unauthorized("未登录")

    # API Key 路径：lbkey_ 开头，sha256 反查（Java ApiKeyServiceImpl.authenticateWithDetails 同款）
    if token.startswith("lbkey_"):
        return await _resolve_api_key(request, db, token)

    user_id = await token_store.resolve(token)
    if user_id is None:
        raise Unauthorized("登录已过期，请重新登录")
    return user_id


async def _resolve_api_key(request: Request, db: AsyncSession, raw_key: str) -> int:
    """API Key 认证 + 限流 + Agent 作用域（Java `ApiKeyAuthInterceptor` 三步全移植）。"""
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    row = (
        await db.execute(
            select(ApiKey).where(ApiKey.key_hash == key_hash, ApiKey.is_enabled == 1)
        )
    ).scalars().first()
    if row is None:
        raise Unauthorized("无效的 API Key")
    if row.expires_at is not None and row.expires_at < datetime.now():
        raise Unauthorized("API Key 已过期")

    # 2. 请求频率限制（Java 拦截器第二步）
    from app.services.api_key_service import check_rate_limit

    if not await check_rate_limit(int(row.id), row.rate_limit or 60):
        raise RateLimited(f"请求过于频繁，限制 {row.rate_limit} 次/分钟")

    # 3. Agent 作用域校验（Java 只查 query 参数 agentId，body 里的不查 —— 照抄）
    if request.url.path.startswith("/api/chat"):
        agent_id = request.query_params.get("agentId")
        if agent_id and not _agent_allowed(row, agent_id):
            raise AgentForbidden("该 API Key 无权访问此 Agent")

    current_api_key_id.set(int(row.id))
    return int(row.user_id)


def _agent_allowed(row: ApiKey, agent_id: str) -> bool:
    """agent_ids 空列表 = 不限；非空 = 白名单（Java checkAgentScope 同款）。"""
    allowed = row.agent_ids or []
    return not allowed or str(agent_id) in {str(x) for x in allowed}
