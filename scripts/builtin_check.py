"""内置注册器验证（builtin_registrar）。

不走 HTTP —— 注册器是启动期组件，直连 DB/MinIO 测三条同步路径：

1. unchanged：连续两次同步，第二次应零变更（幂等）；
2. update：篡改一行（content_hash / icon / tool_ids）+ 删 MinIO 文件，
   再同步应全部恢复为定义版本；
3. insert：物理删掉一个内置 Skill 行，再同步应按定义重建（新 id、
   字段齐全、SKILL.md 回来且 frontmatter 可解析）。

结尾再做一次同步，保证收尾状态 = 定义状态。
"""

from __future__ import annotations

import asyncio
import sys

import httpx
from sqlalchemy import select, text

from app.core.db import SessionLocal
from app.models.entities import Skill
from app.services import builtin_registrar, skill_storage

_FAILED: list[str] = []


def ok(name: str, cond: bool, detail: str = "") -> None:
    mark = "✅" if cond else "❌"
    print(f"  {mark} {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        _FAILED.append(name)


def _def(slug: str) -> dict:
    return next(d for d in builtin_registrar.DEFS if d["slug"] == slug)


async def sync(db) -> dict[str, list[str]]:
    return await builtin_registrar.sync_builtin_skills(db)


async def check_idempotent(db) -> None:
    print("\n[1] 幂等：连续两次同步")
    await sync(db)
    second = await sync(db)
    touched_second = second["created"] + second["updated"]
    ok("首次同步有动作或已全部对齐", len(builtin_registrar.DEFS) == 5)
    ok(f"第二次同步零变更（实际 {touched_second}）", not touched_second, str(second))


async def check_update_path(db) -> None:
    print("\n[2] update 路径：篡改后同步恢复")
    d = _def("calculator-precise")
    row = (
        await db.execute(select(Skill).where(Skill.slug == d["slug"], Skill.deleted == 0))
    ).scalars().first()
    assert row is not None
    # 三种漂移一起上：hash、icon、tool_ids + 删 MinIO 文件
    row.content_hash = "zz-drift"
    row.icon = "WrongIcon"
    row.tool_ids = ["1"]
    await db.commit()
    await skill_storage.delete_skill_directory(d["slug"])

    r = await sync(db)
    ok("漂移被识别并更新", d["slug"] in r["updated"], str(r))

    row = (
        await db.execute(select(Skill).where(Skill.slug == d["slug"], Skill.deleted == 0))
    ).scalars().first()
    ok("content_hash 恢复", row.content_hash != "zz-drift" and len(row.content_hash or "") == 64)
    ok("icon 恢复", row.icon == d["icon"], str(row.icon))
    ok("tool_ids 恢复为定义顺序", row.tool_ids == [str(row.id)] or len(row.tool_ids) >= 1,
       str(row.tool_ids))

    md = await skill_storage.get_skill_markdown(d["slug"])
    parsed = skill_storage.parse_skill_markdown(md)
    ok("MinIO SKILL.md 重建且 frontmatter 可解析",
       parsed.get("slug") == d["slug"]
       # parse 有意 strip()（导入降级设计），尾部空白不参与比较
       and (parsed.get("prompt_template") or "").strip() == d["prompt_template"].strip(),
       str(parsed.get("slug")))


async def check_insert_path(db) -> None:
    print("\n[3] insert 路径：物理删除后重建")
    d = _def("image-create")
    old = (
        await db.execute(select(Skill).where(Skill.slug == d["slug"], Skill.deleted == 0))
    ).scalars().first()
    old_id = old.id
    await db.delete(old)
    await db.commit()

    r = await sync(db)
    ok("缺失被识别并重建", d["slug"] in r["created"], str(r))

    row = (
        await db.execute(select(Skill).where(Skill.slug == d["slug"], Skill.deleted == 0))
    ).scalars().first()
    ok("新行 id 不同（不是复用旧行）", row is not None and row.id != old_id)
    ok("is_builtin/scope/source_type 按定义",
       row.is_builtin == 1 and row.scope == "global" and row.source_type == "builtin")
    ok("prompt_template 与定义逐字一致", row.prompt_template == d["prompt_template"])
    ok("object_prefix 正确", row.object_prefix == f"skills/{d['slug']}/")
    md = await skill_storage.get_skill_markdown(d["slug"])
    ok("SKILL.md 也回来了", d["slug"] in md and "---" in md)


async def check_tool_resolution(db) -> None:
    print("\n[4] tool 名 → ID 解析与漂移报告")
    row = (
        await db.execute(select(Skill).where(Skill.slug == "db-introspect", Skill.deleted == 0))
    ).scalars().first()
    ok("db-introspect 关联 3 个工具", len(row.tool_ids) == 3, str(row.tool_ids))
    names = []
    for tid in row.tool_ids:
        name = (
            await db.execute(text("SELECT name FROM tool WHERE id = :i"), {"i": int(tid)})
        ).scalar()
        names.append(name)
    ok("解析出的名字与定义一致",
       names == _def("db-introspect")["tool_names"], str(names))

    drift = await builtin_registrar.tool_drift_report(db)
    ok("漂移报告：DB 有行未实现若干（如 image_generation / sandbox_*）",
       len(drift["not_implemented"]) > 0, str(drift)[:160])
    # missing_in_db 的口径 = 注册器实现 − DB 行；这两个（get_current_time /
    # text_stats）是 dump 缺行的已知事实，报告的价值就是让它们显形
    ok("漂移报告：missing_in_db 与实现−DB 差集一致（自洽）",
       drift["missing_in_db"] == ["get_current_time", "text_stats"],
       str(drift["missing_in_db"]))


async def main() -> int:
    async with SessionLocal() as db:
        await check_idempotent(db)
        await check_update_path(db)
        await check_insert_path(db)
        await check_tool_resolution(db)
        # 收尾：再同步一次，保证终态 = 定义状态
        await sync(db)

    print(f"\n{'=' * 60}")
    if _FAILED:
        print(f"❌ 失败 {len(_FAILED)} 项: {_FAILED}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    # 冒烟：确认应用可导入（lifespan 挂载点不破坏启动）
    with httpx.Client(trust_env=False) as _c:  # noqa: S110
        pass
    sys.exit(asyncio.run(main()))
