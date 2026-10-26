"""Agent 工作流编排接口（对齐前端 `src/api/workflow.js` + `workflowTestStream.js`）。

**16 个接口**，分五组：

| 组 | 接口 | 说明 |
|---|---|---|
| 编辑 | GET `` / POST ``/draft`` / ``/publish`` / ``/validate`` / GET ``/io-schema`` | 草稿、发布、校验 |
| 版本 | GET ``/versions`` / GET ``/versions/{v}`` / POST ``/versions/{v}/restore`` | 历史版本 |
| 调试 | POST ``/test``、POST ``/test/stream``、POST ``/test-node`` | 整条 / 流式 / 单节点 |
| 记录 | GET/DELETE ``/test-runs``、``/test-runs/{runId}`` | 最近 50 条 |
| 确认 | POST ``/resume``、``/resume/stream``、POST ``/abandon`` | HITL |

**SSE 那两个是实际主路径**：前端 `testWorkflowStream()` 打的是
`/workflow/test/stream`，REST 的 `/workflow/test` 只用于脚本和排障。
只实现 REST 的话，编排页点"运行"看不到节点逐个亮起来 —— 节点状态是靠
流式事件驱动的。

帧格式和聊天一致（`[STATUS]` + JSON / `[DONE]` + JSON），前端
`processSseLines` 用同一套解析。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import text

from app.api.deps import CurrentUserId, DbSession
from app.core.db import SessionLocal
from app.schemas.common import Result
from app.services.workflow_editor_service import WorkflowEditorService, validate_graph
from app.services.workflow_test_service import WorkflowTestService
from app.workflow.events import to_sse_event
from app.workflow.executor import WorkflowExecutor
from app.workflow.graph import WorkflowGraph
from app.workflow.nodes import WorkflowContext
from app.workflow.runs import suspended_runs

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agents/{agent_id}/workflow", tags=["workflow"])

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    # 关掉 nginx 缓冲，否则事件会被攒着一起发，看不到"逐个节点亮起来"
    "X-Accel-Buffering": "no",
}


# ======================================================================
# 编辑：状态 / 草稿 / 发布 / 校验 / IO
# ======================================================================


@router.get("", response_model=Result[dict], summary="工作流编辑器状态（草稿 + 已发布）")
async def get_workflow(agent_id: int, db: DbSession, user_id: CurrentUserId) -> Result[dict]:
    """一次返回编辑画布需要的全部信息。

    前端靠 `status` 决定顶栏显示"草稿"/"已发布"/"有未发布的修改"，
    靠 `publishedVersion` 显示版本号，靠 `draft` 渲染画布。
    """
    return Result.ok(await WorkflowEditorService(db).get_editor_state(agent_id))


@router.post("/draft", response_model=Result[None], summary="保存草稿（跳过校验）")
async def save_draft(
    agent_id: int,
    db: DbSession,
    user_id: CurrentUserId,
    graph: Annotated[dict[str, Any] | None, Body()] = None,
) -> Result[None]:
    """body 就是画布本身：`{nodes, edges, globalConfig?, publishDescription?}`。

    **绝对不能校验**：用户拖动节点、改一半提示词的时候，前端每 2.5 秒自动
    保存一次。这时图必然"不合格"（比如 LLM 节点还没选模型），一旦校验就
    永远存不上，用户的编辑全丢。
    """
    await WorkflowEditorService(db).save_draft(agent_id, graph or {})
    return Result.ok(None)


@router.post("/publish", response_model=Result[dict], summary="发布工作流（先校验）")
async def publish_workflow(
    agent_id: int,
    db: DbSession,
    user_id: CurrentUserId,
    graph: Annotated[dict[str, Any] | None, Body()] = None,
) -> Result[dict]:
    return Result.ok(await WorkflowEditorService(db).publish(agent_id, graph or {}))


@router.post("/validate", response_model=Result[list[str]], summary="校验工作流（返回错误列表）")
async def validate_workflow(
    agent_id: int,
    db: DbSession,
    user_id: CurrentUserId,
    graph: Annotated[dict[str, Any] | None, Body()] = None,
) -> Result[list[str]]:
    """**返回 200 + 空数组**表示通过，不是 204。

    前端是 `const errors = res.data` 然后读 `errors.length`，
    返回 null 会直接 TypeError。
    """
    return Result.ok(validate_graph(graph or {}, agent_id=agent_id))


@router.get("/io-schema", response_model=Result[dict], summary="已发布工作流的 IO 签名")
async def get_io_schema(agent_id: int, db: DbSession, user_id: CurrentUserId) -> Result[dict]:
    return Result.ok(await WorkflowEditorService(db).get_io_schema(agent_id))


# ======================================================================
# 版本
# ======================================================================


@router.get("/versions", response_model=Result[list[dict]], summary="已发布版本列表")
async def list_versions(
    agent_id: int, db: DbSession, user_id: CurrentUserId
) -> Result[list[dict]]:
    return Result.ok(await WorkflowEditorService(db).list_versions(agent_id))


@router.get("/versions/{version}", response_model=Result[dict], summary="某个版本的画布")
async def get_version(
    agent_id: int, version: int, db: DbSession, user_id: CurrentUserId
) -> Result[dict]:
    return Result.ok(await WorkflowEditorService(db).get_version_graph(agent_id, version))


@router.post(
    "/versions/{version}/restore", response_model=Result[None], summary="恢复到某个版本（写入草稿）"
)
async def restore_version(
    agent_id: int, version: int, db: DbSession, user_id: CurrentUserId
) -> Result[None]:
    """只改草稿，不会自动发布 —— 用户还要再点一次"发布"才生效。"""
    await WorkflowEditorService(db).restore_version(agent_id, version)
    return Result.ok(None)


# ======================================================================
# 调试运行
# ======================================================================


@router.post("/test", response_model=Result[dict], summary="调试运行（一次性返回）")
async def test_workflow(
    agent_id: int,
    db: DbSession,
    user_id: CurrentUserId,
    req: Annotated[dict[str, Any] | None, Body()] = None,
) -> Result[dict]:
    return Result.ok(await WorkflowTestService(db).run_sync(agent_id, user_id, req or {}))


@router.post("/test/stream", summary="调试运行（SSE 实时推送）")
async def test_workflow_stream(
    agent_id: int,
    user_id: CurrentUserId,
    req: Annotated[dict[str, Any] | None, Body()] = None,
) -> StreamingResponse:
    """流式版。**自带 session**，不用请求作用域的 db。

    原因：生成器在响应发送期间还在跑，而请求作用域的 session 会在响应开始
    之后被清理链回收，中途写库（running / completed）就炸 `Session is closed`。
    """
    agent_id_int = int(agent_id)
    user_id_int = int(user_id)

    async def gen():
        async with SessionLocal() as session:
            async for frame in WorkflowTestService(session).stream(
                agent_id_int, user_id_int, req or {}
            ):
                yield frame

    return StreamingResponse(gen(), media_type="text/event-stream", headers=SSE_HEADERS)


@router.post("/test-node", response_model=Result[dict], summary="单节点调试")
async def test_node(
    agent_id: int,
    db: DbSession,
    user_id: CurrentUserId,
    req: Annotated[dict[str, Any] | None, Body()] = None,
) -> Result[dict]:
    return Result.ok(await WorkflowTestService(db).run_node(agent_id, user_id, req or {}))


# ======================================================================
# 调试记录
# ======================================================================


@router.get("/test-runs", response_model=Result[list[dict]], summary="调试记录列表（最近 50 条）")
async def list_test_runs(
    agent_id: int, db: DbSession, user_id: CurrentUserId
) -> Result[list[dict]]:
    return Result.ok(await WorkflowTestService(db).runs.list_runs(agent_id))


@router.get("/test-runs/{run_id}", response_model=Result[dict], summary="调试记录详情")
async def get_test_run(
    agent_id: int, run_id: str, db: DbSession, user_id: CurrentUserId
) -> Result[dict]:
    return Result.ok(await WorkflowTestService(db).runs.get_detail(agent_id, run_id))


@router.delete("/test-runs/{run_id}", response_model=Result[None], summary="删除一条调试记录")
async def delete_test_run(
    agent_id: int, run_id: str, db: DbSession, user_id: CurrentUserId
) -> Result[None]:
    await WorkflowTestService(db).runs.delete_run(agent_id, run_id)
    return Result.ok(None)


@router.delete("/test-runs", response_model=Result[None], summary="清空调试记录")
async def clear_test_runs(agent_id: int, db: DbSession, user_id: CurrentUserId) -> Result[None]:
    await WorkflowTestService(db).runs.clear(agent_id)
    return Result.ok(None)


# ======================================================================
# 人工确认（HITL）
# ======================================================================


class ResumeIn(BaseModel):
    run_id: str
    form_data: dict[str, Any] | None = None
    message_id: str | None = None


class AbandonIn(BaseModel):
    run_id: str


@router.post("/resume", response_model=Result[dict], summary="确认后恢复工作流")
async def resume_workflow(
    agent_id: int,
    req: ResumeIn,
    user_id: CurrentUserId,
) -> Result[dict]:
    """从挂起点继续执行。

    **一次性的**：快照取出即删除。重复提交同一个 runId 返回 404 ——
    有意为之，避免双击"确认"把后续节点跑两遍。
    """
    snapshot = suspended_runs.get(req.run_id)
    if snapshot is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="挂起的流程不存在或已过期（超过 30 分钟未确认）",
        )
    if snapshot.user_id is not None and snapshot.user_id != int(user_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="挂起的流程不存在或已过期")

    suspended_runs.pop(req.run_id)

    ctx = WorkflowContext.from_snapshot(snapshot.ctx)
    graph = WorkflowGraph.parse(snapshot.graph_raw)
    executor = WorkflowExecutor(graph, ctx, run_id=req.run_id)

    events: list[dict[str, Any]] = []
    output: Any = None
    error: str | None = None
    async for ev in executor.resume(snapshot, req.form_data):
        events.append(ev)
        if ev.get("type") == "workflow_done":
            output = ev.get("output")
            error = ev.get("error")

    if output and snapshot.session_id:
        await _persist_result(snapshot.session_id, str(output))

    await _mark_test_run_finished(
        run_id=req.run_id, output=str(output) if output is not None else None, events=events,
        error=error,
    )

    return Result.ok({"runId": req.run_id, "output": output, "error": error, "events": events})


@router.post("/resume/stream", summary="确认后恢复工作流（SSE）")
async def resume_workflow_stream(
    agent_id: int,
    req: ResumeIn,
    user_id: CurrentUserId,
) -> StreamingResponse:
    """前端 `resumeWorkflowStream()` 打的就是这个 —— 点"确认"之后的续跑。

    和 REST 版语义完全一致（一次性消费 + 越权校验），区别只是边跑边推。
    """

    async def gen():
        def _fail(msg: str):
            return "data:[STATUS]" + json.dumps(
                {"type": "error", "message": msg}, ensure_ascii=False
            ) + "\n\n"

        snapshot = suspended_runs.get(req.run_id)
        if snapshot is None or (
            snapshot.user_id is not None and snapshot.user_id != int(user_id)
        ):
            yield _fail("挂起的流程不存在或已过期")
            yield "data:[DONE]" + json.dumps({"error": "snapshot_not_found"}, ensure_ascii=False) + "\n\n"
            return

        suspended_runs.pop(req.run_id)

        ctx = WorkflowContext.from_snapshot(snapshot.ctx)
        graph = WorkflowGraph.parse(snapshot.graph_raw)
        executor = WorkflowExecutor(graph, ctx, run_id=req.run_id)

        output: Any = None
        error: str | None = None
        events: list[dict[str, Any]] = []
        async for ev in executor.resume(snapshot, req.form_data):
            sse_ev = to_sse_event(ev)
            events.append(sse_ev)
            if sse_ev.get("type") == "workflow_complete":
                output = sse_ev.get("output")
                error = sse_ev.get("error")
            yield "data:[STATUS]" + json.dumps(sse_ev, ensure_ascii=False, default=str) + "\n\n"

        if output and snapshot.session_id:
            await _persist_result(snapshot.session_id, str(output))

        await _mark_test_run_finished(
            run_id=req.run_id, output=str(output) if output is not None else None,
            events=events, error=error,
        )

        yield "data:[DONE]" + json.dumps(
            {
                "runId": req.run_id,
                "output": str(output) if output is not None else "",
                "error": error,
            },
            ensure_ascii=False,
            default=str,
        ) + "\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream", headers=SSE_HEADERS)


@router.post("/abandon", response_model=Result[None], summary="放弃挂起的工作流")
async def abandon_workflow(
    agent_id: int,
    req: AbandonIn,
    user_id: CurrentUserId,
) -> Result[None]:
    """丢弃挂起态（用户点了"取消"）。"""
    snapshot = suspended_runs.pop(req.run_id)
    if snapshot is not None and snapshot.user_id is not None and snapshot.user_id != int(user_id):
        logger.warning("用户 %s 试图放弃别人的挂起流程 %s", user_id, req.run_id)
    return Result.ok()


# ======================================================================
# 内部
# ======================================================================


async def _persist_result(session_id: int, content: str) -> None:
    """把工作流最终输出写成 assistant 消息 + 更新会话统计。"""
    from app.core.snowflake import new_id
    from app.models.entities import Message

    tokens = max(1, len(content) // 4)
    async with SessionLocal() as db:
        db.add(
            Message(
                id=new_id(),
                session_id=session_id,
                role="assistant",
                content=content,
                content_type="text",
                message_type="text",
                token_count=tokens,
                msg_metadata={"workflow": True, "resumed": True},
                create_time=datetime.now(),
            )
        )
        await db.execute(
            text(
                "UPDATE chat_session SET "
                "message_count = message_count + 1, "
                "total_tokens = total_tokens + :tokens, "
                "last_message_at = :ts WHERE id = :sid"
            ),
            {"tokens": tokens, "ts": datetime.now(), "sid": session_id},
        )
        await db.commit()


async def _mark_test_run_finished(
    *,
    run_id: str,
    output: str | None,
    events: list[dict[str, Any]],
    error: str | None,
) -> None:
    """续跑结束后更新对应的 `workflow_test_run` 记录。

    只更新**已存在**的记录 —— 聊天链路的挂起没有 test_run，不该凭空造一条。
    不更新的话记录会永远挂在 `running`，前端一直显示"运行中"。
    """
    from sqlalchemy import update

    from app.models.entities import WorkflowTestRun

    async with SessionLocal() as db:
        exists = (
            await db.execute(
                text("SELECT 1 FROM workflow_test_run WHERE run_id = :r LIMIT 1"), {"r": run_id}
            )
        ).first()
        if not exists:
            return
        await db.execute(
            update(WorkflowTestRun)
            .where(WorkflowTestRun.run_id == run_id)
            .values(
                status="failed" if error else "completed",
                output=output,
                node_events=events,
                error_info=error,
                end_time=datetime.now(),
                update_time=datetime.now(),
            )
        )
        await db.commit()
