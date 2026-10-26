"""Token 预算计数器验证（record_usage / check_budget / 对话流挂钩）。

直连 Redis + DB，不调真 LLM：

1. record_usage：INCR 两个维度 + TTL 生效；
2. check_budget 三道闸：单次上限 / 用户日限额 / 全局日限额（各闸文案与 Java 一致）；
3. 预算闸在 `call_llm` 最前 —— 单次上限调到 1 后，任何 prompt 都在 provider
   解析之前就被拒（不需要真 LLM 即可验证引擎路径整场终止语义）；
4. fail-open：Redis 不可达时 check_budget 放行（用错误 URL 验证）。

结算动作：恢复 system_config 三个限额原值、删除测试用户的计数器 key。
全局计数器里测试期间累加的几百 token 与真实用量混在一起，量级可忽略，
但 key 前缀按 user 隔离的测试 key 必须删干净。
"""

from __future__ import annotations

import asyncio
from datetime import date

from sqlalchemy import text

from app.core.db import SessionLocal
from app.services import rag_eval_engine
from app.services.token_budget_service import (
    BUDGET_KEY_PREFIX,
    BudgetExceeded,
    TokenBudgetService,
    check_budget,
    record_usage,
)

_STAMP = date.today().isoformat()
_TEST_UID = 9876543210  # 测试专用虚拟用户（不与真实用户撞）
_FAILED: list[str] = []


def ok(name: str, cond: bool, detail: str = "") -> None:
    mark = "✅" if cond else "❌"
    print(f"  {mark} {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        _FAILED.append(name)


def _user_key() -> str:
    return f"{BUDGET_KEY_PREFIX}user:{_TEST_UID}:{_STAMP}"


def _global_key() -> str:
    return f"{BUDGET_KEY_PREFIX}global:{_STAMP}"


async def _redis():
    from app.core.token_store import shared_client

    return shared_client()


async def check_record() -> None:
    print("\n[1] record_usage 记账")
    r = await _redis()
    before_user = int(await r.get(_user_key()) or 0)

    await record_usage(_TEST_UID, 500)
    await record_usage(_TEST_UID, 300)

    after_user = int(await r.get(_user_key()) or 0)
    after_global = int(await r.get(_global_key()) or 0)
    ok("用户维度 INCR 800", after_user == before_user + 800, f"{before_user} -> {after_user}")
    ok("全局维度同步累加", after_global >= 800, str(after_global))

    ttl = await r.ttl(_user_key())
    ok("TTL 25h 已设置", 0 < ttl <= 25 * 3600, f"ttl={ttl}")

    # 边界：0 / 负数 / None 用户一律跳过（Java recordUsage 判空同款）
    await record_usage(None, 100)
    await record_usage(_TEST_UID, 0)
    ok("user=None / tokens=0 不写", int(await r.get(_user_key()) or 0) == after_user)


_TOUCHED_KEYS = (
    "llm.token.user.dailyLimit",
    "llm.token.global.dailyLimit",
    "llm.token.singleCallLimit",
)


async def backup_config() -> dict[str, str | None]:
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                text("SELECT config_key, config_value FROM system_config "
                     "WHERE config_key = ANY(:keys)"),
                {"keys": list(_TOUCHED_KEYS)},
            )
        ).all()
    return {k: v for k, v in rows}


async def restore_config(backup: dict[str, str | None]) -> None:
    """三个 key 全量处理：backup 有的覆盖回原值，没有的删行 ——
    只覆盖会漏掉「原本无行」的场景，测试写的行就永久留下了（第一版踩过）。"""
    async with SessionLocal() as db:
        for key in _TOUCHED_KEYS:
            value = backup.get(key)
            if value is None:
                await db.execute(
                    text("DELETE FROM system_config WHERE config_key = :k"), {"k": key}
                )
            else:
                await db.execute(
                    text("UPDATE system_config SET config_value = :v WHERE config_key = :k"),
                    {"v": value, "k": key},
                )
        await db.commit()


