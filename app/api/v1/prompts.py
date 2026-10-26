"""Prompt 管理接口（13 个，对齐前端 `src/api/prompt.js`）。

**路由顺序是这个文件的头号坑**（同一个坑在 `model_providers` / `chat_sessions` /
`agents` 都踩过）：FastAPI 按注册顺序匹配，不像 Spring 优先精确路径。

    GET  /api/prompts/templates      ← 静态，必须最先
    GET  /api/prompts/{prompt_id}    ← 动态 int，必须最后

反过来的话 `GET /api/prompts/templates` 会被 `/{prompt_id}` 吃掉 →
`int("templates")` 失败 → 422，前端只看到"参数错误"，模板页永远空。

另外两个不明显的点：

1. `PUT /api/prompts?id=x` 的 **id 在 query 里**（Java `@RequestParam Long id`），
   不在 body。写成 body 会 422。
2. `DELETE /api/prompts/{id}` 会**级联软删所有版本**，返回前端的 message
   带级联条数（排障时能看出"删了 3 个版本"）。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Query

from app.api.deps import CurrentUserId, DbSession
from app.schemas.common import PageResult, Result
from app.schemas.prompt import (
    PromptCreateIn,
    PromptOut,
    PromptTemplateIn,
    PromptTemplateOut,
    PromptVersionIn,
    PromptVersionOut,
)
from app.services.prompt_service import PromptService

router = APIRouter(prefix="/api/prompts", tags=["prompts"])


def _svc(db: DbSession) -> PromptService:
    return PromptService(db)


# ======================================================================
# ① 模板：静态路径，必须全部排在 /{prompt_id} 之前
# ======================================================================


@router.get("/templates", response_model=Result[list[PromptTemplateOut]], summary="构建模板列表")
async def list_templates(db: DbSession, user_id: CurrentUserId) -> Result[list[PromptTemplateOut]]:
    return Result.ok(await _svc(db).list_templates())


@router.post("/templates", response_model=Result[PromptTemplateOut], summary="新建构建模板")
async def create_template(
    dto: PromptTemplateIn, db: DbSession, user_id: CurrentUserId
) -> Result[PromptTemplateOut]:
    return Result.ok(await _svc(db).create_template(dto))


@router.put("/templates", response_model=Result[None], summary="更新构建模板（id 在 query）")
async def update_template(
    db: DbSession,
    user_id: CurrentUserId,
    template_id: Annotated[int, Query(alias="id")],
    dto: Annotated[PromptTemplateIn, Body()],
) -> Result[None]:
    await _svc(db).update_template(template_id, dto)
    return Result.ok(None)


@router.get(
    "/templates/{key}", response_model=Result[PromptTemplateOut], summary="按 key 取构建模板"
)
async def get_template(key: str, db: DbSession, user_id: CurrentUserId) -> Result[PromptTemplateOut]:
    return Result.ok(await _svc(db).get_template(key))


@router.delete("/templates/{template_id}", response_model=Result[None], summary="删除构建模板")
async def delete_template(
    template_id: int, db: DbSession, user_id: CurrentUserId
) -> Result[None]:
    await _svc(db).delete_template(template_id)
    return Result.ok(None)


# ======================================================================
# ② 版本：/versions/detail 和 /{key}/versions 都是静态首段，也要排在 /{id} 前
# ======================================================================


@router.post("/versions", response_model=Result[PromptVersionOut], summary="新建版本")
async def create_version(
    dto: PromptVersionIn, db: DbSession, user_id: CurrentUserId
) -> Result[PromptVersionOut]:
    """四步规则见 `PromptService.create_version`。

    `status` 传 `release` 表示直接发正式版；其它（含不传）都当 `pre`。
    """
    return Result.ok(await _svc(db).create_version(dto, user_id))


@router.get(
    "/versions/detail", response_model=Result[PromptVersionOut], summary="版本详情（key + version）"
)
async def get_version_detail(
    db: DbSession,
    user_id: CurrentUserId,
    prompt_key: Annotated[str, Query(alias="promptKey")],
    version: Annotated[str, Query()],
) -> Result[PromptVersionOut]:
    return Result.ok(await _svc(db).get_version(prompt_key, version))


@router.get(
    "/{prompt_key}/versions",
    response_model=Result[list[PromptVersionOut]],
    summary="某个 prompt 的所有版本",
)
async def list_versions(
    prompt_key: str, db: DbSession, user_id: CurrentUserId
) -> Result[list[PromptVersionOut]]:
    return Result.ok(await _svc(db).list_versions(prompt_key))


# ======================================================================
# ③ 主表
# ======================================================================


@router.get("", response_model=Result[PageResult[PromptOut]], summary="Prompt 列表")
async def list_prompts(
    db: DbSession,
    user_id: CurrentUserId,
    page_num: Annotated[int, Query(alias="pageNum")] = 1,
    page_size: Annotated[int, Query(alias="pageSize")] = 20,
    keyword: Annotated[str | None, Query()] = None,
) -> Result[PageResult[PromptOut]]:
    records, total = await _svc(db).list(user_id, page_num, page_size, keyword)
    return Result.ok(
        PageResult.of(records=records, total=total, current=page_num, size=page_size)
    )


@router.post("", response_model=Result[PromptOut], summary="新建 Prompt")
async def create_prompt(
    dto: PromptCreateIn, db: DbSession, user_id: CurrentUserId
) -> Result[PromptOut]:
    return Result.ok(await _svc(db).create(dto, user_id))


@router.put("", response_model=Result[None], summary="更新 Prompt（id 在 query，只改描述/标签）")
async def update_prompt(
    db: DbSession,
    user_id: CurrentUserId,
    prompt_id: Annotated[int, Query(alias="id")],
    dto: Annotated[PromptCreateIn, Body()],
) -> Result[None]:
    await _svc(db).update(prompt_id, dto.description, dto.tags)
    return Result.ok(None)


# ======================================================================
# ④ 动态 detail —— 必须最后
# ======================================================================


@router.get("/{prompt_id}", response_model=Result[PromptOut], summary="Prompt 详情")
async def get_prompt(prompt_id: int, db: DbSession, user_id: CurrentUserId) -> Result[PromptOut]:
    return Result.ok(await _svc(db).get(prompt_id))


@router.delete("/{prompt_id}", response_model=Result[str], summary="删除 Prompt（级联软删版本）")
async def delete_prompt(
    prompt_id: int, db: DbSession, user_id: CurrentUserId
) -> Result[str]:
    cascaded = await _svc(db).delete(prompt_id)
    return Result.ok(f"已删除，同时级联删除 {cascaded} 个版本")
