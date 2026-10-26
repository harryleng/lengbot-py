"""Prompt 管理服务（主表 / 版本 / 构建模板）。

对应 Java：`PromptServiceImpl` + `PromptVersionServiceImpl` + `PromptBuildTemplateServiceImpl`。

**这段逻辑里最容易出错的是版本创建的四步**（Java 顺序不能变）：

1. `promptKey` 必须已存在 —— 版本挂在 prompt 下，没有父记录就是脏数据
2. `status` 只认 `release` / 其它（都当 `pre`）
3. 要发 `release` 时，同 key+version 已有 release → **拒绝**
   （否则同一版本号有两个"正式版"，评测/引用时取到哪个是随机的）
4. 创建前**先软删同 key+version 的 `pre`** —— 预发布允许覆盖重来
5. 建完更新 `prompt.latest_version`

第 4 步是"预发布可以反复改"的关键。少了它，用户改一次预发布版本
就会在列表里看到两条同版本号，一条 pre 一条 pre，不知道哪条生效。

**所有删除都是软删**（`deleted=1`）。删 prompt 会级联软删它的所有版本，
且必须在同一事务里 —— Java 那边专门加了 `@Transactional(rollbackFor)`，
否则主表删了、版本还在，下次用同名 key 重建会撞上旧版本。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.snowflake import new_id
from app.models.entities import Prompt, PromptBuildTemplate, PromptVersion
from app.schemas.prompt import (
    PromptCreateIn,
    PromptOut,
    PromptTemplateIn,
    PromptTemplateOut,
    PromptVersionIn,
    PromptVersionOut,
    parse_json_value,
    to_json_text,
)

logger = logging.getLogger(__name__)

STATUS_RELEASE = "release"
STATUS_PRE = "pre"


def _bad_request(msg: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=msg)


def _not_found(msg: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=msg)


class PromptService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # ==================================================================
    # Prompt 主表
    # ==================================================================
    async def list(
        self,
        user_id: int,
        page_num: int = 1,
        page_size: int = 20,
        keyword: str | None = None,
    ) -> tuple[list[PromptOut], int]:
        """列表。**必须过滤 `deleted == 0`** —— 这张表靠 `deleted` 软删，
        而 Java 的 `@TableLogic` 会自动加这个条件，手写 SQL 时很容易忘。
        """
        conditions = [Prompt.deleted == 0, Prompt.user_id == user_id]
        if keyword and keyword.strip():
            like = f"%{keyword.strip()}%"
            conditions.append(
                or_(Prompt.prompt_key.ilike(like), Prompt.description.ilike(like))
            )

        total = int(
            (
                await self.db.execute(
                    select(func.count()).select_from(Prompt).where(*conditions)
                )
            ).scalar()
            or 0
        )
        rows = (
            await self.db.execute(
                select(Prompt)
                .where(*conditions)
                # latest_version 是 varchar，不能按它排序（"v10" < "v2"）
                .order_by(Prompt.create_time.desc().nulls_last(), Prompt.id.desc())
                .offset((page_num - 1) * page_size)
                .limit(page_size)
            )
        ).scalars().all()
        return [PromptOut.model_validate(r) for r in rows], total

    async def _require(self, prompt_id: int) -> Prompt:
        row = (
            await self.db.execute(
                select(Prompt).where(Prompt.id == prompt_id, Prompt.deleted == 0)
            )
        ).scalar_one_or_none()
        if row is None:
            raise _not_found("Prompt 不存在")
        return row

    async def get(self, prompt_id: int) -> PromptOut:
        return PromptOut.model_validate(await self._require(prompt_id))

    async def create(self, dto: PromptCreateIn, user_id: int) -> PromptOut:
        key = dto.prompt_key.strip()
        if not key:
            raise _bad_request("promptKey 不能为空")

        exists = (
            await self.db.execute(
                select(Prompt.id).where(Prompt.prompt_key == key, Prompt.deleted == 0)
            )
        ).first()
        if exists:
            raise _bad_request(f"promptKey 已存在：{key}")

        now = datetime.now()
        row = Prompt(
            id=new_id(),
            prompt_key=key,
            description=dto.description,
            # 新建时还没有任何版本；给 None 而不是 "" —— 前端判断 `latestVersion ? ...` 时
            # 空串会被当成"有版本号"，显示一个空的下拉选项。
            latest_version=None,
            tags=dto.tags,
            user_id=user_id,
            create_time=now,
            update_time=now,
            deleted=0,
        )
        self.db.add(row)
        await self.db.commit()
        return PromptOut.model_validate(row)

    async def update(
        self, prompt_id: int, description: str | None, tags: str | None
    ) -> None:
        """只改描述和标签。

        **不能改 `prompt_key`** —— 版本表靠 key 关联（不是靠外键 id），
        改了 key 所有历史版本就全"失联"了。Java 的 DTO 也就没给这个字段。
        传 None 表示"这个字段不动"（不是"清空"）。
        """
        await self._require(prompt_id)
        values: dict[str, Any] = {"update_time": datetime.now()}
        if description is not None:
            values["description"] = description
        if tags is not None:
            values["tags"] = tags
        await self.db.execute(
            update(Prompt).where(Prompt.id == prompt_id).values(**values)
        )
        await self.db.commit()

    async def delete(self, prompt_id: int) -> int:
        """软删 prompt + 级联软删它的所有版本，**同一事务**。

        不级联会怎样：版本表里留着孤立的 key，用户下次建同名 prompt 时，
        版本列表里立刻冒出上次的旧版本（因为查询只按 prompt_key 过滤）。
        """
        row = await self._require(prompt_id)
        now = datetime.now()
        result = await self.db.execute(
            update(PromptVersion)
            .where(PromptVersion.prompt_key == row.prompt_key, PromptVersion.deleted == 0)
            .values(deleted=1, update_time=now)
        )
        cascaded = int(result.rowcount or 0)
        await self.db.execute(
            update(Prompt)
            .where(Prompt.id == prompt_id)
            .values(deleted=1, update_time=now)
        )
        await self.db.commit()
        logger.info("删除 Prompt %s（级联 %d 个版本）", row.prompt_key, cascaded)
        return cascaded

    # ==================================================================
    # 版本
    # ==================================================================
    async def create_version(self, dto: PromptVersionIn, user_id: int) -> PromptVersionOut:
        key = dto.prompt_key.strip()
        version = dto.version.strip()
        if not key or not version:
            raise _bad_request("promptKey 和 version 都不能为空")

        prompt = (
            await self.db.execute(
                select(Prompt).where(Prompt.prompt_key == key, Prompt.deleted == 0)
            )
        ).scalar_one_or_none()
        if prompt is None:
            raise _bad_request(f"Prompt 不存在：{key}")

        target_status = STATUS_RELEASE if (dto.status or "") == STATUS_RELEASE else STATUS_PRE
        now = datetime.now()

        # 要发正式版：同 key+version 已有正式版就拒绝
        if target_status == STATUS_RELEASE:
            released = (
                await self.db.execute(
                    select(PromptVersion.id).where(
                        PromptVersion.prompt_key == key,
                        PromptVersion.version == version,
                        PromptVersion.status == STATUS_RELEASE,
                        PromptVersion.deleted == 0,
                    )
                )
            ).first()
            if released:
                raise _bad_request(f"版本 {version} 已经是正式版，不能再发一次")

        # 同 key+version 的预发布版本：软删（允许覆盖重来）
        await self.db.execute(
            update(PromptVersion)
            .where(
                PromptVersion.prompt_key == key,
                PromptVersion.version == version,
                PromptVersion.status == STATUS_PRE,
                PromptVersion.deleted == 0,
            )
            .values(deleted=1, update_time=now)
        )

        # jsonb 字段：入参是 JSON 字符串，这里解析成对象再写
        try:
            variables = parse_json_value(dto.variables, default={})
            model_config = parse_json_value(dto.model_config_json, default={})
            tool_config = parse_json_value(dto.tool_config, default={})
        except ValueError as exc:
            raise _bad_request(str(exc)) from exc

        row = PromptVersion(
            id=new_id(),
            prompt_key=key,
            version=version,
            version_desc=dto.version_desc,
            template=dto.template,
            variables=variables,
            model_config=model_config,
            tool_config=tool_config,
            status=target_status,
            user_id=user_id,
            create_time=now,
            update_time=now,
            deleted=0,
        )
        self.db.add(row)

        # 同步 latest_version：前端列表/详情都读它决定"默认打开哪个版本"
        await self.db.execute(
            update(Prompt)
            .where(Prompt.id == prompt.id)
            .values(latest_version=version, update_time=now)
        )
        await self.db.commit()
        return PromptVersionOut.model_validate(row)

    async def list_versions(self, prompt_key: str) -> list[PromptVersionOut]:
        rows = (
            await self.db.execute(
                select(PromptVersion)
                .where(
                    PromptVersion.prompt_key == prompt_key,
                    PromptVersion.deleted == 0,
                )
                .order_by(PromptVersion.create_time.desc().nulls_last(), PromptVersion.id.desc())
            )
        ).scalars().all()
        return [PromptVersionOut.model_validate(r) for r in rows]

    async def get_version(self, prompt_key: str, version: str) -> PromptVersionOut:
        row = (
            await self.db.execute(
                select(PromptVersion).where(
                    PromptVersion.prompt_key == prompt_key,
                    PromptVersion.version == version,
                    PromptVersion.deleted == 0,
                )
                .order_by(PromptVersion.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if row is None:
            raise _not_found(f"版本不存在：{prompt_key}@{version}")
        return PromptVersionOut.model_validate(row)

    # ==================================================================
    # 构建模板
    # ==================================================================
    async def list_templates(self) -> list[PromptTemplateOut]:
        rows = (
            await self.db.execute(
                select(PromptBuildTemplate)
                .where(PromptBuildTemplate.deleted == 0)
                .order_by(PromptBuildTemplate.create_time.desc().nulls_last(),
                          PromptBuildTemplate.id.desc())
            )
        ).scalars().all()
        return [PromptTemplateOut.model_validate(r) for r in rows]

    async def get_template(self, key: str) -> PromptTemplateOut:
        row = (
            await self.db.execute(
                select(PromptBuildTemplate).where(
                    PromptBuildTemplate.prompt_template_key == key,
                    PromptBuildTemplate.deleted == 0,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise _not_found(f"模板不存在：{key}")
        return PromptTemplateOut.model_validate(row)

    async def create_template(self, dto: PromptTemplateIn) -> PromptTemplateOut:
        key = dto.prompt_template_key.strip()
        exists = (
            await self.db.execute(
                select(PromptBuildTemplate.id).where(
                    PromptBuildTemplate.prompt_template_key == key,
                    PromptBuildTemplate.deleted == 0,
                )
            )
        ).first()
        if exists:
            raise _bad_request(f"模板 key 已存在：{key}")

        try:
            model_config = parse_json_value(dto.model_config_json, default={})
            tool_config = parse_json_value(dto.tool_config, default={})
        except ValueError as exc:
            raise _bad_request(str(exc)) from exc

        now = datetime.now()
        row = PromptBuildTemplate(
            id=new_id(),
            prompt_template_key=key,
            tags=dto.tags,
            template_desc=dto.template_desc,
            template=dto.template,
            # 这列是 varchar，原样存（传了合法 JSON 也照原样）
            variables=dto.variables,
            model_config=model_config,
            tool_config=tool_config,
            create_time=now,
            update_time=now,
            deleted=0,
        )
        self.db.add(row)
        await self.db.commit()
        return PromptTemplateOut.model_validate(row)

    async def update_template(self, template_id: int, dto: PromptTemplateIn) -> None:
        row = (
            await self.db.execute(
                select(PromptBuildTemplate).where(
                    PromptBuildTemplate.id == template_id,
                    PromptBuildTemplate.deleted == 0,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise _not_found("模板不存在")

        try:
            model_config = parse_json_value(dto.model_config_json, default=row.model_config or {})
            tool_config = parse_json_value(dto.tool_config, default=row.tool_config or {})
        except ValueError as exc:
            raise _bad_request(str(exc)) from exc

        await self.db.execute(
            update(PromptBuildTemplate)
            .where(PromptBuildTemplate.id == template_id)
            .values(
                tags=dto.tags,
                template_desc=dto.template_desc,
                template=dto.template,
                variables=dto.variables,
                model_config=model_config,
                tool_config=tool_config,
                update_time=datetime.now(),
            )
        )
        await self.db.commit()

    async def delete_template(self, template_id: int) -> None:
        result = await self.db.execute(
            update(PromptBuildTemplate)
            .where(
                PromptBuildTemplate.id == template_id,
                PromptBuildTemplate.deleted == 0,
            )
            .values(deleted=1, update_time=datetime.now())
        )
        if int(result.rowcount or 0) == 0:
            raise _not_found("模板不存在")
        await self.db.commit()


# ----------------------------------------------------------------------
# 供别处复用：把 jsonb 字段转成前端要的字符串（调试运行时回显用）
# ----------------------------------------------------------------------
def jsonb_text(value: Any) -> str:
    return to_json_text(value)
