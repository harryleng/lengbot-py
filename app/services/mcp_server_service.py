"""MCP 服务器管理：CRUD + 启停 + 工具开关。

**范围说明（重要）**：10 个接口里实现了 7 个，3 个需要真连 MCP server。

| 接口 | 状态 | 说明 |
|---|---|---|
| 列表 / 详情 / 新建 / 更新 / 删除 | ✅ | 纯 CRUD |
| `PUT /{id}/enabled` | ✅ | 改 status |
| `PUT /{id}/tools/{name}/toggle` | ✅ | 只改 `disabled_tools` jsonb 数组，不连接 |
| `POST /{id}/test` | ❌ 明确报错 | 要 MCP 握手，需要 `mcp` SDK |
| `GET /{id}/tools` | ❌ 明确报错 | 工具列表是**运行时从 server 拉的**，不落库 |
| `POST /{id}/tools/refresh` | ❌ 明确报错 | 同上 |

为什么后 3 个不做：没有 `mcp_tool` 表，工具列表只能实时连接获取。
而库里两个 MCP server **都是 `disabled`**，其中一个（mcp-server-chart）
靠 `npx @antv/mcp-server-chart` 起子进程 —— 要先装 node 包才能连。
实现一套连不通的客户端代码，既验证不了，也给后来人留个"看起来能用"的坑。

所以这 3 个接口**明确抛错并说明原因**，而不是返回空数组装作没事：

    返回 [] 的后果：前端显示"该服务器下暂无工具"，
    用户以为是 server 自己没工具，去改 server 配置 —— 排查方向完全跑偏。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.snowflake import new_id
from app.models.entities import McpServer
from app.schemas.common import PageResult
from app.schemas.mcp_server import McpServerIn, McpServerOut

logger = logging.getLogger(__name__)

# 库里的内置 server（sequentialthinking / mcp-server-chart）不允许删
_UNSUPPORTED = (
    "Python 版尚未接入 MCP 客户端，无法{action}。"
    "工具列表不落库、只能运行时从 server 拉取，需要先安装 `mcp` SDK 并确保 "
    "server 可连通（库里现有的两个 server 都处于 disabled，其中一个需 npx 起进程）。"
)


def _parse_json(raw: Any, default: Any = None) -> Any:
    if raw is None:
        return default if default is not None else {}
    if isinstance(raw, (dict, list)):
        return raw
    text = str(raw).strip()
    if not text:
        return default if default is not None else {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        logger.warning("MCP 字段 JSON 解析失败，按默认值处理: %s", text[:100])
        return default if default is not None else {}


def _parse_disabled(raw: Any) -> list[str]:
    """`disabled_tools` 归一成字符串数组（可能是 null / 字符串 / 数组）。"""
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(x) for x in raw if x is not None]
    text = str(raw).strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return []
    return _parse_disabled(parsed)


class McpServerService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    async def page(self, page_num: int, page_size: int, keyword: str | None = None) -> PageResult:
        conditions = [McpServer.deleted == 0]
        if keyword:
            like = f"%{keyword}%"
            conditions.append(or_(McpServer.name.ilike(like), McpServer.description.ilike(like)))

        total = (
            await self.db.execute(
                select(func.count()).select_from(McpServer).where(*conditions)
            )
        ).scalar_one()
        rows = (
            (
                await self.db.execute(
                    select(McpServer)
                    .where(*conditions)
                    .order_by(McpServer.create_time.desc(), McpServer.id.desc())
                    .offset((page_num - 1) * page_size)
                    .limit(page_size)
                )
            )
            .scalars()
            .all()
        )
        return PageResult.of(
            records=[McpServerOut.model_validate(r) for r in rows],
            total=int(total),
            current=page_num,
            size=page_size,
        )

    async def get(self, server_id: int) -> McpServerOut:
        return McpServerOut.model_validate(await self._require(server_id))

    # ------------------------------------------------------------------
    # 写操作
    # ------------------------------------------------------------------

    async def create(self, dto: McpServerIn) -> McpServerOut:
        await self._ensure_name_free(dto.name, None)
        now = datetime.now()
        row = McpServer(
            id=new_id(),
            name=dto.name,
            description=dto.description,
            icon=dto.icon,
            install_type=dto.install_type or "npx",
            deploy_config=_parse_json(dto.deploy_config),
            detail_config=_parse_json(dto.detail_config),
            host=dto.host,
            status="active",
            transport=dto.transport or "stdio",
            headers=_parse_json(dto.headers),
            disabled_tools=_parse_disabled(dto.disabled_tools),
            create_time=now,
            update_time=now,
            deleted=0,
            is_builtin=0,  # 用户新建的一律非内置
        )
        self.db.add(row)
        await self.db.commit()
        await self.db.refresh(row)
        logger.info("创建 MCP server %s（transport=%s）", row.name, row.transport)
        return McpServerOut.model_validate(row)

    async def update(self, dto: McpServerIn) -> McpServerOut:
        if not dto.id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="缺少 id")
        row = await self._require(int(dto.id))

        if dto.name and dto.name != row.name:
            await self._ensure_name_free(dto.name, exclude_id=row.id)
            row.name = dto.name
        row.description = dto.description
        row.icon = dto.icon
        row.install_type = dto.install_type or row.install_type
        row.deploy_config = _parse_json(dto.deploy_config)
        row.detail_config = _parse_json(dto.detail_config)
        row.host = dto.host
        row.transport = dto.transport or row.transport
        row.headers = _parse_json(dto.headers)
        if dto.disabled_tools is not None:
            row.disabled_tools = _parse_disabled(dto.disabled_tools)
        row.update_time = datetime.now()

        await self.db.commit()
        await self.db.refresh(row)
        logger.info("更新 MCP server %s", row.name)
        return McpServerOut.model_validate(row)

    async def delete(self, server_id: int) -> None:
        row = await self._require(server_id)
        if int(row.is_builtin or 0) == 1:
            # 内置的删了会导致引用它的 Agent/工作流全部失效，且没法恢复
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="内置 MCP 服务器不能删除，只能禁用",
            )
        row.deleted = 1
        row.update_time = datetime.now()
        await self.db.commit()
        logger.info("删除 MCP server %s（软删）", row.id)

    async def set_enabled(self, server_id: int, enabled: bool) -> None:
        row = await self._require(server_id)
        row.status = "active" if enabled else "disabled"
        row.update_time = datetime.now()
        await self.db.commit()

    async def toggle_tool(self, server_id: int, tool_name: str) -> None:
        """切换某个工具的启用状态（在 `disabled_tools` 里增删）。

        这个操作**不需要连 server** —— 它只是维护一个"黑名单"数组，
        等真正加载工具时按这个名单过滤。所以能完整实现。
        """
        row = await self._require(server_id)
        disabled = _parse_disabled(row.disabled_tools)
        name = (tool_name or "").strip()
        if not name:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="toolName 不能为空"
            )
        if name in disabled:
            disabled.remove(name)
        else:
            disabled.append(name)
        row.disabled_tools = disabled
        row.update_time = datetime.now()
        await self.db.commit()
        logger.info(
            "MCP 工具开关: server=%s tool=%s enabled=%s", server_id, name, name not in disabled
        )

    # ------------------------------------------------------------------
    # 需要 MCP 客户端（未实现，明确报错）
    # ------------------------------------------------------------------

    async def test_connection(self, server_id: int) -> list[dict]:
        await self._require(server_id)  # 先校验存在性，404 比"未实现"更准确
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=_UNSUPPORTED.format(action="测试连接"),
        )

    async def list_tools(self, server_id: int) -> list[dict]:
        await self._require(server_id)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=_UNSUPPORTED.format(action="列出工具"),
        )

    async def refresh_tools(self, server_id: int) -> list[dict]:
        await self._require(server_id)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=_UNSUPPORTED.format(action="刷新工具列表"),
        )

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    async def _require(self, server_id: int) -> McpServer:
        row = (
            await self.db.execute(
                select(McpServer).where(
                    McpServer.id == server_id, McpServer.deleted == 0
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="MCP 服务器不存在"
            )
        return row

    async def _ensure_name_free(self, name: str, exclude_id: int | None) -> None:
        stmt = select(func.count()).select_from(McpServer).where(
            McpServer.name == name, McpServer.deleted == 0
        )
        if exclude_id is not None:
            stmt = stmt.where(McpServer.id != exclude_id)
        if int((await self.db.execute(stmt)).scalar_one()) > 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"MCP 服务器名 {name} 已存在",
            )
