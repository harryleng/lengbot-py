"""工具层自检：schema 生成、DB 解析、schema 漂移、实际执行。

这是阶段 2 前两步的验收脚本。跑法：
    uv run python -m scripts.tools_check
"""

from __future__ import annotations

import asyncio
import json

from sqlalchemy import select

from app.core.db import SessionLocal
from app.models.entities import Agent, Tool
from app.tools.registry import registry

AGENT_ID = 2099681584963604481


async def main() -> None:
    print("=" * 70)
    print("1. 从类型注解自动生成的 OpenAI tools schema")
    print("=" * 70)
    for name in registry.implemented:
        spec = registry.get(name)
        assert spec is not None
        print(f"\n--- {name} ---")
        print(json.dumps(spec.openai_schema(), ensure_ascii=False, indent=2))

    print()
    print("=" * 70)
    print("2. 按 Agent 配置解析可用工具")
    print("=" * 70)
    async with SessionLocal() as db:
        agent = await db.get(Agent, AGENT_ID)
        if agent is None:
            print(f"Agent {AGENT_ID} 不存在")
            return
        raw_ids = (agent.config or {}).get("tools") or []
        print(f"Agent 配置了 {len(raw_ids)} 个工具 ID")

        rows = (await db.execute(select(Tool).where(Tool.id.in_([int(i) for i in raw_ids])))).scalars().all()
        print(f"库里查到 {len(rows)} 个工具定义：")
        for row in rows:
            has_impl = "✅ 已实现" if registry.get(row.name or "") else "❌ 无实现"
            print(f"  {row.name:<24} {row.display_name or '':<10} {has_impl}")

        specs = await registry.resolve_for_agent(db, agent.config)
        print(f"\n实际可交给模型的工具: {[s.name for s in specs] or '（无）'}")

    print()
    print("=" * 70)
    print("3. DB schema 与代码 schema 的一致性（漂移检测）")
    print("=" * 70)
    async with SessionLocal() as db:
        rows = (await db.execute(select(Tool).where(Tool.name.in_(registry.implemented)))).scalars().all()
        if not rows:
            print("库里没有同名工具，跳过")
        for row in rows:
            diffs = registry.verify_against_db(row)
            if diffs:
                print(f"⚠️  {row.name}: {'; '.join(diffs)}")
            else:
                print(f"✅ {row.name}: 完全一致")

    print()
    print("=" * 70)
    print("4. 实际执行工具（校验参数 model + handler）")
    print("=" * 70)
    spec = registry.get("calculator")
    assert spec is not None
    cases = [
        {"a": 10, "b": 5, "operation": "add"},
        {"a": 7, "b": 2, "operation": "divide"},
    ]
    for case in cases:
        params = spec.params_model(**case)
        result = await spec.handler(**params.model_dump())
        print(f"calculator({case}) → {result}")

    # 参数校验：enum 之外的取值应该被 Pydantic 拦住
    try:
        spec.params_model(a=1, b=2, operation="power")
        print("❌ 非法 operation 竟然通过了校验")
    except Exception as exc:  # noqa: BLE001
        print(f"✅ 非法 operation 被拦下: {type(exc).__name__}")

    time_spec = registry.get("get_current_time")
    assert time_spec is not None
    print(f"get_current_time() → {await time_spec.handler()}")
    print(f"get_current_time(Asia/Tokyo) → {await time_spec.handler(timezone='Asia/Tokyo')}")

    print()
    print("=" * 70)
    print("5. 文件工具实际执行 + 路径穿越防护")
    print("=" * 70)
    write_spec = registry.get("local_write_file")
    read_spec = registry.get("local_read_file")
    list_spec = registry.get("local_list_dir")
    append_spec = registry.get("local_append_file")
    delete_spec = registry.get("local_delete_file")
    assert all([write_spec, read_spec, list_spec, append_spec, delete_spec])

    print(f"write  → {await write_spec.handler(path='demo/hello.txt', content='第一行')}")
    print(f"append → {await append_spec.handler(path='demo/hello.txt', content='第二行')}")
    print(f"read   → {await read_spec.handler(path='demo/hello.txt')}")
    print(f"list   → {await list_spec.handler(dirPath='demo')}")

    # 安全验证：这几种路径必须全被拒绝
    attacks = ["../../windows/win.ini", "..\\..\\windows\\win.ini", "/etc/passwd", "C:/Windows/win.ini"]
    for attack in attacks:
        result = await read_spec.handler(path=attack)
        # 返回结构 2026-10-24 起对齐 Java：失败是 {"success": false, "error": ...}
        blocked = '"success": false' in result
        print(f"{'✅' if blocked else '❌'} 穿越拦截 {attack!r} → {'已拒绝' if blocked else '竟然通过了！'}")

    print(f"delete → {await delete_spec.handler(path='demo/hello.txt')}")
    print(f"删除后读取 → {await read_spec.handler(path='demo/hello.txt')}")


if __name__ == "__main__":
    asyncio.run(main())
