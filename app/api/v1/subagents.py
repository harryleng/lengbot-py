"""SubAgent 管理接口（对齐前端 `src/api/subagent.js`）。

**路由顺序**：`/enabled` 是静态路径，必须注册在 `/{sub_id}` 之前。
反过来 `GET /api/subagents/enabled` 会撞上 `/{sub_id}` → `int("enabled")`
失败 → 422，前端"绑定子智能体"下拉永远是空的。

`runs/*` 那一批（运行记录 / 批次 / 取消）**不在本文件**：
Python 版没有子代理执行器（`delegate` 工具没实现），那些接口读的是
Java 侧留下的历史数据，单独做一块更清楚。缺了它们的表现是前端
会话侧栏的"子代理运行状态"区块不显示 —— 不影响绑定和增删改。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import CurrentUserId, DbSession
from app.schemas.common import PageResult, Result
from app.schemas.subagent import SubAgentIn, SubAgentOut
from app.services.subagent_run_service import SubAgentRunService
from app.services.subagent_service import SubAgentService

router = APIRouter(prefix="/api/subagents", tags=["subagents"])


def _svc(db: DbSession) -> SubAgentService:
    return SubAgentService(db)


# ======================================================================
# 静态路径
# ======================================================================


@router.get("/enabled", response_model=Result[list[SubAgentOut]], summary="所有启用的 SubAgent")
async def list_enabled(db: DbSession, user_id: CurrentUserId) -> Result[list[SubAgentOut]]:
    """Agent 绑定下拉的数据源。只返回 `enabled=1` 且未删除的。"""
    return Result.ok(await _svc(db).list_enabled())


# ======================================================================
# 运行记录（只读；字段是 snake_case，见 subagent_run_service 的说明）
# ======================================================================
# ⚠️ 顺序：`/runs/summary` 和 `/runs/projection` 必须在 `/runs/{task_id}` 之前。
# 否则 GET /api/subagents/runs/summary 会被 `/runs/{task_id}` 当成
# taskId="summary" 吃掉，查不到就 404，侧栏面板空白。


@router.get("/runs", response_model=Result[PageResult[dict]], summary="会话内的子代理任务")
async def list_runs(
    db: DbSession,
    user_id: CurrentUserId,
    session_id: Annotated[int, Query(alias="sessionId")],
    batch_id: Annotated[str | None, Query(alias="batchId")] = None,
    parent_request_id: Annotated[str | None, Query(alias="parentRequestId")] = None,
    page_num: Annotated[int, Query(alias="pageNum")] = 1,
    page_size: Annotated[int, Query(alias="pageSize")] = 20,
) -> Result[PageResult[dict]]:
    """⚠️ 这一批字段是**驼峰**（返回的是实体分页），
    而下面 `/runs/{taskId}` 那批是 **snake_case**（Java 手拼 Map）。
    两套命名并存是 Java 侧的现状，照抄即可，别统一。
    """
    records, total = await SubAgentRunService(db).page_runs(
        session_id, user_id, batch_id, parent_request_id, page_num, page_size
    )
    return Result.ok(
        PageResult.of(records=records, total=total, current=page_num, size=page_size)
    )


@router.get("/runs/summary", response_model=Result[list[dict]], summary="侧栏运行态摘要")
async def list_run_summaries(
    db: DbSession,
    user_id: CurrentUserId,
    session_id: Annotated[int, Query(alias="sessionId")],
    parent_request_id: Annotated[str | None, Query(alias="parentRequestId")] = None,
    limit: Annotated[int, Query()] = 20,
) -> Result[list[dict]]:
    return Result.ok(
        await SubAgentRunService(db).list_summaries(
            session_id, user_id, parent_request_id, limit
        )
    )


@router.get("/runs/projection", response_model=Result[dict], summary="调研协作状态投影")
async def get_projection(
    db: DbSession,
    user_id: CurrentUserId,
    session_id: Annotated[int, Query(alias="sessionId")],
    parent_request_id: Annotated[str, Query(alias="parentRequestId")],
) -> Result[dict]:
    """**部分字段恒为空**：`todos` / `attachments` / `artifacts` / `usage`。

    这四块由 Java 侧的研究协作模块维护（待办清单、附件、产物、用量统计），
    Python 版没有对应实现。但 `subagents` 是**真实数据** ——
    面板里"子智能体跑到哪一步了"这一栏是有用的，其余显示"暂无"。
    """
    svc = SubAgentRunService(db)
    subagents = await svc.list_summaries(session_id, user_id, parent_request_id, 50)
    return Result.ok(
        {
            "parentRequestId": parent_request_id,
            "status": "completed" if subagents and all(
                s.get("status") in ("completed", "failed", "cancelled") for s in subagents
            ) else ("running" if subagents else "pending"),
            "version": 1,
            "updateTime": None,
            "todos": [],
            "attachments": [],
            "artifacts": [],
            "subagents": subagents,
            "usage": {},
        }
    )


@router.get("/batches/{batch_id}", response_model=Result[dict], summary="批次详情")
async def get_batch(
    batch_id: str,
    db: DbSession,
    user_id: CurrentUserId,
    session_id: Annotated[int, Query(alias="sessionId")],
) -> Result[dict]:
    return Result.ok(await SubAgentRunService(db).get_batch(batch_id, session_id, user_id))


@router.post("/batches/{batch_id}/cancel", response_model=Result[dict], summary="取消批次")
async def cancel_batch(
    batch_id: str,
    db: DbSession,
    user_id: CurrentUserId,
    session_id: Annotated[int, Query(alias="sessionId")],
) -> Result[dict]:
    """只写 `cancel_requested=1` 标记并返回 `affected`。

    **Python 版没有子代理执行器来消费这个标记**，所以任务不会真的停下 ——
    这也是为什么把 `affected` 原样返回：前端能看出"标记了几个"，
    而不是看到一个假装的"已取消"。
    """
    return Result.ok(
        await SubAgentRunService(db).cancel_batch(batch_id, session_id, user_id)
    )


@router.post("/runs/{task_id}/cancel", response_model=Result[dict], summary="取消单个任务")
async def cancel_task(
    task_id: str,
    db: DbSession,
    user_id: CurrentUserId,
    session_id: Annotated[int, Query(alias="sessionId")],
) -> Result[dict]:
    return Result.ok(
        await SubAgentRunService(db).cancel_task(task_id, session_id, user_id)
    )


@router.get("/runs/{task_id}", response_model=Result[dict], summary="任务详情")
async def get_run(
    task_id: str,
    db: DbSession,
    user_id: CurrentUserId,
    session_id: Annotated[int, Query(alias="sessionId")],
) -> Result[dict]:
    return Result.ok(
        await SubAgentRunService(db).get_task(task_id, session_id, user_id)
    )


@router.get("/runs/{task_id}/thread", response_model=Result[dict], summary="任务子线程")
async def get_run_thread(
    task_id: str,
    db: DbSession,
    user_id: CurrentUserId,
    session_id: Annotated[int, Query(alias="sessionId")],
) -> Result[dict]:
    return Result.ok(
        await SubAgentRunService(db).get_task_thread(task_id, session_id, user_id)
    )


@router.get("/runs/{task_id}/events", response_model=Result[dict], summary="任务运行事件（游标）")
async def get_run_events(
    task_id: str,
    db: DbSession,
    user_id: CurrentUserId,
    session_id: Annotated[int, Query(alias="sessionId")],
    cursor: Annotated[int | None, Query()] = None,
    limit: Annotated[int, Query()] = 50,
) -> Result[dict]:
    return Result.ok(
        await SubAgentRunService(db).get_task_events(
            task_id, session_id, user_id, cursor, limit
        )
    )


# ======================================================================
# 主流程
# ======================================================================


@router.get("", response_model=Result[PageResult[SubAgentOut]], summary="SubAgent 列表")
async def list_subagents(
    db: DbSession,
    user_id: CurrentUserId,
    page_num: Annotated[int, Query(alias="pageNum")] = 1,
    page_size: Annotated[int, Query(alias="pageSize")] = 100,
    keyword: Annotated[str | None, Query()] = None,
    is_builtin: Annotated[bool | None, Query(alias="isBuiltin")] = None,
) -> Result[PageResult[SubAgentOut]]:
    records, total = await _svc(db).list(page_num, page_size, keyword, is_builtin)
    return Result.ok(
        PageResult.of(records=records, total=total, current=page_num, size=page_size)
    )


@router.post("", response_model=Result[SubAgentOut], summary="新建 SubAgent")
async def create_subagent(
    dto: SubAgentIn, db: DbSession, user_id: CurrentUserId
) -> Result[SubAgentOut]:
    return Result.ok(await _svc(db).create(dto, user_id))


@router.put("", response_model=Result[SubAgentOut], summary="更新 SubAgent")
async def update_subagent(
    dto: SubAgentIn, db: DbSession, user_id: CurrentUserId
) -> Result[SubAgentOut]:
    """内置的只能改模型配置；非内置全字段可改（见 service）。"""
    return Result.ok(await _svc(db).update(dto))


@router.put("/{sub_id}/enabled", response_model=Result[None], summary="启用 / 停用")
async def set_enabled(
    sub_id: int, db: DbSession, user_id: CurrentUserId, enabled: bool = Query(...)
) -> Result[None]:
    await _svc(db).set_enabled(sub_id, enabled)
    return Result.ok(None)


# ======================================================================
# 动态 detail（放最后）
# ======================================================================


@router.get("/{sub_id}", response_model=Result[SubAgentOut], summary="SubAgent 详情")
async def get_subagent(
    sub_id: int, db: DbSession, user_id: CurrentUserId
) -> Result[SubAgentOut]:
    return Result.ok(await _svc(db).get(sub_id))


@router.delete("/{sub_id}", response_model=Result[None], summary="删除 SubAgent（内置不可删）")
async def delete_subagent(
    sub_id: int, db: DbSession, user_id: CurrentUserId
) -> Result[None]:
    await _svc(db).delete(sub_id)
    return Result.ok(None)
