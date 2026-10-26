from datetime import datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entities import ChatSession, Message, ToolCall


class ChatSessionRepository:
    """数据访问层。

    对照 Java：相当于 MyBatis-Plus 的 Mapper + Wrapper。
    Python 侧不用写 XML，SQLAlchemy 的 select() 就是类型安全的查询构造器。
    """

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def page_by_user(
        self,
        user_id: int,
        page_num: int,
        page_size: int,
        keyword: str | None = None,
    ) -> tuple[list[ChatSession], int]:
        conditions = [ChatSession.user_id == user_id, ChatSession.deleted == 0]
        if keyword:
            conditions.append(ChatSession.title.ilike(f"%{keyword}%"))

        total = (
            await self.db.execute(select(func.count()).select_from(ChatSession).where(*conditions))
        ).scalar_one()

        stmt = (
            select(ChatSession)
            .where(*conditions)
            # 已核对 Java `ChatSessionServiceImpl.listMySessions`：
            #     .orderByDesc(ChatSession::getPinned)
            #     .orderByDesc(ChatSession::getLastMessageAt)
            # 只有两个字段，**没有 NULLS LAST**。
            # PostgreSQL 的 DESC 默认是 NULLS FIRST，所以"新建但还没发消息"的会话
            # （last_message_at 为空）会排在最前面 —— 这个行为必须照抄，
            # 加了 nulls_last 反而和线上不一致。
            #
            # id DESC 是我加的 tiebreaker：Java 没写，但 last_message_at 相同时
            # PG 不保证顺序，翻页会出现重复/漏行。加它只影响"同一时间戳"的排序，
            # 不影响与 Java 的一致性。
            .order_by(
                ChatSession.pinned.desc(),
                ChatSession.last_message_at.desc(),
                ChatSession.id.desc(),
            )
            .offset((page_num - 1) * page_size)
            .limit(page_size)
        )
        rows = (await self.db.execute(stmt)).scalars().all()
        return list(rows), int(total)

    async def get_by_id(self, session_id: int) -> ChatSession | None:
        """主键点查。

        注意：Java 侧 getById 走 Redis 缓存（30 分钟），Python 侧刻意**不加缓存**。
        学阶段先把正确性做实，缓存等问题出现了再加 —— 有缓存就会有本会话里
        踩过的那个 lost update 坑。
        """
        return await self.db.get(ChatSession, session_id)

    async def page_messages(
        self,
        session_id: int,
        page_num: int,
        page_size: int,
    ) -> tuple[list[Message], int]:
        conditions = [Message.session_id == session_id]
        total = (
            await self.db.execute(select(func.count()).select_from(Message).where(*conditions))
        ).scalar_one()
        stmt = (
            select(Message)
            .where(*conditions)
            .order_by(Message.create_time.desc(), Message.id.desc())
            .offset((page_num - 1) * page_size)
            .limit(page_size)
        )
        rows = (await self.db.execute(stmt)).scalars().all()
        return list(rows), int(total)

    # ---------- 写操作（阶段 5：前端会话管理） ----------

    async def update_title(self, session_id: int, title: str) -> None:
        await self.db.execute(
            update(ChatSession)
            .where(ChatSession.id == session_id)
            .values(title=title, update_time=datetime.now())
        )
        await self.db.commit()

    async def set_pinned(self, session_id: int, pinned: bool) -> None:
        await self.db.execute(
            update(ChatSession)
            .where(ChatSession.id == session_id)
            .values(pinned=pinned, update_time=datetime.now())
        )
        await self.db.commit()

    async def soft_delete(self, session_id: int) -> None:
        """软删：置 deleted=1。消息不删（保留可追溯性，和 Java 侧一致）。"""
        await self.db.execute(
            update(ChatSession)
            .where(ChatSession.id == session_id)
            .values(deleted=1, update_time=datetime.now())
        )
        await self.db.commit()

    async def all_messages(self, session_id: int) -> list[Message]:
        """导出用：按时间正序取全部消息（不分页）。"""
        rows = (
            await self.db.execute(
                select(Message)
                .where(Message.session_id == session_id)
                .order_by(Message.create_time.asc(), Message.id.asc())
            )
        ).scalars().all()
        return list(rows)

    async def delete_message(self, session_id: int, message_id: int) -> bool:
        """物理删除一条消息。

        message 表**没有 deleted 列**（查过 information_schema），所以只能真删。

        删完必须重算会话统计 —— 但**不能用"读出实体再改再 updateById"**：
        那种 read-modify-write 在并发下会丢更新（Java 侧 2026-10-18 的
        token 统计被抹掉就是这个原因）。这里用一条带子查询的 UPDATE，
        让数据库在一次语句里算完，天然原子。
        """
        result = await self.db.execute(
            delete(Message).where(Message.id == message_id, Message.session_id == session_id)
        )
        if result.rowcount == 0:
            await self.db.rollback()
            return False

        # 连带删掉这条消息名下的工具调用记录 —— 否则前端还能按 id 回查到
        # 一个"已删除消息"的完整工具输出，既是脏数据也是越权面。
        # 对照 Java：MessageServiceImpl 删除链路里调的 toolCallMapper.deleteByMessageIds。
        await self.db.execute(delete(ToolCall).where(ToolCall.message_id == message_id))

        await self.db.execute(
            update(ChatSession)
            .where(ChatSession.id == session_id)
            .values(
                message_count=select(func.count())
                .select_from(Message)
                .where(Message.session_id == session_id)
                .scalar_subquery(),
                total_tokens=select(func.coalesce(func.sum(Message.token_count), 0))
                .select_from(Message)
                .where(Message.session_id == session_id)
                .scalar_subquery(),
                last_message_at=select(func.max(Message.create_time))
                .select_from(Message)
                .where(Message.session_id == session_id)
                .scalar_subquery(),
                update_time=datetime.now(),
            )
        )
        await self.db.commit()
        return True

    async def get_tool_call(self, tool_call_id: int) -> ToolCall | None:
        """按主键查工具调用记录（拿完整 tool_output 用）。

        这是个**不加锁、不过滤状态**的点查：tool_calls 表没有 deleted 列，
        删除是靠删消息时级联物理删，所以查到的一定是有效记录。
        """
        return await self.db.get(ToolCall, tool_call_id)

    async def get_message(self, message_id: int) -> Message | None:
        """主键点查消息（越权校验链上的一跳：tool_call → message → session）。"""
        return await self.db.get(Message, message_id)

    # ------------------------------------------------------------------
    # 阶段 13 新增：搜索 / 收藏 / 归档 / 批量删
    # ------------------------------------------------------------------

    async def archive(self, session_id: int) -> None:
        """归档：`status='archived'`。

        注意**不是软删** —— 归档的会话还在库里、还能被搜到，
        只是列表默认不展示。和 `deleted=1` 是两件事，别混。
        """
        await self.db.execute(
            update(ChatSession)
            .where(ChatSession.id == session_id)
            .values(status="archived", update_time=datetime.now())
        )
        await self.db.commit()

    async def hard_delete_many(self, user_id: int, ids: list[int]) -> int:
        """批量**物理**删除会话及其消息（Java `deleteSessions` 也是物理删）。

        为什么物理删：前端"批量删除"的语义是"这些记录我不要了"，
        留着 `deleted=1` 只会在库里堆垃圾，还会占着唯一标题。

        **必须带 `user_id` 条件**：ids 来自前端，不校验归属就是批量越权。
        """
        if not ids:
            return 0
        owned = (
            await self.db.execute(
                select(ChatSession.id).where(
                    ChatSession.id.in_(ids), ChatSession.user_id == user_id
                )
            )
        ).scalars().all()
        if not owned:
            return 0

        # 先删消息（含关联的 tool_calls / feedback），再删会话。
        # message 表没有外键级联，顺序反了会留孤儿消息。
        msg_ids = (
            await self.db.execute(select(Message.id).where(Message.session_id.in_(owned)))
        ).scalars().all()
        if msg_ids:
            await self.db.execute(
                delete(ToolCall).where(ToolCall.message_id.in_(msg_ids))
            )
        await self.db.execute(delete(Message).where(Message.session_id.in_(owned)))
        result = await self.db.execute(delete(ChatSession).where(ChatSession.id.in_(owned)))
        await self.db.commit()
        return int(result.rowcount or 0)

    async def search_messages(
        self, session_id: int, keyword: str, page_num: int, page_size: int
    ) -> tuple[list[Message], int]:
        """会话内消息模糊搜索。

        用 `ilike`（不区分大小写）。PG 上中文不分大小写，所以中英混排都能命中。
        不含 `%` / `_` 转义 —— 用户输入这两个字符会被当通配符，
        Java 侧也是这么写的，行为一致（真要修两边一起改）。
        """
        like = f"%{keyword}%"
        conditions = [Message.session_id == session_id, Message.content.ilike(like)]
        total = int(
            (
                await self.db.execute(
                    select(func.count()).select_from(Message).where(*conditions)
                )
            ).scalar()
            or 0
        )
        rows = (
            await self.db.execute(
                select(Message)
                .where(*conditions)
                .order_by(Message.create_time.desc().nulls_last(), Message.id.desc())
                .offset((max(page_num, 1) - 1) * page_size)
                .limit(page_size)
            )
        ).scalars().all()
        return list(rows), total

    async def search_across_sessions(
        self, user_id: int, keyword: str, limit: int
    ) -> list[dict]:
        """跨会话搜索：返回消息 + 所属会话标题 / Agent / 是否置顶。

        一条 JOIN 出结果，避免"先搜消息再逐条查会话"的 N+1。
        只搜 `deleted=0` 的会话 —— 用户删掉的会话不该出现在搜索里。
        """
        like = f"%{keyword}%"
        rows = (
            await self.db.execute(
                select(
                    Message.id,
                    Message.role,
                    Message.content,
                    Message.create_time,
                    ChatSession.id.label("session_id"),
                    ChatSession.title,
                    ChatSession.agent_id,
                    ChatSession.pinned,
                    ChatSession.last_message_at,
                )
                .join(ChatSession, ChatSession.id == Message.session_id)
                .where(
                    ChatSession.user_id == user_id,
                    ChatSession.deleted == 0,
                    Message.content.ilike(like),
                )
                .order_by(Message.create_time.desc().nulls_last(), Message.id.desc())
                .limit(limit)
            )
        ).mappings().all()
        return [dict(r) for r in rows]

    async def toggle_star(self, message_id: int) -> bool:
        """消息收藏开关（取反），返回新状态。

        用一条 UPDATE + RETURNING 完成，不做 read-modify-write ——
        收藏是低风险操作，但一条语句能省一次往返。
        """
        result = await self.db.execute(
            update(Message)
            .where(Message.id == message_id)
            .values(starred=~func.coalesce(Message.starred, False))
            .returning(Message.starred)
        )
        row = result.first()
        await self.db.commit()
        return bool(row[0]) if row else False

    async def page_starred(
        self, user_id: int, page_num: int, page_size: int
    ) -> tuple[list[Message], int]:
        """收藏消息列表。**必须按 user_id 过滤** ——
        不加就是全站用户的收藏混在一起，是明确的数据泄露。
        """
        session_subq = select(ChatSession.id).where(
            ChatSession.user_id == user_id, ChatSession.deleted == 0
        )
        conditions = [Message.session_id.in_(session_subq), Message.starred.is_(True)]
        total = int(
            (
                await self.db.execute(
                    select(func.count()).select_from(Message).where(*conditions)
                )
            ).scalar()
            or 0
        )
        rows = (
            await self.db.execute(
                select(Message)
                .where(*conditions)
                .order_by(Message.create_time.desc().nulls_last(), Message.id.desc())
                .offset((max(page_num, 1) - 1) * page_size)
                .limit(page_size)
            )
        ).scalars().all()
        return list(rows), total

    async def get_attachments(self, session_id: int) -> list[dict]:
        """会话附件（存在 `chat_session.attachments` jsonb 数组里）。

        没有独立附件表。存储形态是数组，但历史数据里可能是
        `{"items": [...]}` 这种包装 —— 两种都兼容，解析失败返回空。
        """
        raw = (
            await self.db.execute(
                select(ChatSession.attachments).where(ChatSession.id == session_id)
            )
        ).scalar()
        return _as_attachment_list(raw)

    async def set_attachments(self, session_id: int, items: list[dict]) -> None:
        await self.db.execute(
            update(ChatSession)
            .where(ChatSession.id == session_id)
            .values(attachments=items, update_time=datetime.now())
        )
        await self.db.commit()


def _as_attachment_list(raw: object) -> list[dict]:
    """把 attachments 列解析成数组。

    兼容三种形态（历史数据实测都有）：
    - `None` → []
    - `[...]` → 原样
    - `{"items": [...]}` → 取 items
    """
    if raw is None:
        return []
    if isinstance(raw, list):
        return [x for x in raw if isinstance(x, dict)]
    if isinstance(raw, dict):
        items = raw.get("items")
        if isinstance(items, list):
            return [x for x in items if isinstance(x, dict)]
    return []
