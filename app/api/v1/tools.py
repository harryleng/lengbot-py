"""工具管理接口（9 个，对齐前端 `src/api/tool.js`）。

路由顺序：`/{tool_id}/xxx` 比 `/{tool_id}` 更具体，但 FastAPI 按注册顺序匹配时
只要段数不同就不会冲突（`/{tool_id}/test` 是两段、`/{tool_id}` 是一段）。
这里没有 `model-providers` 那种静态前缀坑，但**把 list/create/update 放前面**
仍然更安全 —— 以后有人加 `/types` 之类的静态路径不会踩。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Query

from app.api.deps import CurrentUserId, DbSession
from app.schemas.common import PageResult, Result
from app.schemas.tool import ToolIn, ToolOut
from app.services.tool_service import ToolService

router = APIRouter(prefix="/api/tools", tags=["tools"])


def _svc(db: DbSession) -> ToolService:
    return ToolService(db)


@router.get("", response_model=Result[PageResult[ToolOut]], summary="分页查询工具（支持关键字/类型/标签）")
async def page(
    db: DbSession,
    user_id: CurrentUserId,
    page_num: int = Query(1, alias="pageNum"),
    page_size: int = Query(100, alias="pageSize"),
    keyword: str | None = Query(None),
    tool_type: str | None = Query(None, alias="toolType"),
    tag: str | None = Query(None),
) -> Result[PageResult[ToolOut]]:
    """`pageSize` 默认 100（Java 同），因为工具总共才 32 个，前端一页全展示。"""
    return Result.ok(await _svc(db).page(page_num, page_size, keyword, tool_type, tag))


@router.post("", response_model=Result[ToolOut], summary="新增工具")
async def create(dto: ToolIn, db: DbSession, user_id: CurrentUserId) -> Result[ToolOut]:
    return Result.ok(await _svc(db).create(dto))


@router.put("", response_model=Result[ToolOut], summary="更新工具")
async def update(dto: ToolIn, db: DbSession, user_id: CurrentUserId) -> Result[ToolOut]:
    """注意两条业务规则（见 `tool_service.py` 模块 docstring）：

    - `knowledge` 类型直接 400（由知识库自动生成，改了会被覆盖）
    - `builtin` 类型只落限流字段（其余字段静默忽略）
    """
    return Result.ok(await _svc(db).update(dto))


@router.get("/{tool_id}", response_model=Result[ToolOut], summary="获取单个工具")
async def get_one(tool_id: int, db: DbSession, user_id: CurrentUserId) -> Result[ToolOut]:
    return Result.ok(await _svc(db).get(tool_id))


@router.delete("/{tool_id}", response_model=Result[None], summary="删除工具（软删）")
async def delete(tool_id: int, db: DbSession, user_id: CurrentUserId) -> Result[None]:
    await _svc(db).delete(tool_id)
    return Result.ok(None)


@router.put("/{tool_id}/enabled", response_model=Result[None], summary="启用 / 禁用")
async def set_enabled(
    tool_id: int,
    db: DbSession,
    user_id: CurrentUserId,
    enabled: bool = Query(..., description="true 启用 / false 禁用"),
) -> Result[None]:
    await _svc(db).set_enabled(tool_id, enabled)
    return Result.ok(None)


@router.post("/{tool_id}/test", response_model=Result[str], summary="测试执行工具")
async def test(
    tool_id: int,
    db: DbSession,
    user_id: CurrentUserId,
    body: Annotated[dict[str, Any], Body()] = None,  # type: ignore[assignment]
) -> Result[str]:
    """body 形如 `{"args": {...}}`，`args` 可以是对象也可以是 JSON 字符串。

    失败**不抛 500**：把工具的原始错误输出当正常返回给前端，
    让用户在弹窗里看到真实报错（对齐 Java 的行为）。

    `body` 用 `Annotated[..., Body()]` 而不是 `= Body(...)`：
    后者会被 ruff B008 拦（"不要在默认值里调函数"），而且 Annotated 也是
    FastAPI 现在的推荐写法。
    """
    return Result.ok(await _svc(db).test(tool_id, (body or {}).get("args")))


@router.get("/{tool_id}/io-schema", response_model=Result[dict], summary="输入/输出参数列表（工作流映射用）")
async def io_schema(tool_id: int, db: DbSession, user_id: CurrentUserId) -> Result[dict]:
    return Result.ok(await _svc(db).io_schema(tool_id))


@router.get("/{tool_id}/example", response_model=Result[dict], summary="示例参数（测试弹窗预填）")
async def example(tool_id: int, db: DbSession, user_id: CurrentUserId) -> Result[dict]:
    return Result.ok(await _svc(db).example_params(tool_id))
