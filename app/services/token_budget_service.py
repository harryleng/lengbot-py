"""Token 预算控制（对照 Java `TokenBudgetService` 的查询面）。

## 与 Java 的口径

**计数器已接入对话流**（record_usage / check_budget，本文件下半部分）：
key 口径与 Java 完全一致（`lengbot:token_budget:{scope}:{id}:{date}`，TTL 25h），
两边连同一台 Redis 实例 —— 混跑期消耗累加在同一个计数器上，限额不会被换后端绕过。

本文件上半部分的查询接口（my-usage / ranking / stats）仍走 llm_trace 表聚合：

- 历史范围反正要走 DB（Redis 只有当日计数器）；
- 数据口径一致：每次 LLM 调用都会落一条 llm_trace，SUM(total_tokens) 就是真消耗；
- 差异只剩"正在进行的本次调用"不计入 —— 对排行榜无影响。

## 配置存储

三个 key 存 `system_config`：`llm.token.user.dailyLimit` / `llm.token.global.dailyLimit` /
`llm.token.singleCallLimit`，缺省 1,000,000 / 10,000,000 / 32,000（Java DEFAULT_* 同值）。

## 时间窗口（自然日切）

`[today-(N-1) 的 0 点, 明天 0点)` —— end 是**开区间**（不含明天），
SQL 里 `create_time >= begin AND create_time < end`，照抄。
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entities import SystemConfig, User

logger = logging.getLogger(__name__)

KEY_USER_LIMIT = "llm.token.user.dailyLimit"
KEY_GLOBAL_LIMIT = "llm.token.global.dailyLimit"
KEY_SINGLE_LIMIT = "llm.token.singleCallLimit"

DEFAULT_USER_DAILY_LIMIT = 1_000_000
DEFAULT_GLOBAL_DAILY_LIMIT = 10_000_000
DEFAULT_SINGLE_CALL_LIMIT = 32_000

RANGE_DAYS = {"today": 1, "7d": 7, "14d": 14, "30d": 30}


def _window(range_name: str) -> tuple[datetime, datetime]:
    days = RANGE_DAYS.get(range_name)
    if days is None:
        raise ValueError(f"不支持的时间范围: {range_name}")
    today = date.today()
    begin = datetime.combine(today - timedelta(days=days - 1), time.min)
    end = datetime.combine(today + timedelta(days=1), time.min)
    return begin, end


class TokenBudgetService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # ---------------- 配置 ----------------
    async def _config_value(self, key: str, default: int) -> int:
        row = (
            await self.db.execute(
                select(SystemConfig.config_value).where(SystemConfig.config_key == key)
            )
        ).scalar_one_or_none()
        if row is None or not str(row).strip():
            return default
        try:
            return int(str(row).strip())
        except ValueError:
            logger.warning("[TokenBudget] 配置 %s 不是数字: %s，用默认", key, row)
            return default

    async def get_config(self) -> dict:
        return {
            "userDailyLimit": await self._config_value(KEY_USER_LIMIT, DEFAULT_USER_DAILY_LIMIT),
            "globalDailyLimit": await self._config_value(KEY_GLOBAL_LIMIT, DEFAULT_GLOBAL_DAILY_LIMIT),
            "singleCallLimit": await self._config_value(KEY_SINGLE_LIMIT, DEFAULT_SINGLE_CALL_LIMIT),
        }

    async def update_config(self, config: dict) -> None:
        """只更新传入的 key。值校验：非负整数。"""
        pairs = {
            "userDailyLimit": KEY_USER_LIMIT,
            "globalDailyLimit": KEY_GLOBAL_LIMIT,
            "singleCallLimit": KEY_SINGLE_LIMIT,
        }
        now = datetime.now()
        for field, key in pairs.items():
            if field not in config:
                continue
            try:
                value = int(str(config[field]))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{field} 必须是整数") from exc
            if value < 0:
                raise ValueError(f"{field} 不能为负数")
            row = (
                await self.db.execute(
                    select(SystemConfig).where(SystemConfig.config_key == key)
                )
            ).scalar_one_or_none()
            if row is None:
                self.db.add(
                    SystemConfig(config_key=key, config_value=str(value),
                                 description="Token 限额", create_time=now, update_time=now)
                )
            else:
                row.config_value = str(value)
                row.update_time = now
        await self.db.commit()

    # ---------------- 统计 ----------------
    async def get_global_stats(self) -> dict:
        begin, end = _window("today")
        used = await self._sum_tokens(begin, end)
        return {
            "globalUsed": used,
            "globalLimit": await self._config_value(KEY_GLOBAL_LIMIT, DEFAULT_GLOBAL_DAILY_LIMIT),
            "date": date.today().isoformat(),
        }

    async def get_my_usage(self, user_id: int) -> dict:
        begin, end = _window("today")
        today_used = await self._sum_tokens(begin, end, user_id=user_id)
        w7_begin, w7_end = _window("7d")
        last7d = await self._sum_tokens(w7_begin, w7_end, user_id=user_id)
        return {
            "userUsed": today_used,
            "userLimit": await self._config_value(KEY_USER_LIMIT, DEFAULT_USER_DAILY_LIMIT),
            "last7dUsed": last7d,
            "date": date.today().isoformat(),
        }

    async def get_user_ranking(self, range_name: str, limit: int) -> list[dict]:
        begin, end = _window(range_name)
        stmt = text(
            "SELECT user_id, COALESCE(SUM(total_tokens), 0) AS total_tokens "
            "FROM llm_trace "
            "WHERE create_time >= :begin AND create_time < :end "
            "  AND total_tokens > 0 AND user_id IS NOT NULL "
            "GROUP BY user_id ORDER BY total_tokens DESC LIMIT :limit"
        )
        rows = (await self.db.execute(stmt, {"begin": begin, "end": end, "limit": limit})).all()

        if not rows:
            return []
        user_ids = [int(r.user_id) for r in rows]
        users = (
            await self.db.execute(select(User).where(User.id.in_(user_ids)))
        ).scalars().all()
        user_map = {u.id: u for u in users}

        ranking = []
        for r in rows:
            uid = int(r.user_id)
            user = user_map.get(uid)
            ranking.append(
                {
                    "userId": str(uid),
                    "usedTokens": int(r.total_tokens),
                    # 昵称优先（Java：nickname 优先，其次 username）
                    "username": (user.nickname or user.username) if user else None,
                    "avatar": user.avatar if user else None,
                }
            )
        return ranking

    async def _sum_tokens(
        self, begin: datetime, end: datetime, user_id: int | None = None
    ) -> int:
        sql = (
            "SELECT COALESCE(SUM(total_tokens), 0) FROM llm_trace "
            "WHERE create_time >= :begin AND create_time < :end AND total_tokens > 0"
        )
        params: dict = {"begin": begin, "end": end}
        if user_id is not None:
            sql += " AND user_id = :uid"
            params["uid"] = user_id
        return int((await self.db.execute(text(sql), params)).scalar() or 0)


# ======================================================================
# Redis 计数器（recordUsage / checkBudget，对照 Java `TokenBudgetService`）
#
# **key 口径与 Java 完全一致**：`lengbot:token_budget:{scope}:{id}:{date}`。
# 两边连的是同一台 Redis 实例（db=9）——混跑期 Java 和 Python 的消耗
# 累加在**同一个计数器**上，限额不会被"换了个后端"绕过去。
#
# TTL 25h（Java KEY_TTL_HOURS 同值）：跨过自然日后旧 key 自然过期，
# 不需要显式清理。
# ======================================================================

BUDGET_KEY_PREFIX = "lengbot:token_budget:"
BUDGET_KEY_TTL_HOURS = 25


class BudgetExceeded(ValueError):
    """预算超限。消息文案与 Java checkBudget 抛的 BizException 一致。"""


def _today() -> str:
    return date.today().isoformat()


async def record_usage(user_id: int | None, total_tokens: int) -> None:
    """对话收尾记账：用户维度 + 全局维度各 INCR 一次。

    失败只 log 不抛 —— 记账丢了不影响本轮对话结果；
    Java 同位置（ChatServiceImpl 两条 persist 路径收尾）也是"尽力而为"。
    `user_id=None` 直接跳过（Java 同款判空）。
    """
    if not user_id or total_tokens <= 0:
        return
    from app.core.token_store import shared_client

    try:
        client = shared_client()
        user_key = f"{BUDGET_KEY_PREFIX}user:{user_id}:{_today()}"
        global_key = f"{BUDGET_KEY_PREFIX}global:{_today()}"
        async with client.pipeline(transaction=True) as pipe:
            pipe.incrby(user_key, total_tokens).expire(user_key, BUDGET_KEY_TTL_HOURS * 3600)
            pipe.incrby(global_key, total_tokens).expire(global_key, BUDGET_KEY_TTL_HOURS * 3600)
            await pipe.execute()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[TokenBudget] record_usage 失败（不影响对话）: %s", exc)


async def check_budget(db: AsyncSession, user_id: int, estimated_tokens: int) -> None:
    """三道闸：单次上限 → 用户日限额 → 全局日限额。超限抛 `BudgetExceeded`。

    只在「预估成本已知」的批处理路径调用（评测引擎，Java 同款只在
    EvalChatServiceImpl 里调）——普通对话无法预估输出，Java 也不查，
    只靠收尾 recordUsage 事后记账。
    """
    svc = TokenBudgetService(db)
    single_limit = await svc._config_value(KEY_SINGLE_LIMIT, DEFAULT_SINGLE_CALL_LIMIT)
    if estimated_tokens > single_limit:
        raise BudgetExceeded(f"单次调用 Token 超限: 预估 {estimated_tokens}, 上限 {single_limit}")

    from app.core.token_store import shared_client

    try:
        client = shared_client()
        today = _today()
        user_used = int(await client.get(f"{BUDGET_KEY_PREFIX}user:{user_id}:{today}") or 0)
        global_used = int(await client.get(f"{BUDGET_KEY_PREFIX}global:{today}") or 0)
    except Exception as exc:  # noqa: BLE001
        # Redis 挂了闸不住但也不能全停 —— 放行并记日志（fail-open）
        logger.warning("[TokenBudget] 读取计数失败，本次放行: %s", exc)
        return

    user_limit = await svc._config_value(KEY_USER_LIMIT, DEFAULT_USER_DAILY_LIMIT)
    if user_used + estimated_tokens > user_limit:
        raise BudgetExceeded(f"今日 Token 预算已用尽: 已用 {user_used}, 限额 {user_limit}")

    global_limit = await svc._config_value(KEY_GLOBAL_LIMIT, DEFAULT_GLOBAL_DAILY_LIMIT)
    if global_used + estimated_tokens > global_limit:
        raise BudgetExceeded("系统今日 Token 预算已用尽，请明天再试")
