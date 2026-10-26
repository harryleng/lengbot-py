"""Skill 管理业务层（对照 Java `SkillServiceImpl` 567 行）。

## 三条必须保住的规矩

1. **内置 Skill（`is_builtin=1`）不可编辑、不可删除**。库里 5 个内置 Skill
   是代码注册器写的，用户改了下次启动就被覆盖回去 —— 不挡住的话用户以为
   保存成功了，实际改动静默丢失。
2. **`slug` 冲突要自动加后缀 `-v2/-v3...`**（仅导入/远程安装路径）。
   手工创建则直接报冲突，因为那是用户自己填的，静默改名更让人困惑。
3. **删 Skill 要连 MinIO 目录一起删**，但 **MinIO 失败不能让删除整体失败** ——
   对象已经没了就是目标状态，抛异常用户会反复重试都删不掉。

## 关于 `contentHash`

DB 存的 `contentHash` 是 SKILL.md 全文的 sha256，`objectPrefix` 是
`skills/{slug}/`。两者都不参与业务逻辑判断，是给"内置 Skill 内容是否变了"
这种场景留的。Java 在 create/update promptTemplate 时重算，这里照做。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.snowflake import new_id
from app.models.entities import McpServer, Skill, Tool
from app.schemas.common import PageResult
from app.schemas.skill import (
    SkillFileNode,
    SkillImportPreview,
    SkillIn,
    SkillOut,
)
from app.services import github_skill_service, skill_storage

logger = logging.getLogger(__name__)

# `^[a-z0-9]+(-[a-z0-9]+)*$` —— 和 Java 完全一致。
# 它同时是 MinIO 路径的一部分，所以不允许大写、下划线、连续短横线。
SLUG_PATTERN = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def _bad(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


def _conflict(detail: str) -> HTTPException:
    """409：slug / 名称冲突。前端靠 code 区分"填错了"和"重名了"。"""
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


def _to_out(row: Skill) -> SkillOut:
    return SkillOut.model_validate(row)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class SkillService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    async def _require(self, skill_id: int) -> Skill:
        row = (
            await self.db.execute(
                select(Skill).where(Skill.id == skill_id, Skill.deleted == 0)
            )
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill 不存在")
        return row

    async def _get_by_slug(self, slug: str) -> Skill | None:
        if not slug:
            return None
        return (
            await self.db.execute(
                select(Skill).where(Skill.slug == slug, Skill.deleted == 0)
            )
        ).scalar_one_or_none()

    async def page(
        self, page_num: int, page_size: int, keyword: str | None
    ) -> PageResult[SkillOut]:
        """全局 Skill 分页。

        排序是 `is_builtin DESC, sort_order ASC, create_time DESC` ——
        内置 Skill 永远排在最前面，和用户自己建的分区，别改成单一排序。
        """
        conditions = [Skill.deleted == 0, Skill.scope == "global"]
        if keyword:
            like = f"%{keyword}%"
            conditions.append(
                or_(Skill.name.ilike(like), Skill.display_name.ilike(like), Skill.slug.ilike(like))
            )

        total = (
            await self.db.execute(
                select(func.count()).select_from(Skill).where(*conditions)
            )
        ).scalar_one()
        rows = (
            (
                await self.db.execute(
                    select(Skill)
                    .where(*conditions)
                    .order_by(Skill.is_builtin.desc(), Skill.sort_order, Skill.create_time.desc())
                    .offset((page_num - 1) * page_size)
                    .limit(page_size)
                )
            )
            .scalars()
            .all()
        )
        return PageResult.of([_to_out(r) for r in rows], total, page_num, page_size)

    async def list_enabled(self) -> list[SkillOut]:
        rows = (
            (
                await self.db.execute(
                    select(Skill)
                    .where(
                        Skill.deleted == 0,
                        Skill.scope == "global",
                        Skill.status == "active",
                    )
                    .order_by(Skill.is_builtin.desc(), Skill.sort_order, Skill.create_time.desc())
                )
            )
            .scalars()
            .all()
        )
        return [_to_out(r) for r in rows]

    async def list_by_agent(self, agent_id: int, name: str | None) -> list[SkillOut]:
        """兼容旧接口：按 Agent 私有查询。

        现在新建的 Skill 都是 `scope=global`，这条基本只会返回空 ——
        但前端「Agent 编辑」页还在调，删了会 404。
        """
        conditions = [Skill.deleted == 0, Skill.agent_id == agent_id]
        if name:
            conditions.append(Skill.name.ilike(f"%{name}%"))
        rows = (
            (
                await self.db.execute(
                    select(Skill)
                    .where(*conditions)
                    .order_by(Skill.sort_order, Skill.create_time.desc())
                )
            )
            .scalars()
            .all()
        )
        return [_to_out(r) for r in rows]

    # ------------------------------------------------------------------
    # 创建 / 更新 / 删除
    # ------------------------------------------------------------------

    async def create(self, dto: SkillIn) -> SkillOut:
        scope = dto.scope or "global"
        if scope == "global":
            await self._validate_slug(dto.slug, None)

        if not dto.name or not dto.name.strip():
            raise _bad("Skill 名称不能为空")
        await self._ensure_name_free(dto.name.strip(), None)

        slug = dto.slug
        skill = Skill(
            id=new_id(),
            slug=slug,
            agent_id=int(dto.agent_id) if scope == "agent" and dto.agent_id else None,
            name=dto.name.strip(),
            display_name=dto.display_name,
            icon=dto.icon,
            description=dto.description,
            prompt_template=dto.prompt_template,
            config=_parse_json(dto.config),
            tool_ids=dto.tool_ids or [],
            mcp_server_ids=dto.mcp_server_ids or [],
            model_id=int(dto.model_id) if dto.model_id else None,
            sort_order=dto.sort_order or 0,
            status="active",
            scope=scope,
            is_builtin=0,
            version=dto.version or "1.0.0",
            skill_dependencies=dto.skill_dependencies or [],
            source_type=dto.source_type or "upload",
            user_id=None,
            create_time=datetime.now(),
            update_time=datetime.now(),
            deleted=0,
        )

        if slug:
            skill.object_prefix = f"skills/{slug}/"
            md = skill_storage.build_skill_markdown(_meta_of(skill))
            skill.content_hash = _sha256(md)
            await skill_storage.write_skill_markdown(slug, md)

        self.db.add(skill)
        await self.db.commit()
        await self.db.refresh(skill)
        logger.info("创建 Skill slug=%s name=%s", skill.slug, skill.name)
        return _to_out(skill)

    async def update(self, dto: SkillIn) -> SkillOut:
        if not dto.id:
            raise _bad("缺少 id")
        skill = await self._require(int(dto.id))
        if skill.is_builtin == 1:
            raise _bad("内置 Skill 不可编辑")

        if skill.scope == "global" and dto.slug and dto.slug != skill.slug:
            await self._validate_slug(dto.slug, skill.id)
            skill.slug = dto.slug

        if dto.name and dto.name != skill.name:
            await self._ensure_name_free(dto.name, skill.id)
            skill.name = dto.name
        skill.display_name = dto.display_name
        skill.icon = dto.icon
        skill.description = dto.description
        skill.config = _parse_json(dto.config)
        # 悬空引用清理：前端会展示"已删除的工具"，但落库必须剥掉
        skill.tool_ids = await self._clean_stale_tool_ids(dto.tool_ids or [])
        skill.mcp_server_ids = await self._clean_stale_mcp_ids(dto.mcp_server_ids or [])
        skill.model_id = int(dto.model_id) if dto.model_id else None
        skill.sort_order = dto.sort_order or 0
        if dto.version:
            skill.version = dto.version
        if dto.skill_dependencies is not None:
            skill.skill_dependencies = dto.skill_dependencies

        # 只有 promptTemplate 变了才重写 MinIO：写一次 SKILL.md 就是一次
        # 对象存储往返，无关字段变更也写会让 contentHash 无意义地跳变。
        if dto.prompt_template is not None and dto.prompt_template != skill.prompt_template:
            skill.prompt_template = dto.prompt_template
            if skill.slug:
                skill.object_prefix = f"skills/{skill.slug}/"
                md = skill_storage.build_skill_markdown(_meta_of(skill))
                skill.content_hash = _sha256(md)
                await skill_storage.write_skill_markdown(skill.slug, md)

        skill.update_time = datetime.now()
        await self.db.commit()
        await self.db.refresh(skill)
        return _to_out(skill)

    async def delete(self, skill_id: int) -> None:
        skill = await self._require(skill_id)
        if skill.is_builtin == 1:
            raise _bad("内置 Skill 不可删除")
        if skill.slug:
            try:
                await skill_storage.delete_skill_directory(slug=skill.slug)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[Skill] 删除 MinIO 文件失败 slug=%s err=%s", skill.slug, exc)
        skill.deleted = 1
        skill.update_time = datetime.now()
        await self.db.commit()

    async def set_enabled(self, skill_id: int, enabled: bool) -> None:
        skill = await self._require(skill_id)
        skill.status = "active" if enabled else "disabled"
        skill.update_time = datetime.now()
        await self.db.commit()

    # ------------------------------------------------------------------
    # ZIP 导入 / 导出
    # ------------------------------------------------------------------

    async def import_preview(self, zip_bytes: bytes) -> SkillImportPreview:
        preview = await skill_storage.stage_draft(skill_storage.new_draft_id(), zip_bytes)
        return SkillImportPreview.model_validate(preview)

    async def import_commit(self, draft_id: str, target_slug: str | None) -> SkillOut:
        final_slug = await skill_storage.commit_draft(draft_id, target_slug)
        md = await skill_storage.get_skill_markdown(final_slug)
        meta = skill_storage.parse_skill_markdown(md)

        existing = await self._get_by_slug(final_slug)
        if existing is not None:
            final_slug = await self._next_free_slug(final_slug)
            await skill_storage.delete_skill_directory(final_slug)
            await skill_storage.write_skill_markdown(final_slug, md)

        return await self._save_from_metadata(final_slug, md, meta, "upload")

    async def export_zip(self, skill_id: int) -> bytes:
        skill = await self._require(skill_id)
        if not skill.slug:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill 未关联文件目录")
        return await skill_storage.export_skill_zip(skill.slug)

    # ------------------------------------------------------------------
    # 文件管理
    # ------------------------------------------------------------------

    async def list_files(self, skill_id: int) -> list[SkillFileNode]:
        skill = await self._require(skill_id)
        if not skill.slug:
            return []
        tree = await skill_storage.build_file_tree(skill.slug)
        return [SkillFileNode.model_validate(n) for n in tree]

    async def read_file(self, skill_id: int, path: str) -> bytes:
        skill = await self._require(skill_id)
        if not skill.slug:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill 未关联文件目录")
        return await skill_storage.read_file(skill.slug, skill_storage.validate_file_path(path))

    async def create_file(self, skill_id: int, path: str, content: str | None, is_dir: bool) -> None:
        skill = await self._require(skill_id)
        if not skill.slug:
            raise _bad("Skill 未关联文件目录")
        path = skill_storage.validate_file_path(path)
        if is_dir:
            # MinIO 没有"空目录"概念，写个 .gitkeep 占位（Java 同）
            dir_path = path if path.endswith("/") else path + "/"
            await skill_storage.write_file(skill.slug, dir_path + ".gitkeep", "")
        else:
            await skill_storage.write_file(skill.slug, path, content or "")

    async def update_file(self, skill_id: int, path: str, content: str | None) -> None:
        skill = await self._require(skill_id)
        if not skill.slug:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill 未关联文件目录")
        await skill_storage.write_file(
            skill.slug, skill_storage.validate_file_path(path), content or ""
        )

    async def delete_file(self, skill_id: int, path: str) -> None:
        skill = await self._require(skill_id)
        if not skill.slug:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill 未关联文件目录")
        await skill_storage.delete_file(skill.slug, skill_storage.validate_file_path(path))

    # ------------------------------------------------------------------
    # 远程安装
    # ------------------------------------------------------------------

    async def list_remote(self, source: str) -> list[dict[str, str]]:
        if github_skill_service.is_model_scope_url(source):
            return await github_skill_service.list_model_scope_skills(source)
        owner, repo, branch = github_skill_service.parse_source(source)
        return await github_skill_service.list_remote_skills(owner, repo, branch)

    async def search_remote(self, source: str) -> list[dict[str, str]]:
        return await github_skill_service.search_remote_skills(source)

    async def prepare_remote(self, source: str, skills: list[str]) -> list[SkillImportPreview]:
        if github_skill_service.is_model_scope_url(source):
            previews = await github_skill_service.prepare_model_scope_install(source, skills)
        else:
            owner, repo, branch = github_skill_service.parse_source(source)
            previews = await github_skill_service.prepare_remote_install(
                owner, repo, branch, skills
            )
        return [SkillImportPreview.model_validate(p) for p in previews]

    async def commit_remote(self, draft_id: str, slug: str) -> SkillOut:
        final_slug = await skill_storage.commit_draft_for_slug(draft_id, slug)
        md = await skill_storage.get_skill_markdown(final_slug)
        meta = skill_storage.parse_skill_markdown(md)

        existing = await self._get_by_slug(final_slug)
        if existing is not None:
            final_slug = await self._next_free_slug(final_slug)
            await skill_storage.delete_skill_directory(final_slug)
            await skill_storage.write_skill_markdown(final_slug, md)

        return await self._save_from_metadata(final_slug, md, meta, "remote")

    async def cleanup_draft(self, draft_id: str) -> None:
        await skill_storage.cleanup_draft(draft_id)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    async def _validate_slug(self, slug: str | None, current_id: int | None) -> None:
        if not slug or not slug.strip():
            raise _bad("全局 Skill 必须填写 slug")
        if not SLUG_PATTERN.match(slug):
            raise _bad("slug 只能包含小写字母、数字和短横线")
        exists = await self._get_by_slug(slug)
        if exists is not None and exists.id != current_id:
            raise _conflict(f"slug 已存在: {slug}")

    async def _ensure_name_free(self, name: str, exclude_id: int | None) -> None:
        conditions = [Skill.deleted == 0, Skill.name == name]
        if exclude_id is not None:
            conditions.append(Skill.id != exclude_id)
        count = (
            await self.db.execute(select(func.count()).select_from(Skill).where(*conditions))
        ).scalar_one()
        if count > 0:
            raise _conflict(f"Skill 名称已存在: {name}")

    async def _next_free_slug(self, base: str) -> str:
        for i in range(2, 101):
            candidate = f"{base}-v{i}"
            if await self._get_by_slug(candidate) is None:
                return candidate
        raise _conflict(f"slug 冲突且无法自动改名: {base}")

    async def _clean_stale_tool_ids(self, ids: list[str]) -> list[str]:
        if not ids:
            return ids
        return await self._filter_existing(Tool, ids)

    async def _clean_stale_mcp_ids(self, ids: list[str]) -> list[str]:
        if not ids:
            return ids
        return await self._filter_existing(McpServer, ids)

    async def _filter_existing(self, model: Any, ids: list[str]) -> list[str]:
        """过滤掉已被删除的 Tool / MCP 引用。

        Java 侧 `cleanStaleToolIds` 直接 `Long.parseLong`，遇到非数字会炸；
        这里用 try 跳过 —— 前端「悬空引用」提示会把已删除的 id 也回传上来。
        """
        numeric: list[int] = []
        for raw in ids:
            try:
                numeric.append(int(raw))
            except (TypeError, ValueError):
                continue
        if not numeric:
            return []
        rows = (
            (await self.db.execute(select(model.id).where(model.id.in_(numeric), model.deleted == 0)))
            .scalars()
            .all()
        )
        alive = {str(r) for r in rows}
        return [i for i in ids if i in alive]

    async def _save_from_metadata(
        self, slug: str, md: str, meta: dict[str, Any], source_type: str
    ) -> SkillOut:
        skill = Skill(
            id=new_id(),
            slug=slug,
            name=meta.get("name") or slug,
            display_name=meta.get("name"),
            description=meta.get("description"),
            prompt_template=meta.get("prompt_template"),
            config={},
            tool_ids=[],
            mcp_server_ids=[],
            skill_dependencies=meta.get("skill_dependencies") or [],
            version=meta.get("version") or "1.0.0",
            source_type=source_type,
            object_prefix=f"skills/{slug}/",
            content_hash=_sha256(md),
            sort_order=0,
            status="active",
            scope="global",
            is_builtin=0,
            create_time=datetime.now(),
            update_time=datetime.now(),
            deleted=0,
        )
        self.db.add(skill)
        await self.db.commit()
        await self.db.refresh(skill)
        logger.info("导入 Skill slug=%s source=%s", slug, source_type)
        return _to_out(skill)


def _parse_json(raw: str | None) -> dict | None:
    """config 入参是 JSON 文本（前端原样回传），落 JSONB 列要转成 dict。"""
    if raw is None or not str(raw).strip():
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("[Skill] config 不是合法 JSON，忽略: %s", raw[:100])
        return None
    return value if isinstance(value, dict) else {"value": value}


def _meta_of(skill: Skill) -> dict[str, Any]:
    """实体 → SKILL.md 元数据（Java `buildSkillMdContent`）。"""
    return {
        "slug": skill.slug,
        "name": skill.name,
        "description": skill.description,
        "version": skill.version or "1.0.0",
        "skill_dependencies": list(skill.skill_dependencies or []),
        "prompt_template": skill.prompt_template,
    }
