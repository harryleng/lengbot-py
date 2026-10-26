"""工具注册表：把"库里配了哪些工具"和"代码里实现了哪些工具"对上。

设计要点（值得细看）：

**1. 谁是工具清单的事实来源？**
`agent.config.tools` 里存的是工具 ID 数组 —— 这是用户在界面上勾选的，
必须尊重。所以流程是「从库读 ID → 按 name 找实现」，
而不是「把代码里所有工具都塞给模型」。
后者会让模型看到用户根本没启用的工具。

**2. 找不到实现怎么办？**
跳过 + debug 日志，不报错。因为 Python 版只实现了少数工具，
而库里配了 26 个 builtin —— 报错的话每轮对话都炸。
等工具补齐，日志自然就少了。

**3. 为什么还要 verify_schema？**
库里的 `input_schema` 是 Java 侧写的，Python 实现是另一套。
两边参数名/类型不一致时，模型会按 DB 的 schema 传参、
Python 按自己的 model 校验 → 报"缺少字段"。这种 bug 极难查，
所以在启动时主动比对一次，把差异打出来。
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entities import Tool
from app.tools.base import ToolSpec
from app.tools.builtin import BUILTIN_TOOLS
from app.tools.builtin_exec import EXEC_TOOLS
from app.tools.builtin_fs import FILE_TOOLS
from app.tools.builtin_kb import KB_TOOLS
from app.tools.builtin_ocr import OCR_TOOLS
from app.tools.builtin_web import WEB_TOOLS

logger = logging.getLogger(__name__)


class ToolRegistry:
    def __init__(self) -> None:
        self._by_name: dict[str, ToolSpec] = {}

    # ------------------------------------------------------------------
    # 注册
    # ------------------------------------------------------------------

    def register(self, spec: ToolSpec) -> None:
        self._by_name[spec.name] = spec

    def register_all(self, specs: list[ToolSpec]) -> None:
        for spec in specs:
            self.register(spec)

    def get(self, name: str) -> ToolSpec | None:
        return self._by_name.get(name)

    @property
    def implemented(self) -> list[str]:
        return sorted(self._by_name)

    # ------------------------------------------------------------------
    # 给某个 Agent 解析工具
    # ------------------------------------------------------------------

    async def resolve_for_agent(
        self, db: AsyncSession, agent_config: dict[str, Any] | None
    ) -> list[ToolSpec]:
        """按 `agent.config.tools` 里的 ID 列表，返回可用的 ToolSpec。"""
        pairs = await self.resolve_with_meta(db, agent_config)
        return [spec for spec, _ in pairs]

    async def resolve_with_meta(
        self, db: AsyncSession, agent_config: dict[str, Any] | None
    ) -> list[tuple[ToolSpec, Tool]]:
        """同上，但同时返回数据库里的工具记录。

        为什么需要 meta：前端工具面板要显示 `displayName`（中文名）和 `icon`
        （Ant Design 图标组件名），这些只存在库里，代码里没有。
        """
        if not agent_config:
            return []
        raw_ids = agent_config.get("tools") or []
        tool_ids: list[int] = []
        for tid in raw_ids:
            try:
                tool_ids.append(int(tid))
            except (TypeError, ValueError):
                continue
        if not tool_ids:
            return []

        rows = (
            (await db.execute(select(Tool).where(Tool.id.in_(tool_ids), Tool.deleted == 0))).scalars().all()
        )

        pairs: list[tuple[ToolSpec, Tool]] = []
        missing: list[str] = []
        for row in rows:
            if row.status and row.status != "active":
                continue
            spec = self._by_name.get(row.name or "")
            if spec is None:
                missing.append(row.name or f"<id={row.id}>")
                continue
            pairs.append((spec, row))

        if missing:
            # 不报错：Python 版只实现了部分工具，这是预期状态
            logger.debug(
                "Agent 配置了 %d 个工具，其中 %d 个尚无 Python 实现: %s",
                len(rows),
                len(missing),
                ", ".join(missing),
            )

        # 知识库工具自动注入。
        # 对齐 Java 的 @SystemTool 约定："knowledge 类型工具，Agent 绑定知识库时自动注入"
        # —— 它们不在 config.tools 里由用户勾选，而是跟着知识库绑定关系来。
        if agent_config.get("knowledges"):
            seen = {spec.name for spec, _ in pairs}
            kb_rows = (
                (await db.execute(select(Tool).where(Tool.tool_type == "knowledge", Tool.deleted == 0)))
                .scalars()
                .all()
            )
            for row in kb_rows:
                spec = self._by_name.get(row.name or "")
                if spec is not None and spec.name not in seen:
                    pairs.append((spec, row))
                    seen.add(spec.name)

        return pairs

    # ------------------------------------------------------------------
    # schema 一致性校验
    # ------------------------------------------------------------------

    def verify_against_db(self, row: Tool) -> list[str]:
        """比对 DB 里的 input_schema 和代码生成的 schema。返回差异描述（空 = 一致）。"""
        spec = self._by_name.get(row.name or "")
        if spec is None:
            return [f"{row.name}: 无 Python 实现，无法比对"]

        local = spec.db_schema()
        remote = row.input_schema or {}
        diffs: list[str] = []

        local_props = set((local.get("properties") or {}).keys())
        remote_props = set((remote.get("properties") or {}).keys())
        if local_props != remote_props:
            only_local = local_props - remote_props
            only_remote = remote_props - local_props
            if only_local:
                diffs.append(f"仅代码有: {sorted(only_local)}")
            if only_remote:
                diffs.append(f"仅库里有: {sorted(only_remote)}")

        local_required = set(local.get("required") or [])
        remote_required = set(remote.get("required") or [])
        if local_required != remote_required:
            diffs.append(f"必填不一致: 代码={sorted(local_required)} 库={sorted(remote_required)}")

        # 逐个参数比对类型（只比共有参数）
        for prop in local_props & remote_props:
            lt = (local["properties"][prop] or {}).get("type")
            rt = (remote["properties"][prop] or {}).get("type")
            if lt != rt:
                diffs.append(f"{prop}: 类型不同 代码={lt} 库={rt}")

        return diffs


registry = ToolRegistry()
registry.register_all(BUILTIN_TOOLS)
registry.register_all(FILE_TOOLS)
registry.register_all(EXEC_TOOLS)
registry.register_all(WEB_TOOLS)
registry.register_all(OCR_TOOLS)
registry.register_all(KB_TOOLS)
