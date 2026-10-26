"""Token 预算 5 接口验证（对齐前端 `src/api/tokenBudget.js`）。

重点验证这些**静默失败**：

1. **时间窗口是自然日切**：`[today-(N-1) 的 0 点, 明天 0点)`，end 开区间 ——
   写成"now - N 天"会把昨天同一时刻之前的数据漏掉一半。
2. **ranking 仅管理员**：这是他人的消耗数据，普通用户可查 = 越权看别人账单。
3. **配置落 system_config 的 key 名**：`llm.token.user.dailyLimit` 等三个，
   PUT 缺校验（负数/非数字）会让 checkBudget 的 parseLong 静默回落默认值 ——
   管理员"改了限额"实际没生效。
4. **排行榜 username = nickname 优先**，缺 nickname 回落 username。
5. **用 llm_trace 表聚合**（Python 有意不走 Java 的 Redis 计数器）：
   测试造 llm_trace 行验证聚合链路，today / 7d / 30d 三个窗口数字要自洽
   （7d ⊇ today，30d ⊇ 7d）。

用法：
    python -m scripts.token_budget_check
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime

import httpx
from sqlalchemy import text

from app.core.db import SessionLocal
from app.core.snowflake import new_id

BASE = "http://127.0.0.1:18080"
failures = 0
_STAMP = str(int(time.time()))[-6:]
_UID = 2090273750973657090
_TRACE_IDS: list[int] = []
_CFG_KEYS = ("llm.token.user.dailyLimit", "llm.token.global.dailyLimit",
             "llm.token.singleCallLimit")
_OLD_CFG: dict[str, str | None] = {}


def ok(label: str, cond: bool, detail: str = "") -> None:
    global failures
    if not cond:
        failures += 1
    print(f"  {'✅' if cond else '❌'} {label}" + (f"  → {detail}" if detail else ""))


async def api(c: httpx.AsyncClient, method: str, path: str, **kw) -> dict:
    resp = await c.request(method, BASE + path, **kw)
    try:
        return resp.json()
    except Exception:  # noqa: BLE001
        return {"code": resp.status_code, "message": resp.text[:200]}


def data(body: dict) -> dict | list | None:
    return body.get("data")


async def cleanup() -> None:
    """删测试 trace；system_config 的三个 key 由 restore_config 按备份还原。"""
    async with SessionLocal() as db:
        await db.execute(
            text("DELETE FROM llm_trace WHERE request_id LIKE 'zz-tb-%'"),
        )
        await db.commit()


async def seed_traces() -> None:
    """造 4 条 llm_trace：dev 用户今日 2 条 + 历史一条；不存在用户今日一条。"""
    now = datetime.now()
    rows = [
        # (request_id, user_id, total_tokens, days_ago)
        (f"zz-tb-{_STAMP}-a", _UID, 1000, 0),
        (f"zz-tb-{_STAMP}-b", _UID, 500, 0),
        (f"zz-tb-{_STAMP}-c", _UID, 300, 3),  # 3 天前：进 7d/30d，不进 today
        (f"zz-tb-{_STAMP}-d", 123456789, 2000, 0),  # 其他用户今日
    ]
    async with SessionLocal() as db:
        for rid, uid, tokens, days_ago in rows:
            tid = new_id()
            _TRACE_IDS.append(tid)
            await db.execute(text(
                "INSERT INTO llm_trace (id, request_id, user_id, status, input_tokens, "
                "output_tokens, total_tokens, tool_call_count, total_duration_ms, "
                "first_token_ms, prompt_cache_hit_tokens, create_time, trace_source) "
                "VALUES (:id, :rid, :uid, 'completed', 0, :tokens, :tokens, 0, 0, 0, 0, "
                ":ct, 'chat')"),
                {"id": tid, "rid": rid, "uid": uid, "tokens": tokens,
                 "ct": now - __import__("datetime").timedelta(days=days_ago)},
            )
        await db.commit()


async def backup_config() -> None:
    async with SessionLocal() as db:
        for key in _CFG_KEYS:
            row = (await db.execute(
                text("SELECT config_value FROM system_config WHERE config_key=:k"), {"k": key}
            )).scalar_one_or_none()
            _OLD_CFG[key] = row


async def restore_config() -> None:
    async with SessionLocal() as db:
        for key, val in _OLD_CFG.items():
            if val is None:
                await db.execute(text("DELETE FROM system_config WHERE config_key=:k"), {"k": key})
            else:
                await db.execute(
                    text("UPDATE system_config SET config_value=:v WHERE config_key=:k"),
                    {"v": val, "k": key},
                )
        await db.commit()


async def main() -> int:
    await cleanup()
    await backup_config()

    async with httpx.AsyncClient(timeout=60, trust_env=False) as c:
        # ⚠️ 基线必须在 seed_traces 之前取，否则种子值算进"基线"
        base_usage = data(await api(c, "GET", "/api/system-config/token-budget/my-usage")) or {}
        base_7d = base_usage.get("last7dUsed") or 0
        base_today = base_usage.get("userUsed") or 0
        base_rank = data(await api(c, "GET", "/api/system-config/token-budget/ranking?range=7d")) or []
        base_rank_me = next((x for x in base_rank if x.get("userId") == str(_UID)), {}).get("usedTokens") or 0

    await seed_traces()

    async with httpx.AsyncClient(timeout=60, trust_env=False) as c:
        print("\n[1] 配置读写（PUT 仅管理员）")

        r = await api(c, "GET", "/api/system-config/token-budget/config")
        d = data(r) or {}
        ok("config 三键（默认值 1M/10M/32k）",
           d.get("userDailyLimit") == 1_000_000 and d.get("globalDailyLimit") == 10_000_000
           and d.get("singleCallLimit") == 32_000, str(d))

        r = await api(c, "PUT", "/api/system-config/token-budget/config",
                      json={"userDailyLimit": 888_888})
        ok("PUT 配置成功（admin）", r.get("code") == 200, str(r)[:120])
        r = await api(c, "GET", "/api/system-config/token-budget/config")
        ok("改后回读生效", (data(r) or {}).get("userDailyLimit") == 888_888,
           str((data(r) or {}).get("userDailyLimit")))

        r = await api(c, "PUT", "/api/system-config/token-budget/config",
                      json={"userDailyLimit": -5})
        ok("负数被拒", r.get("code") == 400, str(r)[:100])
        r = await api(c, "PUT", "/api/system-config/token-budget/config",
                      json={"userDailyLimit": "abc"})
        ok("非数字被拒（否则 parseLong 静默回落默认）", r.get("code") == 400, str(r)[:100])

        r = await api(c, "PUT", "/api/system-config/token-budget/config",
                      json={"globalDailyLimit": 20_000_000, "singleCallLimit": 40_000})
        ok("部分字段更新不动其他键", r.get("code") == 200)

        print("\n[2] 统计 / 排行（llm_trace 聚合）")

        r = await api(c, "GET", "/api/system-config/token-budget/my-usage")
        d = data(r) or {}
        ok("my-usage 四键", {"userUsed", "userLimit", "last7dUsed", "date"} <= set(d.keys()),
           str(d))
        ok("userUsed = 基线+1500（种子两条）", d.get("userUsed") == base_today + 1500,
           f"{d.get('userUsed')} vs base={base_today}")
        ok("userLimit 用的是刚改的 888888", d.get("userLimit") == 888_888, str(d.get("userLimit")))
        ok("last7dUsed = 基线+1800（今日1500+3天前300）",
           d.get("last7dUsed") == base_7d + 1800,
           f"{d.get('last7dUsed')} vs base={base_7d}")
        ok("date 是今天", d.get("date") == time.strftime("%Y-%m-%d"), str(d.get("date")))

        r = await api(c, "GET", "/api/system-config/token-budget/stats")
        d = data(r) or {}
        ok("stats 三键", {"globalUsed", "globalLimit", "date"} <= set(d.keys()), str(d))
        ok("globalUsed ≥ 3500（种子 + 库里真实今日消耗）",
           d.get("globalUsed") >= 3500, str(d.get("globalUsed")))
        ok("globalLimit = 20000000（刚改的）", d.get("globalLimit") == 20_000_000)

        r = await api(c, "GET", "/api/system-config/token-budget/ranking?range=today")
        items = data(r) or []
        ok("today 排行含 dev 用户", any(x.get("userId") == str(_UID) for x in items),
           str(items[:2])[:200])
        me = next((x for x in items if x.get("userId") == str(_UID)), {})
        ok("dev 今日 = 基线+1500", me.get("usedTokens") == base_today + 1500,
           f"{me.get('usedTokens')} vs base={base_today}")
        ok("username 是 nickname 优先（dev 用户 nickname=admin）",
           me.get("username") == "admin", str(me.get("username")))
        ok("降序排列", items and items[0].get("usedTokens") >= items[-1].get("usedTokens", 0),
           str([x.get("usedTokens") for x in items[:3]]))

        r = await api(c, "GET", "/api/system-config/token-budget/ranking?range=7d")
        me7 = next((x for x in (data(r) or []) if x.get("userId") == str(_UID)), {})
        ok("7d 窗口 = 基线+1800（含 3 天前那条）",
           me7.get("usedTokens") == base_rank_me + 1800,
           f"{me7.get('usedTokens')} vs base={base_rank_me}")

        r = await api(c, "GET", "/api/system-config/token-budget/ranking?range=30d&limit=1")
        ok("30d + limit=1 生效", len(data(r) or []) == 1, str(len(data(r) or [])))

        r = await api(c, "GET", "/api/system-config/token-budget/ranking?range=bogus")
        ok("非法 range 400", r.get("code") == 400, str(r)[:100])

    await restore_config()
    await cleanup()
    print(f"\n{'=' * 60}")
    print(f"失败 {failures} 项")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
