from fastapi import APIRouter, Query, Response

from app.api.deps import ChatSessionServiceDep, CurrentUserId
from app.schemas.chat_session import ChatSessionOut
from app.schemas.common import PageResult, Result
from app.schemas.message import MessageOut

router = APIRouter(prefix="/api/chat/sessions", tags=["chat-sessions"])

# 路径与参数名严格对齐 Java 的 ChatSessionController：
#   GET /api/chat/sessions?pageNum=1&pageSize=20&keyword=
# 前端 src/api/chatSession.js 直接拼这些参数，改一个前端就 400。

# ======================================================================
# ⚠️ 静态路径必须全部注册在 `/{session_id}` 之前
# ======================================================================
# 已有：`/tool-calls/{id}/result`（三段，不会被一段的 `/{session_id}` 吃）
# 本轮新增：
#   GET    /search                      ← 一段静态
#   DELETE /batch                       ← 一段静态
#   GET    /messages/starred            ← 两段静态首段
#   PUT    /messages/{id}/star          ← 三段
#
# `/{session_id}` 是一段动态 int。上面这些如果注册在它后面，
# `GET /api/chat/sessions/search` 会被当 session_id="search" → 422，
# 前端只看到"参数错误"，搜索框永远没结果。
# 所以这几个**全部提到文件最前面**。


@router.get("/search", response_model=Result[list[dict]], summary="跨会话搜索消息")
async def search_conversations(
    service: ChatSessionServiceDep,
    user_id: CurrentUserId,
    q: str = Query(..., min_length=1),
    limit: int = Query(20),
) -> Result[list[dict]]:
    """搜内容和关键字都做了归一化（trim / 截 64 字 / limit 夹 1~50）。

    `snippet` 会把关键字**居中**裁剪，不是简单截前 200 字 ——
    否则关键词在正文第 3000 字时，结果里看不到关键词，像搜错了。
    """
    items = await service.search_conversations(user_id, q, limit)
    return Result.ok([x.model_dump(by_alias=True, mode="json") for x in items])


@router.delete("/batch", response_model=Result[str], summary="批量删除会话（物理删）")
async def delete_sessions_batch(
    ids: list[int],
    service: ChatSessionServiceDep,
    user_id: CurrentUserId,
) -> Result[str]:
    """body 是**裸数组** `[1,2,3]`。

    **物理删除**（会话 + 消息 + 该消息的工具调用记录），和单条删除的软删不同 ——
    单条删是"从列表里移走"，批量删是"我不要了"。
    只删属于当前用户的，混着别人的 id 静默跳过（报错会泄露 id 是否存在）。
    """
    deleted = await service.delete_sessions(user_id, ids)
    return Result.ok(f"已删除 {deleted} 个会话")


@router.get(
    "/messages/starred",
    response_model=Result[PageResult[MessageOut]],
    summary="我收藏的所有消息",
)
async def list_starred(
    service: ChatSessionServiceDep,
    user_id: CurrentUserId,
    page_num: int = Query(1, alias="pageNum"),
    page_size: int = Query(20, alias="pageSize"),
) -> Result[PageResult[MessageOut]]:
    return Result.ok(await service.list_starred(user_id, page_num, page_size))


@router.put("/messages/{message_id}/star", response_model=Result[bool], summary="切换消息收藏")
async def toggle_star(
    message_id: int, service: ChatSessionServiceDep, user_id: CurrentUserId
) -> Result[bool]:
    """返回新状态（true = 已收藏）。

    越权链：message → session → user_id。少这一跳，任何登录用户
    都能收藏别人会话里的消息（Java 侧也有这一步）。
    """
    return Result.ok(await service.toggle_star(message_id, user_id))


@router.get("", response_model=Result[PageResult[ChatSessionOut]], summary="分页查询当前用户的会话列表")
async def list_sessions(
    service: ChatSessionServiceDep,
    user_id: CurrentUserId,
    page_num: int = Query(1, alias="pageNum"),
    page_size: int = Query(20, alias="pageSize"),
    keyword: str | None = Query(None),
) -> Result[PageResult[ChatSessionOut]]:
    page = await service.list_sessions(user_id, page_num, page_size, keyword)
    return Result.ok(page)


@router.post("", response_model=Result[ChatSessionOut], summary="创建会话")
async def create_session(
    service: ChatSessionServiceDep,
    user_id: CurrentUserId,
    agent_id: str | None = Query(None, alias="agentId"),
) -> Result[ChatSessionOut]:
    """前端「新对话」时先调这个拿 sessionId，再发起 stream。

    agentId 走 query 参数（对齐 Java 的 @RequestParam），不是 body。
    """
    return Result.ok(await service.create_session(user_id, int(agent_id) if agent_id else None))


@router.get("/{session_id}/title", response_model=Result[str], summary="获取会话标题（轻量轮询）")
async def get_title(
    session_id: int,
    service: ChatSessionServiceDep,
    user_id: CurrentUserId,
) -> Result[str]:
    return Result.ok(await service.get_title(session_id, user_id))


@router.get("/{session_id}", response_model=Result[ChatSessionOut], summary="获取会话详情")
async def get_session(
    session_id: int,
    service: ChatSessionServiceDep,
    user_id: CurrentUserId,
) -> Result[ChatSessionOut]:
    return Result.ok(await service.get_session(session_id, user_id))


