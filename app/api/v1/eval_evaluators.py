"""评估器接口（12 个，对齐前端 `src/api/evaluator.js`）。

**路由顺序**：`/templates`、`/templates/{key}`、`/versions`、`/test`、`/examples`
全部必须排在 `/{evaluator_id}` 之前。反了就是 422（第 7 次，已经是肌肉记忆了）。

`/test` 是**真调模型**的接口（花几分钱），它会：
填变量 → 追加"只返回 JSON"指令 → 调评估器模型 → 解析 score。
版本不存在时返回 `data: null` 而不是 404 —— 前端在版本下拉没选好时也会触发。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import CurrentUserId, DbSession
from app.schemas.common import PageResult, Result
from app.schemas.eval import (
    EvalEvaluatorIn,
    EvalEvaluatorOut,
    EvalEvaluatorTemplateOut,
    EvalEvaluatorTestIn,
    EvalEvaluatorVersionIn,
    EvalEvaluatorVersionOut,
    EvalScoreOut,
)
from app.services.eval_evaluator_service import EvalEvaluatorService
from app.services.eval_examples import (
    create_evaluator_from_example,
    list_evaluator_examples,
)

router = APIRouter(prefix="/api/eval/evaluators", tags=["eval-evaluators"])


def _svc(db: DbSession) -> EvalEvaluatorService:
    return EvalEvaluatorService(db)


# ======================================================================
# ① 静态路径（必须最前）
# ======================================================================


@router.get("/templates", response_model=Result[list[EvalEvaluatorTemplateOut]], summary="构建模板列表")
async def list_templates(
    db: DbSession, user_id: CurrentUserId
) -> Result[list[EvalEvaluatorTemplateOut]]:
    return Result.ok(await _svc(db).list_templates())


@router.get(
    "/templates/{key}", response_model=Result[EvalEvaluatorTemplateOut], summary="按 key 取模板"
)
async def get_template(
    key: str, db: DbSession, user_id: CurrentUserId
) -> Result[EvalEvaluatorTemplateOut]:
    return Result.ok(await _svc(db).get_template(key))


@router.get("/examples", response_model=Result[list[dict]], summary="内置示例评估器列表")
async def list_examples(user_id: CurrentUserId) -> Result[list[dict]]:
    """4 个内置模板：文本相似度 / 回答完整性 / 代码质量 / 情感分析。"""
    return Result.ok(list_evaluator_examples())


@router.post("/examples/{key}", response_model=Result[dict], summary="从示例创建评估器")
async def create_from_example(
    key: str, db: DbSession, user_id: CurrentUserId
) -> Result[dict]:
    """建评估器 + 自动建 v1 版本（含提示词）。

    注意：内置模板的 `modelConfig` **没有 providerId** ——
    用户创建后需要自己去选模型，否则跑测试会报"模型配置缺少 providerId"。
    这是有意的（不能绑死某家模型）。
    """
    return Result.ok(await create_evaluator_from_example(db, key, user_id))


@router.post("/versions", response_model=Result[EvalEvaluatorVersionOut], summary="创建评估器版本")
async def create_version(
    dto: EvalEvaluatorVersionIn, db: DbSession, user_id: CurrentUserId
) -> Result[EvalEvaluatorVersionOut]:
    return Result.ok(await _svc(db).create_version(dto))


@router.post("/test", response_model=Result[EvalScoreOut | None], summary="测试评估器（真调模型）")
async def test_evaluator(
    dto: EvalEvaluatorTestIn, db: DbSession, user_id: CurrentUserId
) -> Result[EvalScoreOut | None]:
    """跑一次打分看效果。**版本不存在返回 null**（不是 404）。

    前端"测试"按钮在版本还没选好时也会触发，404 会弹红框；
    `data: null` 它只是不显示结果。

    ⚠️ 这个接口会**真花钱**（一次 LLM 调用）。验证脚本默认不跑它。
    """
    result = await _svc(db).test(dto)
    return Result.ok(None if result is None else EvalScoreOut(
        score=result.score, reason=result.reason
    ))


# ======================================================================
# ② 主表
# ======================================================================


@router.get("", response_model=Result[PageResult[EvalEvaluatorOut]], summary="评估器列表")
async def list_evaluators(
    db: DbSession,
    user_id: CurrentUserId,
    page_num: Annotated[int, Query(alias="pageNum")] = 1,
    page_size: Annotated[int, Query(alias="pageSize")] = 20,
    keyword: Annotated[str | None, Query()] = None,
) -> Result[PageResult[EvalEvaluatorOut]]:
    records, total = await _svc(db).list(user_id, page_num, page_size, keyword)
    return Result.ok(
        PageResult.of(records=records, total=total, current=page_num, size=page_size)
    )


@router.post("", response_model=Result[EvalEvaluatorOut], summary="创建评估器")
async def create_evaluator(
    dto: EvalEvaluatorIn, db: DbSession, user_id: CurrentUserId
) -> Result[EvalEvaluatorOut]:
    return Result.ok(await _svc(db).create(dto, user_id))


# ======================================================================
# ③ 动态 detail（最后）
# ======================================================================


@router.get("/{evaluator_id}", response_model=Result[EvalEvaluatorOut], summary="评估器详情")
async def get_evaluator(
    evaluator_id: int, db: DbSession, user_id: CurrentUserId
) -> Result[EvalEvaluatorOut]:
    return Result.ok(await _svc(db).get(evaluator_id))


@router.put("/{evaluator_id}", response_model=Result[None], summary="更新评估器")
async def update_evaluator(
    evaluator_id: int,
    dto: EvalEvaluatorIn,
    db: DbSession,
    user_id: CurrentUserId,
) -> Result[None]:
    await _svc(db).update(evaluator_id, dto)
    return Result.ok(None)


@router.delete("/{evaluator_id}", response_model=Result[None], summary="删除评估器")
async def delete_evaluator(
    evaluator_id: int, db: DbSession, user_id: CurrentUserId
) -> Result[None]:
    """软删。**不级联删版本** —— 历史实验结果还要能查到当时的提示词版本。"""
    await _svc(db).delete(evaluator_id)
    return Result.ok(None)


@router.get(
    "/{evaluator_id}/versions",
    response_model=Result[list[EvalEvaluatorVersionOut]],
    summary="评估器版本列表",
)
async def list_versions(
    evaluator_id: int, db: DbSession, user_id: CurrentUserId
) -> Result[list[EvalEvaluatorVersionOut]]:
    return Result.ok(await _svc(db).list_versions(evaluator_id))


__all__ = ["router"]
