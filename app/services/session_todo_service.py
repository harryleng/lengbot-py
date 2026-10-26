"""会话待办查询（对照 Java `SessionTodoController` / `SessionTodoServiceImpl`，只读 2 个）。

事实源**不是独立表**：todos 是 `write_todos` 工具的执行结果，
落在 `message.tool_events`（JSONB 数组）里。恢复算法：

1. 拉本轮（或全会话）的 assistant 消息，时间正序；
2. 逐条扫 `tool_events`，取 `type=tool_result` 且 `toolName=write_todos`
   且 `result.success=true` 的 `result.todos` —— **迭代覆盖**，
   同一条消息内可能先初稿再细化，只留最后一次成功的快照。

`listByRequest` 参数缺省直接返回空列表（Java 同），不算错误。
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entities import Message

logger = logging.getLogger(__name__)

WRITE_TODOS_TOOL = "write_todos"
ROLE_ASSISTANT = "assistant"


def extract_latest_todos(messages: list[Message]) -> list[dict]:
    """从 assistant 消息列表提取最后一次成功的 write_todos 快照。

    纯函数（无 IO），tool_events_check 的离线用例直接喂这里。
    """
    latest: list[dict] = []
    for message in messages:
        events = message.tool_events
        if not isinstance(events, list):
            continue
        for event in events:
            if not isinstance(event, dict):
                continue
            if event.get("type") != "tool_result":
                continue
            if event.get("toolName") != WRITE_TODOS_TOOL:
                continue
            result = event.get("result")
            if isinstance(result, str):
                import json

                try:
                    result = json.loads(result)
                except (ValueError, TypeError):
                    continue
            if not isinstance(result, dict) or result.get("success") is not True:
                continue
            todos = result.get("todos")
            if not isinstance(todos, list):
                continue
            snapshot = [
                {
                    "id": item.get("id") if isinstance(item, dict) else None,
                    "content": item.get("content") if isinstance(item, dict) else None,
                    "status": (item.get("status") or "pending") if isinstance(item, dict) else "pending",
                }
                for item in todos
                if isinstance(item, dict)
            ]
            latest = snapshot
    return latest


class SessionTodoService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def list_by_request(self, session_id: int, parent_request_id: str | None) -> list[dict]:
        if not parent_request_id or not parent_request_id.strip():
            return []
        stmt = (
            select(Message)
            .where(Message.session_id == session_id)
            .where(Message.role == ROLE_ASSISTANT)
            # ⚠️ Java 参数名叫 parentRequestId，但 SQL 查的是 metadata->>'requestId'
            #（selectAssistantByRequestId 注释原文："按 metadata.requestId 查询本轮助手消息"），
            # 照抄字段名，别"修正"成 parentRequestId
            .where(Message.msg_metadata["requestId"].astext == parent_request_id.strip())
            .order_by(Message.create_time, Message.id)
        )
        rows = (await self.db.execute(stmt)).scalars().all()
        return extract_latest_todos(list(rows))

    async def list_latest_by_session(self, session_id: int) -> list[dict]:
        stmt = (
            select(Message)
            .where(Message.session_id == session_id)
            .where(Message.role == ROLE_ASSISTANT)
            .order_by(Message.create_time)
        )
        rows = (await self.db.execute(stmt)).scalars().all()
        return extract_latest_todos(list(rows))