@router.get(
    "/{session_id}/messages", response_model=Result[PageResult[MessageOut]], summary="获取会话消息历史"
)
async def list_messages(
    session_id: int,
    service: ChatSessionServiceDep,
    user_id: CurrentUserId,
    page_num: int = Query(1, alias="pageNum"),
    page_size: int = Query(10, alias="pageSize"),
) -> Result[PageResult[MessageOut]]:
    page = await service.list_messages(session_id, user_id, page_num, page_size)
    return Result.ok(page)


# ---------- 会话管理（前端侧栏的重命名 / 置顶 / 删除 / 导出 / 删消息） ----------
# 这几个都是"点一下才发现 404"的接口，一次性补齐，免得逐个撞。


@router.get(
    "/tool-calls/{tool_call_id}/result",
    response_model=Result[str],
    summary="按 toolCallId 拉取工具完整输出（前端展开「查看结果」时调）",
)
async def get_tool_result_detail(
    tool_call_id: int,
    service: ChatSessionServiceDep,
    user_id: CurrentUserId,
) -> Result[str]:
    """历史消息里的 `tool_result.result` 会被瘦身（>500 字删正文、只留长度），
    前端点开详情时用这个接口拿原文。

    **路由顺序有讲究**：这条是三段路径（tool-calls/{id}/result），
    而 `/{session_id}` 是一段、`/{session_id}/title` 是两段，
    所以不会被 "tool-calls" 抢先匹配成 session_id（那样会 422 int 解析失败）。
    以后再加类似静态前缀的路径，要么保持段数不同，要么挪到这个文件最前面注册。
    """
    return Result.ok(await service.get_tool_result_detail(tool_call_id, user_id))


@router.put("/{session_id}/title", response_model=Result[str], summary="重命名会话")
async def rename_session(
    session_id: int,
    service: ChatSessionServiceDep,
    user_id: CurrentUserId,
    title: str = Query(..., description="新标题（query 参数，对齐前端 params 传法）"),
) -> Result[str]:
    return Result.ok(await service.rename_session(session_id, user_id, title))


@router.put("/{session_id}/pin", response_model=Result[bool], summary="置顶 / 取消置顶（取反）")
async def toggle_pin(
    session_id: int,
    service: ChatSessionServiceDep,
    user_id: CurrentUserId,
) -> Result[bool]:
    return Result.ok(await service.toggle_pin(session_id, user_id))


@router.put("/{session_id}/archive", response_model=Result[None], summary="归档会话")
async def archive_session(
    session_id: int,
    service: ChatSessionServiceDep,
    user_id: CurrentUserId,
) -> Result[None]:
    """归档 = `status='archived'`，**不是删除**。

    归档后列表默认不展示，但数据还在、也能被搜索到。
    Java 的 `archiveSession` 漏了归属校验（只 getById），
    任何人传别人的 sessionId 就能把对方会话归档掉 —— 这里补上了。
    """
    await service.archive_session(session_id, user_id)
    return Result.ok(None)


@router.get(
    "/{session_id}/messages/search",
    response_model=Result[PageResult[MessageOut]],
    summary="会话内消息搜索",
)
async def search_messages(
    session_id: int,
    service: ChatSessionServiceDep,
    user_id: CurrentUserId,
    keyword: str = Query(..., min_length=1),
    page_num: int = Query(1, alias="pageNum"),
    page_size: int = Query(20, alias="pageSize"),
) -> Result[PageResult[MessageOut]]:
    return Result.ok(
        await service.search_messages(session_id, user_id, keyword, page_num, page_size)
    )


@router.get(
    "/{session_id}/attachments",
    response_model=Result[list[dict]],
    summary="会话附件列表",
)
async def list_attachments(
    session_id: int,
    service: ChatSessionServiceDep,
    user_id: CurrentUserId,
) -> Result[list[dict]]:
    """附件存在 `chat_session.attachments`（jsonb 数组），**没有独立表、不依赖 MinIO**。"""
    items = await service.list_attachments(session_id, user_id)
    return Result.ok([x.model_dump(by_alias=True, mode="json") for x in items])


@router.delete(
    "/{session_id}/attachments/{attachment_id}",
    response_model=Result[None],
    summary="移除会话附件",
)
async def remove_attachment(
    session_id: int,
    attachment_id: str,
    service: ChatSessionServiceDep,
    user_id: CurrentUserId,
) -> Result[None]:
    """只从 jsonb 数组里摘掉，**不删存储对象**。

    同一个对象可能被别的消息引用，删 key 不该由"从会话移除"隐式触发。
    """
    await service.remove_attachment(session_id, attachment_id, user_id)
    return Result.ok(None)


@router.delete("/{session_id}", response_model=Result[None], summary="删除会话（软删）")
async def delete_session(
    session_id: int,
    service: ChatSessionServiceDep,
    user_id: CurrentUserId,
) -> Result[None]:
    await service.delete_session(session_id, user_id)
    return Result.ok(None)


@router.delete(
    "/{session_id}/messages/{message_id}",
    response_model=Result[None],
    summary="删除单条消息（物理删除，并重算会话统计）",
)
async def delete_message(
    session_id: int,
    message_id: int,
    service: ChatSessionServiceDep,
    user_id: CurrentUserId,
) -> Result[None]:
    await service.delete_message(session_id, message_id, user_id)
    return Result.ok(None)


@router.get("/{session_id}/export", summary="导出会话（markdown / json，裸文本不是 Result）")
async def export_session(
    session_id: int,
    service: ChatSessionServiceDep,
    user_id: CurrentUserId,
    fmt: str = Query("markdown", alias="format"),
) -> Response:
    content, media_type, ext = await service.export_session(session_id, user_id, fmt)
    filename = f"session-{session_id}.{ext}"
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
