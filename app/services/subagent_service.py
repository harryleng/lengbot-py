"""SubAgent 管理服务（CRUD + 启停 + 运行记录）。

对应 Java `SubAgentServiceImpl` + `SubAgentTaskServiceImpl` 的只读部分。

**三条业务规则，错了会静默出错**：

1. **内置 SubAgent（`is_builtin=1`）只能改模型配置**。
   名称、提示词这些字段由代码里的注册器同步，用户改了保存"成功"，
   下次重启就被覆盖 —— 修改丢了还不报错。

2. **内置 SubAgent 不可删除**，但**可以启停**（Java 的 `setEnabled` 没拦）。

3. `toolIds` 提交时要**剔除已删除的工具**。
   Java 的 `cleanStaleToolIds` 做这件事；不剔除的话，前端会认为
   "有悬空引用"并弹警告，而用户明明只是在删过一个工具之后没重存。

另外 `update` 里有个不对称处理：`modelId` 为 null 时要**单独清空**
`model_id` 和 `llm_model` 两列。常规的"null 表示不修改"在这里不适用 ——
用户点"继承主 Agent"就是要清空，按"不改"处理会永远切不回去。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.snowflake import new_id
from app.models.entities import SubAgent, Tool
from app.schemas.subagent import SubAgentIn, SubAgentOut

logger = logging.getLogger(__name__)

# 与 Java 常量一致（库里 DDL 默认值也是这个）
DEFAULT_CONNECT_TIMEOUT = 10
DEFAULT_READ_TIMEOUT = 60
DEFAULT_RETRY_TIMES = 1


def _bad_request(msg: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=msg)


def _not_found(msg: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=msg)


def _clamp(value: int | None, low: int, high: int, default: int) -> int:
    if value is None:
        return default
    return max(low, min(high, int(value)))


class SubAgentService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # ==================================================================
    # 查询
    # ==================================================================
    async def list(
        self,
        page_num: int = 1,
        page_size: int = 100,
        keyword: str | None = None,
        is_builtin: bool | None = None,
    ) -> tuple[list[SubAgentOut], int]:
        """列表**不过滤 user_id**。

        Java 的 `listPage` 只按 keyword / isBuiltin 过滤，不按用户 ——
        子智能体是全局共享资源（Agent 绑定下拉要能看到所有人建的）。
        照抄，别自作聪明加 user_id 条件。
        """
        conditions: list[Any] = [SubAgent.deleted == 0]
        if keyword and keyword.strip():
            like = f"%{keyword.strip()}%"
            conditions.append(
                or_(SubAgent.name.ilike(like), SubAgent.display_name.ilike(like))
            )
        if is_builtin is not None:
            conditions.append(SubAgent.is_builtin == (1 if is_builtin else 0))

        total = int(
            (
                await self.db.execute(
                    select(func.count()).select_from(SubAgent).where(*conditions)
                )
            ).scalar()
            or 0
        )
        rows = (
            await self.db.execute(
                select(SubAgent)
                .where(*conditions)
                .order_by(SubAgent.create_time.desc().nulls_last(), SubAgent.id.desc())
                .offset((page_num - 1) * page_size)
                .limit(page_size)
            )
        ).scalars().all()
        return [SubAgentOut.model_validate(r) for r in rows], total

    async def list_enabled(self) -> list[SubAgentOut]:
        rows = (
            await self.db.execute(
                select(SubAgent)
                .where(SubAgent.deleted == 0, SubAgent.enabled == 1)
                .order_by(SubAgent.create_time.desc().nulls_last(), SubAgent.id.desc())
            )
        ).scalars().all()
        return [SubAgentOut.model_validate(r) for r in rows]

    async def _require(self, sub_id: int) -> SubAgent:
        row = (
            await self.db.execute(
                select(SubAgent).where(SubAgent.id == sub_id, SubAgent.deleted == 0)
            )
        ).scalar_one_or_none()
        if row is None:
            raise _not_found("SubAgent 不存在")
        return row

    async def get(self, sub_id: int) -> SubAgentOut:
        return SubAgentOut.model_validate(await self._require(sub_id))

    # ==================================================================
    # 写
    # ==================================================================
    async def _ensure_name_free(self, name: str, exclude_id: int | None = None) -> None:
        conditions = [SubAgent.name == name, SubAgent.deleted == 0]
        if exclude_id is not None:
            conditions.append(SubAgent.id != exclude_id)
        hit = (await self.db.execute(select(SubAgent.id).where(*conditions))).first()
        if hit:
            raise _bad_request(f"SubAgent 标识已存在：{name}")

    async def _clean_stale_tool_ids(self, tool_ids: list[str] | None) -> list[str]:
        """剔除已不存在的工具 id（对齐 Java `cleanStaleToolIds`）。

        注意入参可能是 19 位雪花字符串：直接 `int()` 转换后再查库，
        避免把字符串塞进 bigint 比较时 PG 报类型错。
        """
        if not tool_ids:
            return []
        valid: list[int] = []
        for raw in tool_ids:
            try:
                valid.append(int(raw))
            except (TypeError, ValueError):
                continue
        if not valid:
            return []
        rows = (
            await self.db.execute(
                select(Tool.id).where(Tool.id.in_(valid), Tool.deleted == 0)
            )
        ).scalars().all()
        existing = {str(x) for x in rows}
        # 保持用户提交的顺序（前端展示顺序就是它）
        return [str(x) for x in tool_ids if str(x) in existing]

    async def create(self, dto: SubAgentIn, user_id: int) -> SubAgentOut:
        name = dto.name.strip()
        await self._ensure_name_free(name)

        tool_ids = await self._clean_stale_tool_ids(dto.tool_ids)
        now = datetime.now()
        row = SubAgent(
            id=new_id(),
            name=name,
            display_name=dto.display_name.strip(),
            description=dto.description.strip(),
            system_prompt=dto.system_prompt,
            tool_ids=tool_ids,
            model_id=dto.resolve_provider_id(),
            llm_model=dto.llm_model,
            connect_timeout_seconds=_clamp(
                dto.connect_timeout_seconds, 1, 60, DEFAULT_CONNECT_TIMEOUT
            ),
            read_timeout_seconds=_clamp(
                dto.read_timeout_seconds, 10, 300, DEFAULT_READ_TIMEOUT
            ),
            model_retry_times=_clamp(
                dto.model_retry_times, 0, 10, DEFAULT_RETRY_TIMES
            ),
            enabled=0 if dto.enabled is False else 1,
            # 新建的一律非内置（内置由代码注册器维护）
            is_builtin=0,
            icon=dto.icon,
            user_id=user_id,
            create_time=now,
            update_time=now,
            deleted=0,
        )
        self.db.add(row)
        await self.db.commit()
        return SubAgentOut.model_validate(row)

    async def update(self, dto: SubAgentIn) -> SubAgentOut:
        if dto.id is None:
            raise _bad_request("缺少 id")
        sub_id = int(dto.id)
        row = await self._require(sub_id)

        provider_id = dto.resolve_provider_id()

        # 内置：只允许改模型配置，其余字段一律保持原值
        if row.is_builtin == 1:
            await self.db.execute(
                update(SubAgent)
                .where(SubAgent.id == sub_id)
                .values(
                    model_id=provider_id,
                    llm_model=dto.llm_model if provider_id is not None else None,
                    update_time=datetime.now(),
                )
            )
            await self.db.commit()
            return SubAgentOut.model_validate(await self._require(sub_id))

        name = dto.name.strip()
        if name != row.name:
            await self._ensure_name_free(name, exclude_id=sub_id)

        tool_ids = await self._clean_stale_tool_ids(dto.tool_ids)
        values: dict[str, Any] = {
            "name": name,
            "display_name": dto.display_name.strip(),
            "description": dto.description.strip(),
            "system_prompt": dto.system_prompt,
            "tool_ids": tool_ids,
            "connect_timeout_seconds": _clamp(
                dto.connect_timeout_seconds, 1, 60, DEFAULT_CONNECT_TIMEOUT
            ),
            "read_timeout_seconds": _clamp(
                dto.read_timeout_seconds, 10, 300, DEFAULT_READ_TIMEOUT
            ),
            "model_retry_times": _clamp(dto.model_retry_times, 0, 10, DEFAULT_RETRY_TIMES),
            "icon": dto.icon,
            "update_time": datetime.now(),
        }
        if dto.enabled is not None:
            values["enabled"] = 1 if dto.enabled else 0
        # 关键：providerId 为 null 表示"继承主 Agent"，必须清空两列。
        # 按"null 不改"处理的话，用户永远切不回继承模式。
        values["model_id"] = provider_id
        values["llm_model"] = dto.llm_model if provider_id is not None else None

        await self.db.execute(
            update(SubAgent).where(SubAgent.id == sub_id).values(**values)
        )
        await self.db.commit()
        return SubAgentOut.model_validate(await self._require(sub_id))

    async def delete(self, sub_id: int) -> None:
        row = await self._require(sub_id)
        if row.is_builtin == 1:
            raise _bad_request("内置 SubAgent 不可删除")
        await self.db.execute(
            update(SubAgent)
            .where(SubAgent.id == sub_id)
            .values(deleted=1, update_time=datetime.now())
        )
        await self.db.commit()

    async def set_enabled(self, sub_id: int, enabled: bool) -> None:
        """启停。**内置也允许**（Java 的 setEnabled 只校验归属，没拦内置）。"""
        await self._require(sub_id)
        await self.db.execute(
            update(SubAgent)
            .where(SubAgent.id == sub_id)
            .values(enabled=1 if enabled else 0, update_time=datetime.now())
        )
        await self.db.commit()

    # ==================================================================
    # 存量数据自检用
    # ==================================================================
    async def stale_tool_refs(self) -> list[tuple[str, list[str]]]:
        """返回 [(subagent.name, 悬空 toolId 列表)]，给自检脚本用。"""
        rows = (
            await self.db.execute(
                select(SubAgent.name, SubAgent.tool_ids).where(SubAgent.deleted == 0)
            )
        ).all()
        if not rows:
            return []
        valid = {
            str(x)
            for x in (
                await self.db.execute(select(Tool.id).where(Tool.deleted == 0))
            ).scalars().all()
        }
        out: list[tuple[str, list[str]]] = []
        for name, tool_ids in rows:
            ids = tool_ids or []
            if isinstance(ids, str):
                try:
                    ids = json.loads(ids)
                except json.JSONDecodeError:
                    ids = []
            stale = [str(x) for x in ids if str(x) not in valid]
            if stale:
                out.append((name, stale))
        return out
