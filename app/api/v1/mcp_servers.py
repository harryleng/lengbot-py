"""MCP 服务器管理接口（10 个，对齐前端 `src/api/mcp.js`）。

7 个实现 + 3 个明确报错（原因见 `services/mcp_server_service.py` 模块 docstring）。

**路由顺序**：`/{server_id}/tools/{tool_name}/toggle` 是四段，`/{server_id}` 是一段，
不会冲突。但静态路径如果以后要加（比如 `/presets`），记得放前面。
"""

from __future__ import annotations

from fastapi import APIRouter, Query

from app.api.deps import CurrentUserId, DbSession
from app.schemas.common import PageResult, Result
from app.schemas.mcp_server import McpServerIn, McpServerOut
from app.services.mcp_server_service import McpServerService

router = APIRouter(prefix="/api/mcp-servers", tags=["mcp-servers"])


def _svc(db: DbSession) -> McpServerService:
    return McpServerService(db)


@router.get("", response_model=Result[PageResult[McpServerOut]], summary="分页查询 MCP 服务器")
async def page(
    db: DbSession,
    user_id: CurrentUserId,
    page_num: int = Query(1, alias="pageNum"),
    page_size: int = Query(20, alias="pageSize"),
    keyword: str | None = Query(None),
) -> Result[PageResult[McpServerOut]]:
    return Result.ok(await _svc(db).page(page_num, page_size, keyword))


@router.post("", response_model=Result[McpServerOut], summary="新增 MCP 服务器")
async def create(dto: McpServerIn, db: DbSession, user_id: CurrentUserId) -> Result[McpServerOut]:
    return Result.ok(await _svc(db).create(dto))


@router.put("", response_model=Result[McpServerOut], summary="更新 MCP 服务器")
async def update(dto: McpServerIn, db: DbSession, user_id: CurrentUserId) -> Result[McpServerOut]:
    return Result.ok(await _svc(db).update(dto))


@router.get("/{server_id}", response_model=Result[McpServerOut], summary="获取单个 MCP 服务器")
async def get_one(server_id: int, db: DbSession, user_id: CurrentUserId) -> Result[McpServerOut]:
    return Result.ok(await _svc(db).get(server_id))


@router.delete("/{server_id}", response_model=Result[None], summary="删除（内置不可删）")
async def delete(server_id: int, db: DbSession, user_id: CurrentUserId) -> Result[None]:
    await _svc(db).delete(server_id)
    return Result.ok(None)


@router.put("/{server_id}/enabled", response_model=Result[None], summary="启用 / 禁用")
async def set_enabled(
    server_id: int,
    db: DbSession,
    user_id: CurrentUserId,
    enabled: bool = Query(..., description="true 启用 / false 禁用"),
) -> Result[None]:
    await _svc(db).set_enabled(server_id, enabled)
    return Result.ok(None)


@router.put("/{server_id}/tools/{tool_name}/toggle", response_model=Result[None],
            summary="切换单个工具的启用状态")
async def toggle_tool(
    server_id: int,
    tool_name: str,
    db: DbSession,
    user_id: CurrentUserId,
) -> Result[None]:
    """不连 server，只维护 `disabled_tools` 黑名单数组。"""
    await _svc(db).toggle_tool(server_id, tool_name)
    return Result.ok(None)


# ----------------------------------------------------------------------
# 需要 MCP 客户端（明确报错，不装没事）
# ----------------------------------------------------------------------


@router.post("/{server_id}/test", response_model=Result[list[dict]], summary="测试连接（未实现）")
async def test_connection(
    server_id: int, db: DbSession, user_id: CurrentUserId
) -> Result[list[dict]]:
    return Result.ok(await _svc(db).test_connection(server_id))


@router.get("/{server_id}/tools", response_model=Result[list[dict]], summary="工具列表（未实现）")
async def list_tools(
    server_id: int, db: DbSession, user_id: CurrentUserId
) -> Result[list[dict]]:
    return Result.ok(await _svc(db).list_tools(server_id))


@router.post("/{server_id}/tools/refresh", response_model=Result[list[dict]],
             summary="刷新工具列表（未实现）")
async def refresh_tools(
    server_id: int, db: DbSession, user_id: CurrentUserId
) -> Result[list[dict]]:
    return Result.ok(await _svc(db).refresh_tools(server_id))
