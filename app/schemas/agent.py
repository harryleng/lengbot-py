from datetime import datetime
from typing import Any

from pydantic import field_validator

from app.schemas.common import CamelModel


class AgentOut(CamelModel):
    """Agent 列表项 / 详情。

    字段取自前端实际消费点，不是照抄整张表：
      - `components/AgentSelect.vue` 读 id / name / agentType / avatar
      - `composables/useChatAgents.js` 额外读 welcomeMessage / recommendedQuestions
      - `isDefault` 决定新对话默认选哪个 Agent

    和 ChatSessionOut 同一条铁律：**bigint 主键必须序列化成字符串**，
    否则 19 位 snowflake 在 JS 里精度丢失，后续所有按 id 的请求全 404。
    """

    id: str
    user_id: str | None = None
    name: str | None = None
    description: str | None = None
    avatar: str | None = None
    # 前端会读 a.agentType?.code || a.agentType，所以这里给纯字符串即可
    agent_type: str | None = None
    status: str | None = None
    is_default: bool = False
    welcome_message: str | None = None
    recommended_questions: Any | None = None
    create_time: datetime | None = None
    update_time: datetime | None = None

    @field_validator("id", "user_id", mode="before")
    @classmethod
    def _bigint_to_str(cls, v: int | str | None) -> str | None:
        return None if v is None else str(v)


class AgentDetailOut(CamelModel):
    """详情比列表多出 systemPrompt / config。

    前端 `loadCurrentAgent` 读的是 `res.data.agent`，所以接口外层还要再包一层
    `{"agent": {...}}` —— 这个结构不能省。
    """

    id: str
    name: str | None = None
    description: str | None = None
    avatar: str | None = None
    agent_type: str | None = None
    status: str | None = None
    is_default: bool = False
    system_prompt: str | None = None
    welcome_message: str | None = None
    recommended_questions: Any | None = None
    config: dict[str, Any] | None = None
    create_time: datetime | None = None
    update_time: datetime | None = None

    @field_validator("id", mode="before")
    @classmethod
    def _bigint_to_str(cls, v: int | str | None) -> str | None:
        return None if v is None else str(v)