async def check_gates() -> None:
    print("\n[2] check_budget 三道闸")
    async with SessionLocal() as db:
        # 闸 1：单次上限（预估 100 > 上限 50）
        await TokenBudgetService(db).update_config({"singleCallLimit": 50})
        try:
            await check_budget(db, _TEST_UID, 100)
            ok("单次上限拦截", False, "未抛错")
        except BudgetExceeded as exc:
            ok("单次上限拦截", "单次调用 Token 超限" in str(exc), str(exc))

        # 闸 2：用户日限额（上限调大到放行闸 1；计数器已有 800，预算 300 → 1100 > 1000）
        await TokenBudgetService(db).update_config({"singleCallLimit": 100000, "userDailyLimit": 1000})
        try:
            await check_budget(db, _TEST_UID, 300)
            ok("用户日限额拦截", False, "未抛错")
        except BudgetExceeded as exc:
            ok("用户日限额拦截", "今日 Token 预算已用尽" in str(exc), str(exc))

        # 放行：预算 100 → 900 ≤ 1000
        await check_budget(db, _TEST_UID, 100)
        ok("未超限放行", True)

        # 闸 3：全局日限额 —— 换一个干净用户（用户维度不限），只压全局
        await TokenBudgetService(db).update_config({"userDailyLimit": 100000000, "globalDailyLimit": 900})
        try:
            await check_budget(db, 9876543211, 300)
            ok("全局日限额拦截", False, "未抛错")
        except BudgetExceeded as exc:
            ok("全局日限额拦截", "系统今日 Token 预算已用尽" in str(exc), str(exc))


async def check_engine_gate() -> None:
    print("\n[3] 评估引擎预算闸（call_llm 最前，整场终止语义）")
    async with SessionLocal() as db:
        # 单次上限压到 1：任何非空 prompt 的估算（len*1.2 ≥ 1.2 → int ≥ 1，
        # 空串算 0 —— 用真实长 prompt 保证 > 1）都在 provider 解析前被拒
        await TokenBudgetService(db).update_config({"singleCallLimit": 1})
    try:
        await rag_eval_engine.call_llm(None, None, "这是一段足够长的评估提示词" * 20)
        ok("超限时 call_llm 抛错（不静默返回 None）", False, "未抛错")
    except BudgetExceeded:
        ok("超限时 call_llm 抛错（整场评估终止，Java 同语义）", True)


async def check_fail_open() -> None:
    print("\n[4] Redis 不可达 → fail-open 放行")
    # 上一段把 singleCallLimit 压到 1 了，先放开，保证走到 Redis 读取那步
    async with SessionLocal() as db:
        await TokenBudgetService(db).update_config(
            {"singleCallLimit": 100000, "userDailyLimit": 100000000, "globalDailyLimit": 100000000}
        )
    from unittest.mock import patch

    from app.core import token_store

    async def broken():
        raise ConnectionError("模拟 Redis 挂了")

    with patch.object(token_store, "shared_client", side_effect=lambda: _BrokenClient()):
        async with SessionLocal() as db:
            # 不抛错即放行
            await check_budget(db, _TEST_UID, 10)
            ok("Redis 挂时 check_budget 放行（fail-open）", True)
            await record_usage(_TEST_UID, 100)
            ok("Redis 挂时 record_usage 静默（不影响对话）", True)


class _BrokenClient:
    """最小假客户端：任何属性访问返回会抛错的协程函数。"""

    def pipeline(self, transaction: bool = True):  # noqa: ARG002
        raise ConnectionError("模拟 Redis 挂了")

    def get(self, key: str):  # noqa: ARG002
        raise ConnectionError("模拟 Redis 挂了")


async def main() -> int:
    backup = await backup_config()
    try:
        r = await _redis()
        await r.delete(_user_key())
        await check_record()
        await check_gates()
        await check_engine_gate()
        await check_fail_open()
    finally:
        await restore_config(backup)
        try:
            r = await _redis()
            await r.delete(_user_key())
        except Exception:  # noqa: BLE001
            pass

    print(f"\n{'=' * 60}")
    if _FAILED:
        print(f"❌ 失败 {len(_FAILED)} 项: {_FAILED}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
