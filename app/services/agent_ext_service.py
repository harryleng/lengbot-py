"""Agent 扩展操作：AI 生成提示词/推荐问题、头像、发布。

对照 Java `AgentServiceImpl.generateSystemPrompt / generateRecommendedQuestions /
uploadAvatar` + `AgentVersionServiceImpl.publishChatAgent`。

要点：

- 两个 generate 的 LLM 调用**跟随 Agent 配置**（providerId/modelId 与主对话同源，
  模型名才匹配供应商）；没配就回落系统默认（`call_llm` 内部处理）。
- questions 的 JSON 校验失败会抛错 —— 前端拿到的是"生成失败"而不是坏数据。
- publish 快照链路：`agent_version` 插一行 PUBLISHED（version+1、config 快照），
  agent 本体 version/status/publish_time 同步推进。
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.snowflake import new_id
from app.models.entities import Agent, AgentVersion
from app.services import minio_client
from app.services.rag_eval_engine import call_llm

logger = logging.getLogger(__name__)

GENERATE_PROMPT_SYSTEM = (
    """你是一个AI助手提示词生成专家。根据用户提供的Agent名称和描述，
生成一段专业的系统提示词。
要求：
- 提示词应定义Agent的角色、能力范围和行为规范
- 语言简洁专业，不超过500字
- 直接输出提示词内容，不要加任何前缀或解释
"""
)

GENERATE_QUESTIONS_SYSTEM = """你是一个推荐问题生成助手。根据用户提供的Agent名称和描述，生成3个推荐问题。
严格规则：
- 只输出JSON数组，不要任何其他文字
- 恰好3个问题，每个问题不超过30个字
- 问题应与Agent的功能和领域相关
- 格式：["问题1","问题2","问题3"]
"""

ALLOWED_AVATAR_EXTS = {"jpg", "jpeg", "png", "gif", "webp", "bmp"}


def _extract_json(text_value: str | None) -> str | None:
    from app.services.rag_eval_engine import extract_json

    return extract_json(text_value)


class AgentExtService:
    def __init__(self, db: AsyncSession, user_id: int) -> None:
        self.db = db
        self.user_id = user_id

    async def _owned(self, agent_id: int) -> Agent:
        row = await self.db.get(Agent, agent_id)
        if row is None or row.deleted:
            raise LookupError("Agent 不存在")
        if row.user_id != self.user_id:
            # 归属不符与不存在同响应，防 ID 枚举
            raise LookupError("Agent 不存在")
        return row

    @staticmethod
    def _model_from(agent: Agent) -> tuple[int | None, str | None]:
        config = agent.config if isinstance(agent.config, dict) else {}
        provider_id = config.get("providerId")
        model_id = config.get("modelId")
        return (
            int(provider_id) if provider_id else None,
            str(model_id) if model_id else None,
        )

    async def generate_system_prompt(self, agent_id: int) -> str:
        agent = await self._owned(agent_id)
        user_msg = (
            f"Agent名称：{agent.name}\nAgent描述：{agent.description or '暂无描述'}"
        )
        provider_id, model_id = self._model_from(agent)
        result = await call_llm(
            provider_id, model_id,
            GENERATE_PROMPT_SYSTEM + "\n\n" + user_msg,
        )
        if not result or not result.strip():
            raise RuntimeError("AI 生成失败")
        return result.strip()

    async def generate_recommended_questions(self, agent_id: int) -> str:
        agent = await self._owned(agent_id)
        user_msg = (
            f"Agent名称：{agent.name}\nAgent描述：{agent.description or '暂无描述'}"
        )
        provider_id, model_id = self._model_from(agent)
        raw = await call_llm(
            provider_id, model_id,
            GENERATE_QUESTIONS_SYSTEM + "\n\n" + user_msg,
        )
        if not raw or not raw.strip():
            raise RuntimeError("AI 生成失败")
        cleaned = (_extract_json(raw) or "").strip()
        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            raise ValueError("AI 返回的推荐问题格式异常") from exc
        # 校验通过后原样返回 JSON 字符串（前端 parse）
        if not isinstance(parsed, list) or len(parsed) != 3:
            raise ValueError("AI 返回的推荐问题数量异常")
        return cleaned

    async def upload_avatar(self, agent_id: int, filename: str, data: bytes) -> str:
        agent = await self._owned(agent_id)
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if ext not in ALLOWED_AVATAR_EXTS:
            raise ValueError("支持格式: jpg/jpeg/png/gif/webp/bmp")
        if not data:
            raise ValueError("请选择文件")

        path = f"agent/{agent_id}/avatar/{uuid.uuid4().hex}.{ext}"
        # 先删旧头像（路径里带 /avatar/ 才是头像对象，避免误删业务文件）
        if agent.avatar and "/avatar/" in agent.avatar:
            old_key = agent.avatar.split("/lengbot/", 1)[-1]
            await minio_client.delete(old_key)
        await minio_client.upload_bytes(data, path, f"image/{'jpeg' if ext == 'jpg' else ext}")
        url = minio_client.public_url(path)
        agent.avatar = url
        agent.update_time = datetime.now()
        await self.db.commit()
        return url

    async def publish(self, agent_id: int, description: str | None) -> dict:
        agent = await self._owned(agent_id)
        snapshot = dict(agent.config or {})

        next_version = (agent.version or 0) + 1
        now = datetime.now()
        self.db.add(
            AgentVersion(
                id=new_id(),
                agent_id=agent_id,
                user_id=agent.user_id,
                version=next_version,
                status="published",
                config={"kind": "chat", "payload": snapshot},
                # ⚠️ 这三列在表里 NOT NULL DEFAULT 0，但 ORM 实体没配 default，
                # 不显式传就是 NotNullViolation（chat 型快照 node/edge 恒 0）
                node_count=0,
                edge_count=0,
                publish_time=now,
                description=description,
                create_time=now,
                update_time=now,
                deleted=0,
            )
        )
        agent.version = next_version
        agent.status = "published"
        agent.publish_time = now
        agent.update_time = now
        await self.db.commit()

        logger.info("[Agent] 发布成功 agentId=%s version=%s", agent_id, next_version)
        return {"version": next_version, "status": "published"}