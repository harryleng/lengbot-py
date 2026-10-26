"""内置资源启动注册器（对照 Java `BuiltInSkillRegistrar` + 工具漂移检查）。

Java 侧在启动时（ApplicationRunner @Order(20)）做两件事：

1. **Skill 同步**：扫描 `BuiltInSkillDefinitions` 的 5 个定义，
   不存在则插入、存在但 content_hash / tool_ids / 图标 / MinIO 文件
   任一漂移则更新 —— 保证「代码里的版本」始终是运行时的版本。
2. **tool 行不做注册**（DB 行来自 dump/手工管理），但运行时回调由
   `BuiltinToolCallbackFactory` 提供。

Python 同构实现：

- Skill 同步逐字段对齐（含 MinIO SKILL.md 写入、tool 名→ID 解析）；
- tool 行同样不动 DB，但启动时输出**实现漂移报告**：DB 里有哪些
  工具行 Python 尚未实现（前端应禁用"测试"按钮）、注册器里有实现
  但 DB 缺行的名字（这种情况说明 dump 缺数据，值得大声说出来）。

失败语义：单条 Skill 同步失败只 log 不中断（MinIO 抖动不该挡启动），
整体异常也吞掉 —— 注册器是"尽力对齐"，不是启动的前置条件。
"""

from __future__ import annotations

import hashlib
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entities import Skill, Tool
from app.services import skill_storage
from app.services.builtin_skill_defs import DEFS

logger = logging.getLogger(__name__)


def _content_hash(skill_md: str) -> str:
    return hashlib.sha256(skill_md.encode("utf-8")).hexdigest()


async def _resolve_tool_ids(db: AsyncSession, tool_names: list[str]) -> list[str]:
    """工具英文名 → ID（缺失忽略，Java 同款：log warn 后跳过）。

    ⚠️ 一条一条查而不是 `name IN (...)`：Java 就是 `selectByName` 循环，
    顺序必须跟 toolNames 一致（toolIds 是有序数组，注册顺序即提示词里的
    依赖顺序）。
    """
    ids: list[str] = []
    for name in tool_names:
        row = (
            await db.execute(select(Tool.id).where(Tool.name == name))
        ).scalars().first()
        if row is None:
            logger.warning("[builtin_registrar] 未找到依赖工具: name=%s（skill 关联将忽略）", name)
            continue
        ids.append(str(row))
    return ids


async def sync_builtin_skills(db: AsyncSession) -> dict[str, list[str]]:
    """同步内置 Skill 到 DB + MinIO。返回 {created, updated, unchanged}（slug 列表）。"""
    result: dict[str, list[str]] = {"created": [], "updated": [], "unchanged": []}

    for d in DEFS:
        slug = d["slug"]
        try:
            outcome = await _sync_one(db, d)
        except Exception:  # noqa: BLE001
            logger.exception("[builtin_registrar] 同步内置 Skill 失败: slug=%s", slug)
            continue
        result[outcome].append(slug)
        if outcome != "unchanged":
            logger.info("[builtin_registrar] %s 内置 Skill: slug=%s", outcome, slug)
            await db.commit()

    return result


async def _sync_one(db: AsyncSession, d: dict) -> str:
    """单个定义的插入/更新。返回 created / updated / unchanged。"""
    slug = d["slug"]
    existing = (
        await db.execute(select(Skill).where(Skill.slug == slug, Skill.deleted == 0))
    ).scalars().first()

    tool_ids = await _resolve_tool_ids(db, d["tool_names"])
    skill_md = skill_storage.build_skill_markdown(
        {
            "name": d["name"],
            "slug": slug,
            "description": d["description"],
            "version": "1.0.0",
            "tool_dependencies": d["tool_names"],
            "skill_dependencies": d["skill_dependencies"],
            "prompt_template": d["prompt_template"],
        }
    )
    content_hash = _content_hash(skill_md)

    if existing is None:
        from app.core.snowflake import new_id
        from datetime import datetime

        now = datetime.now()
        db.add(
            Skill(
                id=new_id(),
                slug=slug,
                name=d["name"],
                display_name=d["display_name"],
                icon=d["icon"],
                description=d["description"],
                prompt_template=d["prompt_template"],
                tool_ids=tool_ids,
                mcp_server_ids=[],
                sort_order=d["sort_order"],
                status="active",
                scope="global",
                is_builtin=1,
                content_hash=content_hash,
                object_prefix=f"skills/{slug}/",
                version="1.0.0",
                skill_dependencies=d["skill_dependencies"],
                source_type="builtin",
                create_time=now,
                update_time=now,
            )
        )
        await skill_storage.write_skill_markdown(slug, skill_md)
        return "created"

    md_missing = not await skill_storage.skill_markdown_exists(slug)
    needs_update = (
        content_hash != (existing.content_hash or "")
        or existing.is_builtin is None
        or tool_ids != list(existing.tool_ids or [])
        or existing.object_prefix is None
        or d["icon"] != (existing.icon or "")
        or md_missing
    )
    if not needs_update:
        return "unchanged"

    existing.name = d["name"]
    existing.display_name = d["display_name"]
    existing.icon = d["icon"]
    existing.description = d["description"]
    existing.prompt_template = d["prompt_template"]
    existing.tool_ids = tool_ids
    existing.is_builtin = 1
    existing.scope = "global"
    existing.content_hash = content_hash
    existing.object_prefix = f"skills/{slug}/"
    existing.version = "1.0.0"
    existing.skill_dependencies = d["skill_dependencies"]
    existing.source_type = "builtin"
    if existing.sort_order is None:
        existing.sort_order = d["sort_order"]
    from datetime import datetime

    existing.update_time = datetime.now()
    await skill_storage.write_skill_markdown(slug, skill_md)
    return "updated"


async def tool_drift_report(db: AsyncSession) -> dict[str, list[str]]:
    """工具实现漂移报告（只读，不写库）。

    - `not_implemented`：DB 有行、Python 注册器没有实现 —— 前端 `implemented=false`，
      "测试"按钮应禁用。
    - `missing_in_db`：Python 有实现、DB 缺行 —— dump 数据缺口，需要补行。
    """
    from app.tools.registry import registry

    db_names = set(
        (await db.execute(select(Tool.name).where(Tool.deleted == 0))).scalars().all()
    )
    implemented = set(registry.implemented)
    return {
        "not_implemented": sorted(db_names - implemented),
        "missing_in_db": sorted(implemented - db_names),
    }


async def run_startup_sync() -> None:
    """启动入口：Skill 同步 + 漂移报告。任何异常只 log，不挡启动。"""
    from app.core.db import SessionLocal

    try:
        async with SessionLocal() as db:
            stats = await sync_builtin_skills(db)
            logger.info(
                "[builtin_registrar] 内置 Skill 同步完成: created=%s updated=%s unchanged=%s",
                stats["created"], stats["updated"], stats["unchanged"],
            )
            drift = await tool_drift_report(db)
            logger.info(
                "[builtin_registrar] 工具实现漂移: 未实现 %d 个 %s / DB缺行 %d 个 %s",
                len(drift["not_implemented"]), drift["not_implemented"],
                len(drift["missing_in_db"]), drift["missing_in_db"],
            )
    except Exception:  # noqa: BLE001
        logger.exception("[builtin_registrar] 启动同步失败（不阻断启动）")
