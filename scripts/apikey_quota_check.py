"""API Key 配额/限流验证（apikey_quota_check）。

- 服务层直测：配额原子扣减（正常累加 / 跨天重置 / 超配额拒扣 / 0=不限额）、
  每分钟限流（Redis 分钟桶，第 rate_limit+1 次拒）；
- 上线态（AUTH_ENABLED=true，进程内 ASGI）：lbkey_ 认证放行、限流 429、
  Agent 作用域白名单 403、停用/过期 401、ContextVar 传 key id。
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta

os.environ["AUTH_ENABLED"] = "true"  # 必须在 import app 之前

import httpx
from sqlalchemy import text

from app.core.db import SessionLocal
from app.core.snowflake import new_id
from app.services.api_key_service import check_rate_limit, consume_quota

_STAMP = uuid.uuid4().hex[:8]
_UID = 2090273750973657090
_FAILED: list[str] = []


def ok(name: str, cond: bool, detail: str = "") -> None:
    mark = "✅" if cond else "❌"
    print(f"  {mark} {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        _FAILED.append(name)


async def make_key(
    daily_quota: int = 1000, rate_limit: int = 60,
    agent_ids: list | None = None, enabled: int = 1,
    expires_at: datetime | None = None,
) -> int:
    raw = f"lbkey_{uuid.uuid4().hex}{uuid.uuid4().hex[:16]}"
    key_hash = __import__("hashlib").sha256(raw.encode()).hexdigest()
    now = datetime.now()
    from app.models.entities import ApiKey

    async with SessionLocal() as db:
        row = ApiKey(
            id=new_id(), user_id=_UID, name=f"zz-k-{_STAMP}", key_prefix=raw[:12] + "****",
            key_hash=key_hash, permissions="chat", is_enabled=enabled,
            expires_at=expires_at, create_time=now, update_time=now, deleted=0,
            agent_ids=agent_ids, rate_limit=rate_limit, daily_quota=daily_quota,
            used_tokens=0,
        )
        db.add(row)
        await db.commit()
    return int(row.id), raw


async def get_row(key_id: int):
    from app.models.entities import ApiKey

    async with SessionLocal() as db:
        row = await db.get(ApiKey, key_id)
        return row


async def cleanup() -> None:
    async with SessionLocal() as db:
        await db.execute(text("DELETE FROM api_key WHERE name LIKE 'zz-k-%'"))
        await db.commit()
    from app.core.token_store import shared_client

    client = shared_client()
    keys = await client.keys("lengbot-py:apikey:rl:*")
    if keys:
        await client.delete(*keys)


async def _consume(kid: int, usage: int) -> bool:
    async with SessionLocal() as db:
        return await consume_quota(db, kid, usage)


async def check_quota() -> None:
    print("\n[1] 配额原子扣减")
    kid, _ = await make_key(daily_quota=1000)

    ok("首次扣 600", await _consume(kid, 600) is True)
    ok("再扣 300（同日累加）", await _consume(kid, 300) is True)
    row = await get_row(kid)
    ok("used_tokens=900", row.used_tokens == 900, str(row.used_tokens))
    ok("quota_reset_at=今天", row.quota_reset_at == datetime.now().date())

    ok("超配额扣 200 拒绝", await _consume(kid, 200) is False)
    row = await get_row(kid)
    ok("拒绝后 used_tokens 不变（原子 WHERE 防超扣）", row.used_tokens == 900)

    # 跨天重置：把 reset_at 改成昨天 → 再扣应重置为本次用量
    async with SessionLocal() as db:
        await db.execute(
            text("UPDATE api_key SET quota_reset_at = :y WHERE id = :i"),
            {"y": datetime.now().date() - timedelta(days=1), "i": kid},
        )
        await db.commit()
    ok("跨天后扣 100 → 重置语义", await _consume(kid, 100) is True)
    row = await get_row(kid)
    ok("重置后 used_tokens=100（非 1000）", row.used_tokens == 100, str(row.used_tokens))

    # 0 = 不限额
    kid0, _ = await make_key(daily_quota=0)
    ok("dailyQuota=0 不限额（扣 10^9 也 True）",
       await _consume(kid0, 10**9) is True)
    row = await get_row(kid0)
    ok("不限额时不写 used_tokens", row.used_tokens == 0, str(row.used_tokens))


async def check_rate() -> None:
    print("\n[2] 每分钟限流")
    kid, _ = await make_key(rate_limit=3)
    results = [await check_rate_limit(kid, 3) for _ in range(4)]
    ok("前 3 次放行", results[:3] == [True, True, True], str(results))
    ok("第 4 次拒绝", results[3] is False)
    ok("rate_limit=0 不限流", await check_rate_limit(new_id(), 0) is True)


async def check_gate() -> None:
    print("\n[3] 上线态门（AUTH_ENABLED=true，进程内 ASGI）")
    from httpx import ASGITransport

    from app.core import auth_gate
    from app.main import app

    transport = ASGITransport(app=app)

    async def call(path: str, key: str | None, method: str = "GET", **kw):
        headers = kw.pop("headers", {})
        if key:
            headers["Authorization"] = key
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.request(method, path, headers=headers, **kw)
        # ⚠️ 全站约定 HTTP 恒 200，错误码在 body.code（auth_gate_check 同款）
        body = r.json()
        return int(body.get("code") or 0), body

    kid_ok, raw_ok = await make_key(daily_quota=1000)
    kid_scope, raw_scope = await make_key(agent_ids=[1])
    kid_off, raw_off = await make_key(enabled=0)
    kid_exp, raw_exp = await make_key(expires_at=datetime.now() - timedelta(hours=1))

    code, _ = await call("/api/agents", raw_ok)
    ok("有效 lbkey_ 放行", code == 200, str(code))
    ok("ContextVar 记录了 key id", auth_gate.current_api_key_id.get() == kid_ok)

    code, _ = await call("/api/agents", "lbkey_invalid")
    ok("无效 key → body.code 401", code == 401, str(code))
    code, _ = await call("/api/agents", raw_off)
    ok("停用 key → 401", code == 401, str(code))
    code, _ = await call("/api/agents", raw_exp)
    ok("过期 key → 401", code == 401, str(code))

    # 作用域门只看 path 前缀 /api/chat + query 的 agentId（Java 同款）
    code, _ = await call("/api/chat/sessions?agentId=1", raw_scope)
    ok("白名单内 agent 放过作用域门", code == 200, str(code))
    code, _ = await call("/api/chat/sessions?agentId=2", raw_scope)
    ok("白名单外 agent → body.code 403", code == 403, str(code))
    code, _ = await call("/api/agents", raw_scope)
    ok("非 /api/chat 路径不查作用域", code == 200, str(code))

    # 限流门：rate_limit=2 的 key 打 3 次 → 第 3 次 429
    kid_rl, raw_rl = await make_key(rate_limit=2)
    codes = [(await call("/api/agents", raw_rl))[0] for _ in range(3)]
    ok("限流前 2 次 200 / 第 3 次 body.code 429", codes == [200, 200, 429], str(codes))

    # 无认证 → 401
    code, _ = await call("/api/agents", None)
    ok("无凭据 → body.code 401", code == 401, str(code))


async def main() -> int:
    try:
        await check_quota()
        await check_rate()
        await check_gate()
    finally:
        await cleanup()

    print(f"\n{'=' * 60}")
    if _FAILED:
        print(f"❌ 失败 {len(_FAILED)} 项: {_FAILED}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
