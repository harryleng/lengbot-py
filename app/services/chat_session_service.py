import json
from datetime import datetime

from fastapi import HTTPException, status

from app.core.constants import DEFAULT_TITLE
from app.core.snowflake import new_id
from app.models.entities import ChatSession
from app.repositories.chat_session_repo import ChatSessionRepository
from app.schemas.chat_session import ChatSessionOut
from app.schemas.common import PageResult
from app.schemas.feedback import ConversationSearchOut, SessionAttachmentOut
from app.schemas.message import MessageOut
from app.services.tool_events import compact_events_for_list


class ChatSessionService:
    """业务层。对照 Java：`ChatSessionServiceImpl`。

    刻意保持薄：阶段 0 只有读操作，没有缓存、没有统计累加。
    """

    def __init__(self, repo: ChatSessionRepository) -> None:
        self.repo = repo

    async def create_session(self, user_id: int, agent_id: int | None) -> ChatSessionOut:
        """创建会话。

        为什么需要：前端 `useChatStream` 是**先 createSession 拿到 sid，再发 stream**，
        不是让后端在流里创建。缺这个接口，前端点"新对话"就 404。

        id 必须应用侧生成 snowflake —— 表里 id 没有自增序列。
        """
        now = datetime.now()
        session = ChatSession(
            id=new_id(),
            agent_id=agent_id,
            user_id=user_id,
            title=DEFAULT_TITLE,  # 对齐 Java：新会话标题是"新对话"，首轮问答后自动替换
            status="active",
            context={},
            message_count=0,
            total_tokens=0,
            pinned=False,
            create_time=now,
            update_time=now,
            deleted=0,
        )
        self.repo.db.add(session)
        await self.repo.db.commit()
        return ChatSessionOut.model_validate(session)

    async def get_title(self, session_id: int, user_id: int) -> str:
        """轻量取标题，供前端轮询。

        Java 侧这个接口特意跳过缓存，因为标题是异步生成的，
        缓存会导致前端一直看到旧标题。
        """
        session = await self.repo.get_by_id(session_id)
        self._ensure_owned(session, user_id)
        return session.title or ""

    async def list_sessions(
        self,
        user_id: int,
        page_num: int,
        page_size: int,
        keyword: str | None = None,
    ) -> PageResult[ChatSessionOut]:
        rows, total = await self.repo.page_by_user(user_id, page_num, page_size, keyword)
        return PageResult.of(
            records=[ChatSessionOut.model_validate(r) for r in rows],
            total=total,
            current=page_num,
            size=page_size,
        )

    async def get_session(self, session_id: int, user_id: int) -> ChatSessionOut:
        session = await self.repo.get_by_id(session_id)
        self._ensure_owned(session, user_id)
        return ChatSessionOut.model_validate(session)

    async def list_messages(
        self,
        session_id: int,
        user_id: int,
        page_num: int,
        page_size: int,
    ) -> PageResult[MessageOut]:
        session = await self.repo.get_by_id(session_id)
        self._ensure_owned(session, user_id)
        rows, total = await self.repo.page_messages(session_id, page_num, page_size)
        records: list[MessageOut] = []
        for r in rows:
            out = MessageOut.model_validate(r)
            # 历史列表瘦身：超长 tool_result 正文换成 resultTotalLength。
            # 只改 DTO、不动 ORM 对象 —— 直接改 r.tool_events 会让这个 session
            # 变成 dirty，后面任何一次 flush 都可能把瘦身后的数据写回库，把完整记录毁掉。
            if out.tool_events:
                out.tool_events = compact_events_for_list(out.tool_events)
            records.append(out)
        return PageResult.of(
            records=records,
            total=total,
            current=page_num,
            size=page_size,
        )

    async def get_tool_result_detail(self, tool_call_id: int, user_id: int) -> str:
        """按 toolCallId 拉工具完整输出（前端「查看结果」展开时调）。

        三跳越权校验：tool_call → message → chat_session.user_id。
        为什么不直接信 toolCallId：雪花 ID 虽不可猜，但会出现在分享截图、
        日志里，不校验就等于把别人的工具输出（可能含文件内容、SQL 结果）敞开。
        """
        call = await self.repo.get_tool_call(tool_call_id)
        if call is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="工具调用记录不存在")

        message = await self.repo.get_message(call.message_id) if call.message_id else None
        if message is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="工具调用记录不存在")

        session = await self.repo.get_by_id(message.session_id)
        self._ensure_owned(session, user_id)

        # 空结果返回空串而不是 404：前端拿到空串会显示"（空结果）"，
        # 而 404 会被它当成"加载失败"弹错误提示 —— 一个本来正常的空结果
        # （比如 web_search 没搜到）不该报错。
        return call.tool_output or ""

    # ---------- 会话管理（阶段 5） ----------

    async def rename_session(self, session_id: int, user_id: int, title: str) -> str:
        session = await self.repo.get_by_id(session_id)
        self._ensure_owned(session, user_id)
        clean = title.strip()[:100]
        if not clean:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="标题不能为空")
        await self.repo.update_title(session_id, clean)
        return clean

    async def toggle_pin(self, session_id: int, user_id: int) -> bool:
        """置顶开关：取反。

        注意这里读的是**实体当前值再写回**，看似违反"别 read-modify-write"，
        但置顶是用户手动点击的低频操作，且冲突语义本来就是「我点的这次生效」，
        并发双击最多回到原状态，不会像 token 统计那样丢数据。
        """
        session = await self.repo.get_by_id(session_id)
        self._ensure_owned(session, user_id)
        new_value = not bool(session.pinned)
        await self.repo.set_pinned(session_id, new_value)
        return new_value

    async def delete_session(self, session_id: int, user_id: int) -> None:
        session = await self.repo.get_by_id(session_id)
        self._ensure_owned(session, user_id)
        await self.repo.soft_delete(session_id)

    async def delete_message(self, session_id: int, message_id: int, user_id: int) -> None:
        session = await self.repo.get_by_id(session_id)
        self._ensure_owned(session, user_id)
        ok = await self.repo.delete_message(session_id, message_id)
        if not ok:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="消息不存在")

    async def export_session(
        self, session_id: int, user_id: int, fmt: str
    ) -> tuple[str, str, str]:
        """导出会话，返回 (内容, media_type, 文件扩展名)。

        前端用 `responseType: 'blob'` 接，所以这里返回**裸文本**，
        不能包 Result —— 包了前端会把 JSON 当文件下载下来。
        """
        session = await self.repo.get_by_id(session_id)
        self._ensure_owned(session, user_id)
        rows = await self.repo.all_messages(session_id)

        if fmt == "json":
            payload = {
                "session": ChatSessionOut.model_validate(session).model_dump(
                    by_alias=True, mode="json"
                ),
                "messages": [
                    MessageOut.model_validate(m).model_dump(by_alias=True, mode="json")
                    for m in rows
                ],
            }
            return json.dumps(payload, ensure_ascii=False, indent=2), "application/json", "json"

        role_names = {"user": "用户", "assistant": "助手", "system": "系统", "tool": "工具"}
        parts = [
            f"# {session.title or '未命名会话'}",
            "",
            f"- 会话 ID：{session.id}",
            f"- 消息数：{len(rows)}",
            f"- 总 token：{session.total_tokens}",
            f"- 导出时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
            "",
            "---",
            "",
        ]
        for m in rows:
            parts.append(f"## {role_names.get(m.role, m.role)}")
            parts.append("")
            parts.append((m.content or "").strip())
            parts.append("")
        return "\n".join(parts), "text/markdown; charset=utf-8", "md"


    # ------------------------------------------------------------------
    # 阶段 13：搜索 / 收藏 / 归档 / 批量删 / 附件
    # ------------------------------------------------------------------

    async def archive_session(self, session_id: int, user_id: int) -> None:
        """归档。**不是删除** —— 归档后 `status='archived'`，
        列表默认不展示但数据还在，也能被搜索到。

        Java 的 `archiveSession` **没校验归属**（只 getById），这是个真实的越权：
        任何人传别人的 sessionId 就能把对方会话归档掉。这里补上。
        """
        session = await self.repo.get_by_id(session_id)
        self._ensure_owned(session, user_id)
        await self.repo.archive(session_id)

    async def delete_sessions(self, user_id: int, ids: list[int]) -> int:
        """批量物理删除。返回实际删掉的条数。

        **只删属于当前用户的**（repo 里带 user_id 条件）。
        前端传一批 id，其中混着别人的也不会报错 —— 静默跳过，
        因为报错反而泄露了"这个 id 存在但不属于你"。
        """
        if not ids:
            return 0
        return await self.repo.hard_delete_many(user_id, ids)

    async def search_messages(
        self, session_id: int, user_id: int, keyword: str, page_num: int, page_size: int
    ) -> PageResult[MessageOut]:
        session = await self.repo.get_by_id(session_id)
        self._ensure_owned(session, user_id)
        rows, total = await self.repo.search_messages(session_id, keyword, page_num, page_size)
        return PageResult.of(
            records=[MessageOut.model_validate(r) for r in rows],
            total=total,
            current=page_num,
            size=page_size,
        )

    async def search_conversations(
        self, user_id: int, keyword: str, limit: int
    ) -> list[ConversationSearchOut]:
        """跨会话搜索。

        三个归一化动作（Java 也有，别省）：
        1. trim 后为空 → 直接返回空，避免 `ILIKE '%%'` 全表扫
        2. 关键词截到 64 字，避免超长关键字拖慢 ILIKE
        3. limit 夹到 1~50，避免前端传 10000
        """
        normalized = (keyword or "").strip()
        if not normalized:
            return []
        bounded = normalized[:64]
        bounded_limit = max(1, min(limit, 50))

        rows = await self.repo.search_across_sessions(user_id, bounded, bounded_limit)
        return [
            ConversationSearchOut(
                message_id=str(r["id"]),
                message_role=r["role"],
                snippet=_truncate_snippet(r["content"] or "", bounded, 200),
                message_create_time=r["create_time"],
                session_id=str(r["session_id"]),
                session_title=r["title"],
                agent_id=str(r["agent_id"]) if r["agent_id"] else None,
                pinned=bool(r["pinned"]),
                session_last_message_at=r["last_message_at"],
            )
            for r in rows
        ]

    async def toggle_star(self, message_id: int, user_id: int) -> bool:
        """收藏开关，返回新状态。

        越权校验链：message → session → user_id。少了这一跳，
        任何登录用户都能收藏别人会话里的消息。
        """
        msg = await self.repo.get_message(message_id)
        if msg is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="消息不存在")
        session = await self.repo.get_by_id(int(msg.session_id))
        self._ensure_owned(session, user_id)
        return await self.repo.toggle_star(message_id)

    async def list_starred(
        self, user_id: int, page_num: int, page_size: int
    ) -> PageResult[MessageOut]:
        rows, total = await self.repo.page_starred(user_id, page_num, page_size)
        return PageResult.of(
            records=[MessageOut.model_validate(r) for r in rows],
            total=total,
            current=page_num,
            size=page_size,
        )

    async def list_attachments(
        self, session_id: int, user_id: int
    ) -> list[SessionAttachmentOut]:
        """会话附件。按创建时间倒序（新的在前）。

        附件存在 `chat_session.attachments` 这个 jsonb 数组里，**没有独立表**，
        也不依赖 MinIO —— 所以这批接口能完整工作。
        `previewUrl` 原样透传存量值（Java 会按需刷新，那一步要打 MinIO，跳过）。
        """
        session = await self.repo.get_by_id(session_id)
        self._ensure_owned(session, user_id)
        items = await self.repo.get_attachments(session_id)
        out: list[SessionAttachmentOut] = []
        for raw in items:
            try:
                out.append(SessionAttachmentOut.model_validate(raw))
            except Exception:  # noqa: BLE001
                # 单条脏数据不该让整个列表 500
                continue
        out.sort(key=lambda x: x.created_at or "", reverse=True)
        return out

    async def remove_attachment(
        self, session_id: int, attachment_id: str, user_id: int
    ) -> None:
        """移除附件（只从 jsonb 数组里摘掉，不动存储对象）。

        存储对象（MinIO）由 Java 侧的清理任务处理；Python 版不碰，
        因为删一个 key 不该由"从会话里移除"这个动作隐式触发 ——
        同一个对象可能被别的消息引用。
        """
        session = await self.repo.get_by_id(session_id)
        self._ensure_owned(session, user_id)
        items = await self.repo.get_attachments(session_id)
        kept = [x for x in items if str(x.get("id") or "") != str(attachment_id)]
        if len(kept) == len(items):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="附件不存在"
            )
        await self.repo.set_attachments(session_id, kept)

    @staticmethod
    def _ensure_owned(session: ChatSession | None, user_id: int) -> None:
        """归属校验，防 IDOR 越权（Java 侧同样的逻辑在 ensureOwnedByUser）。

        注意：不存在和没权限返回相同的 404，不要用 403 —— 403 会泄露
        "这个 id 确实存在"的信息。
        """
        if session is None or session.deleted != 0 or session.user_id != user_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在")


def _truncate_snippet(content: str, keyword: str, max_len: int) -> str:
    """裁剪搜索片段：关键字居中，前后各留上下文。

    为什么不能简单 `content[:200]`：用户搜的关键词可能在正文第 3000 字，
    截前面 200 字的话搜索结果里完全看不到关键词，看起来像"搜错了"。
    """
    if not content:
        return ""
    if len(content) <= max_len:
        return content
    idx = content.lower().find(keyword.lower())
    if idx < 0:
        return content[:max_len] + "…"
    start = max(0, idx - 60)
    end = min(len(content), start + max_len)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(content) else ""
    return prefix + content[start:end] + suffix
