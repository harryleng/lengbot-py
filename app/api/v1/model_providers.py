"""模型供应商管理接口（15 个，严格对齐前端 `src/api/modelProvider.js`）。

**路由注册顺序是这里唯一容易踩死的坑**：

    /model-providers/presets        ← 静态路径，必须先注册
    /model-providers/default-model
    /model-providers/with-models
    /model-providers/check
    /model-providers/{provider_id}  ← 动态路径，必须最后
    /model-providers/{provider_id}/...

FastAPI 按**注册顺序**匹配，不是像 Spring 那样优先精确路径。如果 `/{provider_id}`
先注册，那 `GET /model-providers/presets` 会被它吃掉，`int` 转换 `"presets"` 失败
→ 422，前端看到的是"参数错误"，完全联想不到是路由顺序问题。

（同样的坑在 `chat_sessions.py` 的 `tool-calls/{id}/result` 那里踩过一次。）
"""

from __future__ import annotations

from fastapi import APIRouter, Query

from app.api.deps import CurrentUserId, DbSession
from app.schemas.common import PageResult, Result
from app.schemas.model_provider import (
    FetchedModelOut,
    ModelProviderCheckIn,
    ModelProviderIn,
    ModelProviderOut,
    ProviderWithModelsOut,
)
from app.services.model_provider_service import ModelProviderService

router = APIRouter(prefix="/api/model-providers", tags=["model-providers"])


def _svc(db: DbSession) -> ModelProviderService:
    return ModelProviderService(db)


# ----------------------------------------------------------------------
# 静态路径优先注册（见模块 docstring）
# ----------------------------------------------------------------------


@router.get("/presets", response_model=Result[list[dict]], summary="提供商预设模板")
async def list_presets(user_id: CurrentUserId) -> Result[list[dict]]:
    """11 条硬编码预设（OpenAI / DeepSeek / 智谱 / Kimi / MiniMax / OpenRouter ...）。

    不查库，纯静态 —— 前端"从预设创建"向导用这个。
    """
    return Result.ok(ModelProviderService.presets())


@router.get("/default-model", response_model=Result[str], summary="某类型提供商的默认（最便宜）模型")
async def get_default_model(
    user_id: CurrentUserId,
    type: str = Query(..., description="提供商类型，如 OPENAI / DEEPSEEK"),
) -> Result[str]:
    return Result.ok(ModelProviderService.default_model(type))


@router.get(
    "/with-models",
    response_model=Result[list[ProviderWithModelsOut]],
    summary="所有启用的提供商及其模型（按类型过滤）",
)
async def list_with_models(
    db: DbSession,
    user_id: CurrentUserId,
    type: str | None = Query(None, description="模型类型：llm / embedding / rerank / tts / stt"),
) -> Result[list[ProviderWithModelsOut]]:
    """`ModelSelect.vue` 的数据源，被 Agent 编辑、工作流节点配置、Playground 复用。"""
    return Result.ok(await _svc(db).list_with_models(type))


@router.post("/check", response_model=Result[str], summary="连通性检查（表单实时数据）")
async def check_by_form(
    dto: ModelProviderCheckIn,
    user_id: CurrentUserId,
) -> Result[str]:
    """**不落库** —— 用户还没点保存时就要能测，所以不注入 db session。"""
    return Result.ok(await ModelProviderService.check_connectivity_by_form(dto))


@router.post("/refresh-cache", response_model=Result[None], summary="刷新供应商缓存（no-op）")
async def refresh_cache(user_id: CurrentUserId) -> Result[None]:
    """Python 侧没有 Model 实例缓存，这里什么都不做，但**接口必须存在**。"""
    ModelProviderService.refresh_cache()
    return Result.ok(None)


# ----------------------------------------------------------------------
# CRUD
# ----------------------------------------------------------------------


@router.get("", response_model=Result[PageResult[ModelProviderOut]], summary="分页查询提供商")
async def page(
    db: DbSession,
    user_id: CurrentUserId,
    page_num: int = Query(1, alias="pageNum"),
    page_size: int = Query(10, alias="pageSize"),
) -> Result[PageResult[ModelProviderOut]]:
    return Result.ok(await _svc(db).page(page_num, page_size))


@router.post("", response_model=Result[ModelProviderOut], summary="新增提供商")
async def create(
    dto: ModelProviderIn,
    db: DbSession,
    user_id: CurrentUserId,
) -> Result[ModelProviderOut]:
    return Result.ok(await _svc(db).create(dto))


@router.put("", response_model=Result[ModelProviderOut], summary="更新提供商")
async def update(
    dto: ModelProviderIn,
    db: DbSession,
    user_id: CurrentUserId,
) -> Result[ModelProviderOut]:
    """注意 id 在 body 里，不在路径上（对齐 Java `@PutMapping` 无路径变量）。"""
    return Result.ok(await _svc(db).update(dto))


# ----------------------------------------------------------------------
# 动态路径（必须最后注册）
# ----------------------------------------------------------------------


@router.get("/{provider_id}", response_model=Result[ModelProviderOut], summary="获取单个提供商")
async def get_one(
    provider_id: int,
    db: DbSession,
    user_id: CurrentUserId,
) -> Result[ModelProviderOut]:
    return Result.ok(await _svc(db).get(provider_id))


@router.delete("/{provider_id}", response_model=Result[None], summary="删除提供商（软删 + 级联）")
async def delete(
    provider_id: int,
    db: DbSession,
    user_id: CurrentUserId,
) -> Result[None]:
    await _svc(db).delete(provider_id)
    return Result.ok(None)


@router.patch("/{provider_id}/status", response_model=Result[None], summary="启用 / 禁用")
async def toggle_status(
    provider_id: int,
    db: DbSession,
    user_id: CurrentUserId,
    status: str = Query(..., description="active / disabled"),
) -> Result[None]:
    await _svc(db).toggle_status(provider_id, status)
    return Result.ok(None)


@router.get(
    "/{provider_id}/config-fields",
    response_model=Result[list[dict]],
    summary="该类型提供商的调参字段定义（前端动态渲染表单）",
)
async def config_fields(
    provider_id: int,
    db: DbSession,
    user_id: CurrentUserId,
) -> Result[list[dict]]:
    return Result.ok(await _svc(db).config_fields(provider_id))


@router.get(
    "/{provider_id}/model-capabilities",
    response_model=Result[list[dict]],
    summary="该类型提供商的能力开关定义（多模态 / 联网 / 深度思考）",
)
async def model_capabilities(
    provider_id: int,
    db: DbSession,
    user_id: CurrentUserId,
) -> Result[list[dict]]:
    return Result.ok(await _svc(db).model_capabilities(provider_id))


@router.get(
    "/{provider_id}/check",
    response_model=Result[str],
    summary="连通性检查（已保存的提供商）",
)
async def check(
    provider_id: int,
    db: DbSession,
    user_id: CurrentUserId,
) -> Result[str]:
    return Result.ok(await _svc(db).check_connectivity(provider_id))


@router.get(
    "/{provider_id}/fetch-models",
    response_model=Result[list[FetchedModelOut]],
    summary="联网拉取该提供商下的可用模型",
)
async def fetch_models(
    provider_id: int,
    db: DbSession,
    user_id: CurrentUserId,
) -> Result[list[FetchedModelOut]]:
    return Result.ok(await _svc(db).fetch_models(provider_id))
