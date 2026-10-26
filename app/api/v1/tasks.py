"""任务中心 API（对照 Java `TaskController` + `TaskEventController`）。

路由顺序：`/count` `/running-count` `/type-counts` 必须先于 `/{task_id}` 注册，
否则 `GET /tasks/count` 会被当成 `task_id="count"` 转 int 炸成 404/422。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.api.deps import CurrentUserId, DbSession
from app.schemas.common import Result
from app.services.task_service import TaskService, task_out

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


def _not_found(exc: LookupError) -> HTTPException:
    return HTTPException(status_code=404, detail=str(exc))


@router.get("/count", response_model=Result[dict], summary="任务计数（HTTP 兜底）")
async def count(db: DbSession, user_id: CurrentUserId) -> Result[dict]:
    return Result.ok(await TaskService(db, user_id).counts())


@router.get("/running-count", response_model=Result[int], summary="运行中+待处理任务数")
async def running_count(db: DbSession, user_id: CurrentUserId) -> Result[int]:
    return Result.ok(await TaskService(db, user_id).running_count())


@router.get("/type-counts", response_model=Result[dict], summary="按类型统计进行中任务数")
async def type_counts(db: DbSession, user_id: CurrentUserId) -> Result[dict]:
    return Result.ok(await TaskService(db, user_id).type_counts())


@router.get("", response_model=Result[dict], summary="分页查询当前用户的任务列表")
async def list_tasks(
    db: DbSession,
    user_id: CurrentUserId,
    page_num: int = Query(default=1, alias="pageNum", ge=1),
    page_size: int = Query(default=10, alias="pageSize", ge=1, le=200),
    name: str | None = Query(default=None),
    status: str | None = Query(default=None),
    type: str | None = Query(default=None),
) -> Result[dict]:
    rows, total = await TaskService(db, user_id).list_page(page_num, page_size, name, status, type)
    return Result.ok(
        {
            "records": [task_out(r) for r in rows],
            "total": total,
            "pageNum": page_num,
            "pageSize": page_size,
        }
    )


@router.get("/{task_id}", response_model=Result[dict], summary="获取任务详情")
async def get_task(db: DbSession, user_id: CurrentUserId, task_id: int) -> Result[dict]:
    svc = TaskService(db, user_id)
    try:
        row = await svc.get_owned(task_id)
    except LookupError as exc:
        raise _not_found(exc) from exc
    return Result.ok(task_out(row))


@router.get("/{task_id}/progress", response_model=Result[dict], summary="任务实时进度快照")
async def get_progress(db: DbSession, user_id: CurrentUserId, task_id: int) -> Result[dict]:
    try:
        return Result.ok(await TaskService(db, user_id).progress(task_id))
    except LookupError as exc:
        raise _not_found(exc) from exc


@router.post("/{task_id}/cancel", response_model=Result[None], summary="取消任务")
async def cancel_task(db: DbSession, user_id: CurrentUserId, task_id: int) -> Result[None]:
    try:
        success = await TaskService(db, user_id).cancel(task_id)
    except LookupError as exc:
        raise _not_found(exc) from exc
    if not success:
        # Java：Result.fail(61002, "任务无法取消（可能已完成或已取消）")
        return Result.fail("任务无法取消（可能已完成或已取消）", code=61002)
    return Result.ok(None)


@router.delete("/{task_id}", response_model=Result[None], summary="删除任务")
async def delete_task(db: DbSession, user_id: CurrentUserId, task_id: int) -> Result[None]:
    try:
        await TaskService(db, user_id).delete(task_id)
    except LookupError as exc:
        raise _not_found(exc) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return Result.ok(None)
