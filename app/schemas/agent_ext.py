"""Agent 管理的**写入** schema。

对照 Java `AgentSaveDTO`。响应侧复用 `schemas/agent.py` 的
`AgentOut` / `AgentDetailOut`（已经过聊天页验证，不要重造）。

`config` / `recommendedQuestions` 入参是 **JSON 字符串**（前端 `JSON.stringify` 后提交），
出参 `config` 是**对象** —— 和 model-provider/tool 相反，别照抄那边。

依据：`GET /api/agents/{id}/detail` 前端读的是 `res.data.agent.config.topP` 这种
直接取属性的写法，说明 config 必须是对象；而保存时是 `config: JSON.stringify(form.config)`。
"""

from __future__ import annotations

from typing import Any

from pydantic import Field, field_validator

from app.schemas.common import CamelModel


class AgentIn(CamelModel):
    """创建 / 更新入参（对齐 Java `AgentSaveDTO`）。

    长度限制照抄 Java 的 `@Size`：名称 50、描述 500、系统提示词 2000、欢迎语 200。
    前端也有这些限制，但服务端必须再校验一次 —— 直接发请求就绕过了。
    """

    id: str | None = None
    name: str = Field(..., max_length=50)
    description: str | None = Field(None, max_length=500)
    system_prompt: str | None = Field(None, max_length=2000)
    welcome_message: str | None = Field(None, max_length=200)
    # 前端传 JSON 字符串（可能是数组或对象）
    recommended_questions: str | None = None
    avatar: str | None = None
    icon: str | None = None
    agent_type: str | None = None
    # 前端传 JSON 字符串
    config: str | None = None

    @field_validator("id", mode="before")
    @classmethod
    def _id_to_str(cls, v: Any) -> str | None:
        return None if v is None else str(v)
