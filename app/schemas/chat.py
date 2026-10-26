from typing import Any

from app.schemas.common import CamelModel


class ChatRequestIn(CamelModel):
    """聊天请求体。字段对齐 Java 的 `ChatRequestDTO`（阶段 1 只用前三个）。

    bigint 类型的 id 一律用 str 接收：前端从 JS 里拿到的就是字符串，
    如果我们声明成 int，Pydantic 会尝试转换 —— 19 位数字虽然 Python 能转，
    但保持 str 更安全，也能接受前端传来的非数字占位。
    """

    message: str
    session_id: str | None = None
    agent_id: str | None = None
    biz_params: dict[str, Any] | None = None
    agent_version_id: str | None = None
    reply_to_message_id: str | None = None
    regenerate: bool | None = None
    delete_assistant_message_id: str | None = None
    edit_message_id: str | None = None
    run_id: str | None = None


class ReconnectIn(CamelModel):
    """断线重连连入参，对齐 Java 的 `ReconnectDTO`。

    lastEventId 是客户端收到的最后一个 eventId；后端会把大于它的帧重放一遍。
    为 null 表示从头发（客户端没记过序号）。
    """

    request_id: str
    last_event_id: int | None = None
