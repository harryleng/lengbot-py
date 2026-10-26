from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse

from app.api.deps import CurrentUserId, DbSession
from app.schemas.chat import ChatRequestIn, ReconnectIn
from app.schemas.common import PageResult, Result
from app.schemas.feedback import FeedbackIn
from app.services.chat_runs import registry, replay
from app.services.chat_stream_service import ChatStreamService
from app.services.message_feedback_service import MessageFeedbackService
from app.services.rag_reference_service import RagReferenceService

router = APIRouter(prefix="/api/chat", tags=["chat"])

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    # 关键：如果前面有 nginx，这一行让它不要缓冲，否则 token 会攒一批才吐给前端，
    # 流式就变成"卡几秒然后一大段"。FastAPI 自己不会缓冲，但反代经常默认开。
    "X-Accel-Buffering": "no",
}


@router.post("", response_model=Result[str], summary="同步对话（非流式，等完整回复）")
async def chat_sync(req: ChatRequestIn, user_id: CurrentUserId) -> Result[str]:
    """Java `ChatService.chat`：非流式语义，但**内部复用流式管道** ——
    收集正文帧拼完整回复，落库/工具事件/用量统计全部走同一条路，
    避免两套对话实现漂移。控制帧（STATUS/DONE/REQUEST_ID/TOOL_EVENT）不进正文。"""
    parts: list[str] = []
    async for frame in ChatStreamService().stream(req, user_id):
        for line in frame.splitlines():
            if not line.startswith("data:"):
                continue
            payload = line[5:]
            if not payload or payload.startswith("["):
                continue
            parts.append(payload)
    return Result.ok("".join(parts))


