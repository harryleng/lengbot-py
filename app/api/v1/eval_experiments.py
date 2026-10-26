"""评测实验接口（9 个，对齐前端 `src/api/experiment.js`）。

**这 9 个接口全部带 `/{experiment_id}`**，没有静态首段冲突 —— 但
`/{id}/stop`、`/{id}/restart`、`/{id}/results`、`/{id}/detail` 都是
`/{id}/xxx` 两段，和 `/{id}` 一段不冲突。

`POST /` 是**异步**的：建完立刻返回 `status=running`，
真正的执行在后台（`asyncio.create_task`）。前端靠轮询列表拿 `progress`。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import CurrentUserId, DbSession
from app.schemas.common import PageResult, Result
from app.schemas.eval import (
    EvalExperimentIn,
    EvalExperimentOut,
    EvalExperimentOverviewOut,
    EvalExperimentResultOut,
)
from app.services.eval_experiment_service import EvalExperimentService

router = APIRouter(prefix="/api/eval/experiments", tags=["eval-experiments"])


def _svc(db: DbSession) -> EvalExperimentService:
    return EvalExperimentService(db)


@router.get("", response_model=Result[PageResult[EvalExperimentOut]], summary="实验列表")
async def list_experiments(
    db: DbSession,
    user_id: CurrentUserId,
    page_num: Annotated[int, Query(alias="pageNum")] = 1,
    page_size: Annotated[int, Query(alias="pageSize")] = 20,
    keyword: Annotated[str | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
) -> Result[PageResult[EvalExperimentOut]]:
    records, total = await _svc(db).list(
        user_id, page_num, page_size, keyword, status
    )
    return Result.ok(
        PageResult.of(records=records, total=total, current=page_num, size=page_size)
    )


@router.post("", response_model=Result[EvalExperimentOut], summary="创建实验并开始运行")
async def create_experiment(
    dto: EvalExperimentIn, db: DbSession, user_id: CurrentUserId
) -> Result[EvalExperimentOut]:
    """建完**立刻返回**（`status=running`, `progress=0`），执行在后台。

    前端看到 running 就显示进度条并开始轮询；跑完变 completed/failed/stopped。

    校验（都是 400，不静默）：
    - 必须指定 `datasetVersionId`
    - `evaluatorConfig` 至少 1 个、最多 5 个、每项要有 `evaluatorVersionId`
    """
    return Result.ok(await _svc(db).create(dto, user_id))


@router.get("/{experiment_id}", response_model=Result[EvalExperimentOut], summary="实验详情")
async def get_experiment(
    experiment_id: int, db: DbSession, user_id: CurrentUserId
) -> Result[EvalExperimentOut]:
    return Result.ok(await _svc(db).get(experiment_id))


@router.put("/{experiment_id}", response_model=Result[EvalExperimentOut], summary="更新实验")
async def update_experiment(
    experiment_id: int,
    dto: EvalExperimentIn,
    db: DbSession,
    user_id: CurrentUserId,
) -> Result[EvalExperimentOut]:
    """运行中的实验不能改（会 400）。要改先停止。"""
    return Result.ok(await _svc(db).update(experiment_id, dto))


@router.put("/{experiment_id}/stop", response_model=Result[None], summary="停止实验")
async def stop_experiment(
    experiment_id: int, db: DbSession, user_id: CurrentUserId
) -> Result[None]:
    """只给 task 打 `cancel_requested=1`，**不直接改 status**。

    执行器每完成一批会检查这个标记，自己退出并置 `stopped`。
    这里改了 status 的话，执行器下一轮写 progress 时会把它改回 running。
    """
    await _svc(db).stop(experiment_id)
    return Result.ok(None)


@router.put("/{experiment_id}/restart", response_model=Result[EvalExperimentOut], summary="重跑实验")
async def restart_experiment(
    experiment_id: int, db: DbSession, user_id: CurrentUserId
) -> Result[EvalExperimentOut]:
    """清空旧结果 + 重置进度 + 重新起后台任务。"""
    return Result.ok(await _svc(db).restart(experiment_id))


@router.delete("/{experiment_id}", response_model=Result[None], summary="删除实验")
async def delete_experiment(
    experiment_id: int, db: DbSession, user_id: CurrentUserId
) -> Result[None]:
    """运行中的不能删。删实验会**级联物理删所有结果行**（Java 同）。"""
    await _svc(db).delete(experiment_id)
    return Result.ok(None)


@router.get(
    "/{experiment_id}/results",
    response_model=Result[list[EvalExperimentOverviewOut]],
    summary="实验汇总（每个评估器一行：平均分 + 条数）",
)
async def get_results(
    experiment_id: int, db: DbSession, user_id: CurrentUserId
) -> Result[list[EvalExperimentOverviewOut]]:
    """`avgScore` 是 0~1 的小数（前端自己乘 100 显示百分比）。"""
    return Result.ok(await _svc(db).overview(experiment_id))


@router.get(
    "/{experiment_id}/detail",
    response_model=Result[PageResult[EvalExperimentResultOut]],
    summary="实验明细（逐条样本 × 评估器）",
)
async def get_detail_results(
    experiment_id: int,
    db: DbSession,
    user_id: CurrentUserId,
    evaluator_version_id: Annotated[int | None, Query(alias="evaluatorVersionId")] = None,
    page_num: Annotated[int, Query(alias="pageNum")] = 1,
    page_size: Annotated[int, Query(alias="pageSize")] = 20,
) -> Result[PageResult[EvalExperimentResultOut]]:
    """按评估器版本过滤（前端切 Tab 用）。不传就是全部混在一起。"""
    records, total = await _svc(db).list_results(
        experiment_id, evaluator_version_id, page_num, page_size
    )
    return Result.ok(
        PageResult.of(records=records, total=total, current=page_num, size=page_size)
    )


__all__ = ["router"]
