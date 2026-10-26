"""消息反馈服务（👍/👎 + 统计）。

对应 Java `MessageFeedbackServiceImpl`。

**核心是 toggle 三态**，只做"新增或覆盖"会出问题：

| 已有反馈 | 新提交 | 结果 |
|---|---|---|
| 无 | like | 新建 |
| like | like | **删除**（再点一次取消） |
| like | dislike | 切换 |

前端的心智模型就是"点一下点亮、再点一下熄灭"，只实现 upsert 的话
用户永远取消不掉自己点错的那个赞。

`agent_id` / `agent_version` 在**提交时快照**（顺着 message → session 查）。
不实时关联是有意的：Agent 后面改名、改版本、甚至删掉，
统计报表仍然要能回答"这条差评是哪个版本给的"。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.snowflake import new_id
from app.models.entities import Agent, AgentVersion, ChatSession, Message, MessageFeedback
from app.schemas.feedback import (
    RATING_DISLIKE,
    RATING_LIKE,
    FeedbackIn,
    FeedbackListOut,
    FeedbackOut,
)

logger = logging.getLogger(__name__)


def _not_found(msg: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=msg)


class MessageFeedbackService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # ------------------------------------------------------------------
    async def _ensure_message_owned(self, message_id: int, user_id: int) -> Message:
        """反馈也要校验归属 —— 否则能给别人会话里的消息点赞，
        统计里就混进了不属于自己的数据。"""
        msg = (
            await self.db.execute(select(Message).where(Message.id == message_id))
        ).scalar_one_or_none()
        if msg is None:
            raise _not_found("消息不存在")
        session = (
            await self.db.execute(
                select(ChatSession.user_id).where(ChatSession.id == msg.session_id)
            )
        ).scalar()
        if session is None or int(session) != int(user_id):
            raise _not_found("消息不存在")
        return msg

    async def _agent_snapshot(self, session_id: int) -> tuple[int | None, int | None]:
        """顺着 session 拿 (agent_id, agent_version)。

        `agent_version_id` 为空时回退到 `agent.version`（当前生效版本）——
        老会话可能没记 version_id。
        """
        row = (
            await self.db.execute(
                select(ChatSession.agent_id, ChatSession.agent_version_id).where(
                    ChatSession.id == session_id
                )
            )
        ).first()
        if row is None:
            return None, None
        agent_id, version_id = row[0], row[1]
        if version_id:
            version = (
                await self.db.execute(
                    select(AgentVersion.version).where(AgentVersion.id == version_id)
                )
            ).scalar()
            if version is not None:
                return (int(agent_id) if agent_id else None), int(version)
        if agent_id:
            version = (
                await self.db.execute(select(Agent.version).where(Agent.id == agent_id))
            ).scalar()
            return int(agent_id), (int(version) if version is not None else None)
        return None, None

    # ------------------------------------------------------------------
    async def submit(
        self, message_id: int, user_id: int, dto: FeedbackIn
    ) -> FeedbackOut | None:
        """提交反馈。**返回 None 表示"取消了"**（Java 也返回 null）。

        前端拿到 `data: null` 就把图标恢复成未选中状态。
        """
        await self._ensure_message_owned(message_id, user_id)

        existing = (
            await self.db.execute(
                select(MessageFeedback).where(
                    MessageFeedback.message_id == message_id,
                    MessageFeedback.user_id == user_id,
                )
            )
        ).scalar_one_or_none()

        if existing is not None:
            if existing.rating == dto.rating:
                # 同类型 → 取消
                await self.db.delete(existing)
                await self.db.commit()
                logger.info("取消反馈 message=%s user=%s rating=%s",
                            message_id, user_id, dto.rating)
                return None
            # 不同类型 → 切换
            existing.rating = dto.rating
            existing.reason = dto.reason
            await self.db.commit()
            logger.info("切换反馈 message=%s user=%s → %s", message_id, user_id, dto.rating)
            return FeedbackOut.model_validate(existing)

        msg = await self._ensure_message_owned(message_id, user_id)
        agent_id, agent_version = await self._agent_snapshot(int(msg.session_id))

        row = MessageFeedback(
            id=new_id(),
            message_id=message_id,
            user_id=user_id,
            rating=dto.rating,
            reason=dto.reason,
            agent_id=agent_id,
            agent_version=agent_version,
            create_time=datetime.now(),
        )
        self.db.add(row)
        await self.db.commit()
        logger.info("新增反馈 message=%s user=%s rating=%s agent=%s v%s",
                    message_id, user_id, dto.rating, agent_id, agent_version)
        return FeedbackOut.model_validate(row)

    async def get_my(self, message_id: int, user_id: int) -> FeedbackOut | None:
        """当前用户对某条消息的反馈。没点过返回 **null**（不是 404）。"""
        row = (
            await self.db.execute(
                select(MessageFeedback).where(
                    MessageFeedback.message_id == message_id,
                    MessageFeedback.user_id == user_id,
                )
            )
        ).scalar_one_or_none()
        return None if row is None else FeedbackOut.model_validate(row)

    async def batch_get(
        self, user_id: int, message_ids: list[int]
    ) -> dict[str, FeedbackOut]:
        """批量查反馈，返回 **以 messageId 字符串为 key 的对象**。

        前端用法：`feedbacks[String(msg.id)]`。返回数组的话它取不到 ——
        这是个很容易写错的地方（Java 返回 `Map<Long, MessageFeedback>`，
        Jackson 序列化后 key 必然是字符串）。
        """
        if not message_ids:
            return {}
        rows = (
            await self.db.execute(
                select(MessageFeedback).where(
                    MessageFeedback.user_id == user_id,
                    MessageFeedback.message_id.in_(message_ids),
                )
            )
        ).scalars().all()
        return {str(r.message_id): FeedbackOut.model_validate(r) for r in rows}

    async def list_my(
        self,
        user_id: int,
        page_num: int = 1,
        page_size: int = 20,
        rating: str | None = None,
    ) -> tuple[list[FeedbackListOut], int]:
        """反馈列表。带上消息正文 / 会话 id / Agent 名，避免前端逐条补查。"""
        conditions = [MessageFeedback.user_id == user_id]
        if rating:
            conditions.append(MessageFeedback.rating == rating)

        total = int(
            (
                await self.db.execute(
                    select(func.count()).select_from(MessageFeedback).where(*conditions)
                )
            ).scalar()
            or 0
        )
        rows = (
            await self.db.execute(
                select(MessageFeedback)
                .where(*conditions)
                .order_by(MessageFeedback.create_time.desc().nulls_last(),
                          MessageFeedback.id.desc())
                .offset((max(page_num, 1) - 1) * page_size)
                .limit(page_size)
            )
        ).scalars().all()
        if not rows:
            return [], total

        # 批量补消息 / 会话 / Agent，避免 N+1
        msg_ids = [r.message_id for r in rows]
        msgs = {
            m.id: m
            for m in (
                await self.db.execute(select(Message).where(Message.id.in_(msg_ids)))
            ).scalars().all()
        }
        session_ids = {m.session_id for m in msgs.values() if m.session_id}
        sessions = {
            s.id: s
            for s in (
                await self.db.execute(
                    select(ChatSession).where(ChatSession.id.in_(session_ids))
                )
            ).scalars().all()
        } if session_ids else {}

        agent_ids = {r.agent_id for r in rows if r.agent_id}
        agent_ids |= {s.agent_id for s in sessions.values() if s.agent_id}
        agents = {
            a.id: a
            for a in (
                await self.db.execute(select(Agent).where(Agent.id.in_(agent_ids)))
            ).scalars().all()
        } if agent_ids else {}

        out: list[FeedbackListOut] = []
        for r in rows:
            msg = msgs.get(r.message_id)
            session = sessions.get(msg.session_id) if msg else None
            agent_id = r.agent_id or (session.agent_id if session else None)
            agent_version = r.agent_version
            if agent_version is None and session is not None and session.agent_id:
                agent_version = (
                    await self.db.execute(
                        select(Agent.version).where(Agent.id == session.agent_id)
                    )
                ).scalar()
            out.append(
                FeedbackListOut(
                    id=str(r.id),
                    message_id=str(r.message_id),
                    rating=r.rating,
                    reason=r.reason,
                    create_time=r.create_time,
                    message_content=msg.content if msg else None,
                    session_id=str(msg.session_id) if msg else None,
                    agent_id=str(agent_id) if agent_id else None,
                    agent_version=int(agent_version) if agent_version is not None else None,
                    agent_name=agents[agent_id].name if agent_id in agents else None,
                )
            )
        return out, total

    async def stats(self, user_id: int) -> dict[str, Any]:
        """统计。**key 是驼峰**（`likeCount`）—— Java 用 `Map.of("likeCount", ...)`。"""
        total = 0
        counts = {RATING_LIKE: 0, RATING_DISLIKE: 0}
        rows = (
            await self.db.execute(
                select(MessageFeedback.rating, func.count())
                .where(MessageFeedback.user_id == user_id)
                .group_by(MessageFeedback.rating)
            )
        ).all()
        for rating, count in rows:
            counts[str(rating)] = int(count)
            total += int(count)
        return {
            "total": total,
            "likeCount": counts.get(RATING_LIKE, 0),
            "dislikeCount": counts.get(RATING_DISLIKE, 0),
        }
