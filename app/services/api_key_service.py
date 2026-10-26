"""API Key 管理（对照 Java `ApiKeyServiceImpl`）。

安全口径（别改）：

- 库里**只存 sha256**，明文只在创建/重新生成那一秒出现在响应里。
- `keyPrefix` = 明文前 12 位 + `****`（Java 是 `rawKey.substring(0,12) + "****"`），
  列表页用它认钥匙；不要因为"看着像密钥泄露"就改成更短，前端有展示宽度依赖。
- 每个接口都要带 `user_id` 校验归属 —— Java 的 `getByIdAndCheckOwner`。
  少了这一步就是 IDOR：A 用户能删掉 B 用户的 key。
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.snowflake import new_id
from app.models.entities import ApiKey
from app.schemas.api_key import PERMISSION_CODE_TO_DESC, ApiKeyOut, ApiKeyWithSecret

logger = logging.getLogger(__name__)

KEY_PREFIX = "lbkey_"


def _gen_raw_key() -> str:
    """Java：`lbkey_` + 32 位 uuid + 16 位 uuid 片段。"""
    return (
        KEY_PREFIX
        + uuid.uuid4().hex
        + uuid.uuid4().hex[:16]
    )


class ApiKeyService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def list_by_user(self, user_id: int) -> list[ApiKeyOut]:
        stmt = (
            select(ApiKey)
            .where(ApiKey.user_id == user_id)
            .where(ApiKey.deleted == 0)
            .order_by(ApiKey.create_time.desc())
        )
        rows = (await self.db.execute(stmt)).scalars().all()
        return [_to_out(r) for r in rows]

    async def create(
        self,
        user_id: int,
        name: str,
        permissions: str | None,
        expires_at: str | None,
        agent_ids: list[str] | None,
        rate_limit: int | None,
        daily_quota: int | None,
    ) -> ApiKeyWithSecret:
        raw = _gen_raw_key()
        now = datetime.now()
        row = ApiKey(
            id=new_id(),
            user_id=user_id,
            name=name,
            key_prefix=raw[:12] + "****",
            key_hash=hashlib.sha256(raw.encode()).hexdigest(),
            permissions=(permissions or "chat").lower(),
            is_enabled=1,
            create_time=now,
            update_time=now,
            deleted=0,
            agent_ids=agent_ids or None,
            rate_limit=rate_limit if rate_limit is not None else 60,
            daily_quota=daily_quota if daily_quota is not None else 100000,
            used_tokens=0,
            expires_at=_parse_dt(expires_at),
        )
        self.db.add(row)
        await self.db.commit()
        await self.db.refresh(row)
        logger.info("[ApiKey] 创建 userId=%s name=%s", user_id, name)
        return ApiKeyWithSecret(api_key=_to_out(row), secret=raw)

    async def toggle(self, key_id: int, user_id: int) -> None:
        row = await self._get_owned(key_id, user_id)
        row.is_enabled = 0 if row.is_enabled == 1 else 1
        row.update_time = datetime.now()
        await self.db.commit()

    async def delete(self, key_id: int, user_id: int) -> None:
        row = await self._get_owned(key_id, user_id)
        row.deleted = 1
        row.update_time = datetime.now()
        await self.db.commit()

    async def regenerate(self, key_id: int, user_id: int) -> ApiKeyWithSecret:
        row = await self._get_owned(key_id, user_id)
        raw = _gen_raw_key()
        row.key_hash = hashlib.sha256(raw.encode()).hexdigest()
        row.key_prefix = raw[:12] + "****"
        row.update_time = datetime.now()
        await self.db.commit()
        await self.db.refresh(row)
        return ApiKeyWithSecret(api_key=_to_out(row), secret=raw)

    async def _get_owned(self, key_id: int, user_id: int) -> ApiKey:
        stmt = (
            select(ApiKey)
            .where(ApiKey.id == key_id)
            .where(ApiKey.user_id == user_id)
            .where(ApiKey.deleted == 0)
        )
        row = (await self.db.execute(stmt)).scalars().first()
        if row is None:
            raise LookupError("API Key 不存在或无权限")
        return row


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _to_out(row: ApiKey) -> ApiKeyOut:
    return ApiKeyOut(
        id=str(row.id),
        user_id=str(row.user_id),
        name=row.name,
        key_prefix=row.key_prefix,
        # 落库是 code，出参是 desc（Java @JsonValue 标在 desc 上）
        permissions=PERMISSION_CODE_TO_DESC.get(row.permissions, row.permissions),
        is_enabled=row.is_enabled,
        last_used_at=row.last_used_at,
        expires_at=row.expires_at,
        create_time=row.create_time,
        update_time=row.update_time,
        agent_ids=list(row.agent_ids) if row.agent_ids else None,
        rate_limit=row.rate_limit,
        daily_quota=row.daily_quota,
        used_tokens=row.used_tokens,
    )


# ======================================================================
# 请求面：限流 + 配额扣减（对照 Java `ApiKeyAuthInterceptor` + `checkAndConsumeQuota`）
# ======================================================================

RATE_LIMIT_KEY_PREFIX = "lengbot-py:apikey:rl:"


async def check_rate_limit(key_id: int, rate_limit: int) -> bool:
    """每分钟请求数限流（Redis 分钟桶，Java 同款：TTL 120s）。

    Redis 不可用 → 放行（fail-open，Java 同款语义）。
    """
    if rate_limit <= 0:
        return True
    from app.core.token_store import shared_client

    minute_bucket = int(datetime.now().timestamp() // 60)
    key = f"{RATE_LIMIT_KEY_PREFIX}{key_id}:{minute_bucket}"
    try:
        client = shared_client()
        count = await client.incr(key)
        if count == 1:
            await client.expire(key, 120)
        return count <= rate_limit
    except Exception as exc:  # noqa: BLE001
        logger.warning("[API Key] Redis 限流检查失败，放行: %s", exc)
        return True


async def consume_quota(db: AsyncSession, key_id: int, token_usage: int) -> bool:
    """日配额原子扣减（Java mapper `checkAndConsumeQuota` 的 SQL 逐字移植）。

    单条 UPDATE 消除 TOCTOU：CASE WHEN 处理跨天重置（**IS DISTINCT FROM
    做 NULL 安全比较** —— 新建的 key quota_reset_at 是 NULL，普通 != 对 NULL
    恒 UNKNOWN，首扣会静默 0 行），WHERE 约束防超扣。
    返回 False = 配额不足（本次未扣减）。

    Java 侧对话收尾对返回值**不做拦截**（只扣不拦，超配额调用照样完成），
    Python 保持同语义：调用方记录日志即可。
    """
    row = await db.get(ApiKey, key_id)
    if row is None:
        return True
    daily_quota = row.daily_quota or 0
    if daily_quota <= 0:
        return True  # 0 = 不限额（Java 同款）

    today = datetime.now().date()
    from sqlalchemy import text

    result = await db.execute(
        text(
            # ⚠️ asyncpg 对同一 :usage 在 bigint 算术与比较上下文混用会推断成 text
            # （bigint + text 报错），必须显式 cast
            "UPDATE api_key SET "
            "used_tokens = CASE "
            "  WHEN quota_reset_at IS DISTINCT FROM :today THEN CAST(:usage AS bigint) "
            "  ELSE used_tokens + CAST(:usage AS bigint) END, "
            "quota_reset_at = :today "
            "WHERE id = :id AND ("
            "  (quota_reset_at IS DISTINCT FROM :today "
            "      AND CAST(:usage AS bigint) <= CAST(:quota AS int)) "
            "  OR (quota_reset_at IS NOT DISTINCT FROM :today "
            "      AND used_tokens + CAST(:usage AS bigint) <= CAST(:quota AS int)))"
        ),
        {"today": today, "usage": token_usage, "id": key_id, "quota": daily_quota},
    )
    await db.commit()
    return (result.rowcount or 0) > 0