@router.post(
    "/stream",
    summary="流式对话（SSE）",
    response_description="text/event-stream：正文 token / [STATUS] / [DONE] / [REQUEST_ID]",
)
async def chat_stream(req: ChatRequestIn, user_id: CurrentUserId) -> StreamingResponse:
    return StreamingResponse(
        ChatStreamService().stream(req, user_id),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


@router.post("/stream/stop", response_model=Result[None], summary="停止流式对话")
async def stop_stream(
    user_id: CurrentUserId,
    request_id: str = Query(..., alias="requestId"),
) -> Result[None]:
    """置位取消信号。

    注意语义：**这是"请求停止"，不是"已经停止"**。真正中断发生在
    主循环下一次检查 cancel 的地方（下一个 token 到达时），
    所以调用方拿到 200 不代表流已经断了 —— 还需要等 `[DONE]`。

    run 不存在（已经结束）或不属于你，同样返回成功：
    幂等的停止调用不应该报错，也不该泄露 requestId 是否存在。
    """
    registry.stop(request_id, user_id)
    return Result.ok()


@router.post(
    "/stream/reconnect",
    summary="SSE 断线重连续流",
    response_description="text/event-stream：从 lastEventId 之后重放，并继续推送直到结束",
)
async def stream_reconnect(req: ReconnectIn, user_id: CurrentUserId) -> StreamingResponse:
    return StreamingResponse(
        replay(req.request_id, req.last_event_id, user_id),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


# ======================================================================
# 消息反馈（👍 / 👎）
# ======================================================================
# **路由顺序**：`/messages/feedbacks/batch` 是静态路径（4 段），
# `/messages/{message_id}/feedback` 也是 4 段但末段是动态的 ——
# 两者不会互相吃掉（末段字面量不同）。但为了保险，
# 静态的 `feedbacks/batch` 仍然排在动态前面。


@router.post(
    "/messages/feedbacks/batch",
    response_model=Result[dict],
    summary="批量查消息反馈（key 是 messageId 字符串）",
)
async def batch_get_feedbacks(
    message_ids: list[int],
    db: DbSession,
    user_id: CurrentUserId,
) -> Result[dict]:
    """body 是**裸数组** `[1, 2, 3]`（对齐 Java `@RequestBody List<Long>`）。

    返回 **对象**（不是数组）：`{"372xxx": {...}, "372yyy": {...}}`。
    前端写法是 `feedbacks[String(msg.id)]`，给数组它取不到 ——
    这个不对齐的话，消息列表上的点赞状态会全部显示成"未点"。
    """
    return Result.ok(await MessageFeedbackService(db).batch_get(user_id, message_ids))


@router.get(
    "/messages/{message_id}/feedback",
    response_model=Result[dict | None],
    summary="当前用户对某条消息的反馈（没点过返回 null）",
)
async def get_message_feedback(
    message_id: int, db: DbSession, user_id: CurrentUserId
) -> Result[dict | None]:
    fb = await MessageFeedbackService(db).get_my(message_id, user_id)
    return Result.ok(None if fb is None else fb.model_dump(by_alias=True, mode="json"))


@router.post(
    "/messages/{message_id}/feedback",
    response_model=Result[dict | None],
    summary="提交 / 切换 / 取消反馈（toggle）",
)
async def submit_feedback(
    message_id: int,
    dto: FeedbackIn,
    db: DbSession,
    user_id: CurrentUserId,
) -> Result[dict | None]:
    """**返回 null 表示"取消了"**（同类型再点一次）。

    前端拿到 `data: null` 就把图标恢复成未选中 —— 别改成返回
    `{rating: null}`，那样前端判断 `if (res.data)` 会走错分支。
    """
    fb = await MessageFeedbackService(db).submit(message_id, user_id, dto)
    return Result.ok(None if fb is None else fb.model_dump(by_alias=True, mode="json"))


@router.get("/feedbacks", response_model=Result[dict], summary="我的反馈列表（分页）")
async def list_my_feedbacks(
    db: DbSession,
    user_id: CurrentUserId,
    page_num: int = Query(1, alias="pageNum"),
    page_size: int = Query(20, alias="pageSize"),
    rating: str | None = Query(None),
) -> Result[dict]:
    records, total = await MessageFeedbackService(db).list_my(
        user_id, page_num, page_size, rating
    )
    page = PageResult.of(
        records=[r.model_dump(by_alias=True, mode="json") for r in records],
        total=total,
        current=page_num,
        size=page_size,
    )
    return Result.ok(page.model_dump(by_alias=True, mode="json"))


@router.get("/feedbacks/stats", response_model=Result[dict], summary="我的反馈统计")
async def feedback_stats(db: DbSession, user_id: CurrentUserId) -> Result[dict]:
    """`{total, likeCount, dislikeCount}` —— key 是驼峰。"""
    return Result.ok(await MessageFeedbackService(db).stats(user_id))


# ======================================================================
# RAG 引用
# ======================================================================


@router.get("/rag-references", response_model=Result[list[dict]], summary="按问题重新检索知识库引用")
async def get_rag_references(
    db: DbSession,
    user_id: CurrentUserId,
    session_id: int = Query(..., alias="sessionId"),
    question: str = Query(...),
    agent_id: int | None = Query(None, alias="agentId"),
) -> Result[list[dict]]:
    """前端点消息气泡上的「引用」按钮时调。

    `agentId` 不传就回落到会话绑定的 Agent —— 前端有时只在会话页能拿到 sessionId。

    **失败返回空数组不报错**：Agent 没绑库 / 库没向量化 / 向量模型不通，
    用户看到的都是"暂无引用"。点引用是想看有没有额外资料，
    不是想处理一个异常弹窗。
    """
    resolved_agent = agent_id
    if resolved_agent is None:
        from sqlalchemy import select as _select

        from app.models.entities import ChatSession

        resolved_agent = (
            await db.execute(
                _select(ChatSession.agent_id).where(
                    ChatSession.id == session_id, ChatSession.user_id == user_id
                )
            )
        ).scalar()

    refs = await RagReferenceService(db).get_references(
        session_id, resolved_agent, question
    )
    return Result.ok([r.model_dump(by_alias=True, mode="json") for r in refs])
