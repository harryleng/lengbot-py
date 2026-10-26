"""Agent 管理：CRUD + 关联绑定 + 版本 + 对话能力。

对照 Java：`AgentServiceImpl`（1018 行）+ `AgentVersionService` 的一部分。
Python 侧能短很多，因为：
- 不需要 Redis 缓存失效（`@CacheEvict` 那一堆）
- 不需要 `ensureAccessible` 的租户逻辑（Python 版单用户 dev 环境）
- 模型调用复用 `services/llm_client.py`，不用 ModelFactory

**但有几条业务规则必须留**，它们都是踩过坑才加的：

1. 关联绑定有**数量上限**（tools≤10 / mcpServers≤5 / subagents≤5 / skills≤10 /
   knowledges≤10）。不限的话，Agent 绑 50 个工具，每轮对话的 tools schema
   就能吃掉几千 token，而模型实际只会用几个。
2. 绑定 ID **统一存字符串**。雪花 ID 19 位，存成 JSON 数字后前端
   `JSON.parse` 会丢精度（同 model-provider 那个坑）。
3. 删除 Agent 要**级联软删版本和会话**。不级联的话，会话列表里点进一个已删
   Agent 的对话，详情接口直接 404，用户以为数据丢了。
4. 设置默认 Agent 要**先清掉其它 Agent 的 is_default**，否则会出现两个默认。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.snowflake import new_id
from app.models.entities import Agent, AgentVersion, ChatSession, Tool
from app.models.entities import McpServer as McpServerRow
from app.schemas.agent import AgentDetailOut
from app.schemas.agent_ext import AgentIn

logger = logging.getLogger(__name__)

# 绑定数量上限（照抄 Java `writeBindingIdsToConfig` 的调用点）
BINDING_LIMITS: dict[str, int] = {
    "tools": 10,
    "mcpServers": 5,
    "subagents": 5,
    "skills": 10,
    "knowledges": 10,
}

# 附件限制（照抄 Java ChatAttachmentConstants）
MAX_ATTACHMENTS_PER_MESSAGE = 3
MAX_IMAGE_BYTES = 1 * 1024 * 1024
MAX_VIDEO_BYTES = 5 * 1024 * 1024
MAX_DOCUMENT_BYTES = 5 * 1024 * 1024
IMAGE_MIMES = ["image/jpeg", "image/png", "image/webp", "image/gif"]
VIDEO_MIMES = ["video/mp4", "video/webm", "video/quicktime"]
DOCUMENT_EXTENSIONS = [
    ".md", ".txt", ".pdf", ".doc", ".docx", ".ppt", ".pptx",
    ".xls", ".xlsx", ".csv", ".html", ".htm",
]


def _parse_ids(raw: Any) -> list[str]:
    """绑定 ID 归一成字符串列表（去重、去空、保序）。

    必须转字符串：库里 `knowledges` 存的已经是 `"2094693967276167170"` 这种，
    但老的 `tools` 可能存的是数字。两种都要能读。
    """
    if raw is None:
        return []
    if not isinstance(raw, (list, tuple)):
        raw = [raw]
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if item is None:
            continue
        text = str(item).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def _parse_json_field(raw: Any, default: Any = None) -> Any:
    if raw is None:
        return default if default is not None else {}
    if isinstance(raw, (dict, list)):
        return raw
    text = str(raw).strip()
    if not text:
        return default if default is not None else {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        logger.warning("Agent 字段 JSON 解析失败，按默认值处理: %s", text[:100])
        return default if default is not None else {}


def _size_label(num_bytes: int) -> str:
    mb = num_bytes / (1024 * 1024)
    return f"{mb:g}MB"


class AgentService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    async def create(self, dto: AgentIn, user_id: int) -> AgentDetailOut:
        await self._ensure_name_free(dto.name, None)
        now = datetime.now()
        row = Agent(
            id=new_id(),
            # user_id 是 NOT NULL（实测插 NULL 直接 IntegrityError），必须从调用方传
            user_id=user_id,
            name=dto.name,
            description=dto.description,
            system_prompt=dto.system_prompt,
            avatar=dto.avatar,
            icon=dto.icon,
            agent_type=dto.agent_type or "chat",
            config=_parse_json_field(dto.config),
            status="draft",
            is_default=False,
            welcome_message=dto.welcome_message,
            recommended_questions=_normalize_questions(dto.recommended_questions),
            version=0,
            create_time=now,
            update_time=now,
            deleted=0,
        )
        self.db.add(row)
        await self.db.commit()
        await self.db.refresh(row)
        logger.info("创建 Agent %s（type=%s）", row.name, row.agent_type)
        return AgentDetailOut.model_validate(row)

    async def update(self, dto: AgentIn) -> AgentDetailOut:
        if not dto.id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="缺少 id")
        row = await self._require(int(dto.id))

        if dto.name and dto.name != row.name:
            await self._ensure_name_free(dto.name, exclude_id=row.id)
            row.name = dto.name
        row.description = dto.description
        row.system_prompt = dto.system_prompt
        row.welcome_message = dto.welcome_message
        if dto.recommended_questions is not None:
            row.recommended_questions = _normalize_questions(dto.recommended_questions)
        if dto.avatar is not None:
            row.avatar = dto.avatar
        if dto.icon is not None:
            row.icon = dto.icon
        if dto.agent_type:
            row.agent_type = dto.agent_type
        if dto.config is not None:
            # **合并**而不是整体替换：前端表单只提交它管理的那些键，
            # 整体替换会把 `knowledges`/`tools` 这些由绑定接口维护的键抹掉。
            # （Java 侧是整体 set，但它每次都会把完整 config 传上来；
            #   前端若只传部分字段，Java 那边就会丢数据 —— 这里更安全。）
            merged = dict(row.config or {})
            merged.update(_parse_json_field(dto.config))
            row.config = merged
        row.update_time = datetime.now()

        await self.db.commit()
        await self.db.refresh(row)
        logger.info("更新 Agent %s", row.name)
        return AgentDetailOut.model_validate(row)

    async def get(self, agent_id: int) -> AgentDetailOut:
        return AgentDetailOut.model_validate(await self._require(agent_id))

    async def delete(self, agent_id: int) -> None:
        """软删 Agent，并级联软删版本与会话。

        会话也一起软删：留下会话但不留 Agent，用户点进历史对话会看到"Agent 不存在"，
        比直接看不到更困惑。
        """
        row = await self._require(agent_id)
        now = datetime.now()
        row.deleted = 1
        row.update_time = now

        versions = (
            (
                await self.db.execute(
                    select(AgentVersion).where(
                        AgentVersion.agent_id == agent_id, AgentVersion.deleted == 0
                    )
                )
            )
            .scalars()
            .all()
        )
        for v in versions:
            v.deleted = 1
            v.update_time = now

        sessions = (
            (
                await self.db.execute(
                    select(ChatSession).where(
                        ChatSession.agent_id == agent_id, ChatSession.deleted == 0
                    )
                )
            )
            .scalars()
            .all()
        )
        for s in sessions:
            s.deleted = 1
            s.update_time = now

        await self.db.commit()
        logger.info(
            "删除 Agent %s（级联 %d 个版本 / %d 个会话）", agent_id, len(versions), len(sessions)
        )

    async def clone(self, agent_id: int, user_id: int) -> AgentDetailOut:
        """深拷贝配置 + 绑定关系。名称加 `副本` 后缀并保证唯一。"""
        src = await self._require(agent_id)
        now = datetime.now()
        name = await self._unique_copy_name(src.name or "Agent")

        row = Agent(
            id=new_id(),
            user_id=user_id,
            name=name,
            description=src.description,
            system_prompt=src.system_prompt,
            avatar=src.avatar,
            icon=src.icon,
            agent_type=src.agent_type,
            # 深拷贝：直接引用同一个 dict 会让改副本时把原件也改了
            config=json.loads(json.dumps(src.config or {}, ensure_ascii=False)),
            status="draft",
            is_default=False,
            welcome_message=src.welcome_message,
            recommended_questions=json.loads(
                json.dumps(src.recommended_questions, ensure_ascii=False)
            )
            if src.recommended_questions is not None
            else None,
            version=0,
            create_time=now,
            update_time=now,
            deleted=0,
        )
        self.db.add(row)
        await self.db.commit()
        await self.db.refresh(row)
        logger.info("克隆 Agent %s → %s", agent_id, row.id)
        return AgentDetailOut.model_validate(row)

    async def set_default(self, agent_id: int) -> None:
        """设为默认：先把其它 Agent 的 is_default 清掉。

        不清的话会同时存在多个默认 Agent，前端"默认"排序和"打开就用哪个"
        的行为就随机了。
        """
        row = await self._require(agent_id)
        # 用 update() 构造器而不是 Agent.__table__.update() —— 后者是 Core 表的
        # 直接操作，不经过 ORM 会话的 identity map，容易和已加载的实体打架
        await self.db.execute(
            update(Agent)
            .where(Agent.deleted == 0, Agent.id != agent_id)
            .values(is_default=False)
        )
        row.is_default = True
        row.update_time = datetime.now()
        await self.db.commit()

    # ------------------------------------------------------------------
    # 关联绑定（5 组，逻辑完全一样）
    # ------------------------------------------------------------------

    async def get_bindings(self, agent_id: int, field: str) -> list[str]:
        agent = await self._require(agent_id)
        config = agent.config or {}
        return _parse_ids(config.get(field)) if isinstance(config, dict) else []

    async def update_bindings(self, agent_id: int, field: str, ids: Any) -> None:
        if field not in BINDING_LIMITS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=f"不支持的绑定类型: {field}"
            )
        values = _parse_ids(ids)
        limit = BINDING_LIMITS[field]
        if len(values) > limit:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"最多绑定 {limit} 个，当前 {len(values)} 个",
            )

        agent = await self._require(agent_id)
        config = dict(agent.config or {}) if isinstance(agent.config, dict) else {}
        config[field] = values  # 统一存字符串列表
        agent.config = config
        agent.update_time = datetime.now()
        await self.db.commit()
        logger.info("更新 Agent %s 的 %s：%d 个", agent_id, field, len(values))

    async def tool_details(self, agent_id: int) -> list[dict[str, Any]]:
        """绑定的工具完整信息（前端在 Agent 编辑页展示已选工具卡片）。"""
        ids = await self.get_bindings(agent_id, "tools")
        if not ids:
            return []
        int_ids = [int(i) for i in ids if i.isdigit()]
        if not int_ids:
            return []
        rows = (
            (
                await self.db.execute(
                    select(Tool).where(Tool.id.in_(int_ids), Tool.deleted == 0)
                )
            )
            .scalars()
            .all()
        )
        by_id = {str(r.id): r for r in rows}
        # 按绑定顺序返回（数据库 IN 查询不保证顺序）
        return [
            {
                "id": str(t.id),
                "name": t.name,
                "displayName": t.display_name,
                "description": t.description,
                "toolType": t.tool_type,
                "icon": t.icon,
                "status": t.status,
            }
            for t in (by_id.get(i) for i in ids)
            if t is not None
        ]

    async def mcp_server_details(self, agent_id: int) -> list[dict[str, Any]]:
        ids = await self.get_bindings(agent_id, "mcpServers")
        if not ids:
            return []
        int_ids = [int(i) for i in ids if i.isdigit()]
        if not int_ids:
            return []
        rows = (
            (
                await self.db.execute(
                    select(McpServerRow).where(
                        McpServerRow.id.in_(int_ids), McpServerRow.deleted == 0
                    )
                )
            )
            .scalars()
            .all()
        )
        by_id = {str(r.id): r for r in rows}
        return [
            {
                "id": str(m.id),
                "name": m.name,
                "description": m.description,
                "transport": m.transport,
                "status": m.status,
                "icon": m.icon,
                "isBuiltin": int(m.is_builtin or 0),
            }
            for m in (by_id.get(i) for i in ids)
            if m is not None
        ]

    # ------------------------------------------------------------------
    # 对话能力 / @ 候选
    # ------------------------------------------------------------------

    async def chat_capabilities(self, agent_id: int) -> dict[str, Any]:
        """前端据此决定"上传/语音/联网"按钮显不显示。

        20 个字段里只有 7 个是配置项，其余全是**派生值**（尺寸标签、MIME 白名单、
        上限个数）。派生值也照给 —— 前端不想自己算，而且前后端算错会不一致。
        """
        agent = await self._require(agent_id)
        config = agent.config or {}
        if not isinstance(config, dict):
            config = {}

        multimodal = bool(config.get("multimodalEnabled"))
        image_ok = bool(config.get("enableImageInput")) or multimodal
        video_ok = bool(config.get("enableVideoInput"))
        # 只要允许上传附件就放开图片：不支持多模态的 Agent 也能上传图片，
        # 交给 OCR 工具兜底识别（Java 注释原文，别"优化"成和 multimodal 绑定）
        allow_upload = image_ok or video_ok or bool(config.get("enableFileRead"))

        return {
            "multimodalEnabled": multimodal,
            "enableImageInput": image_ok,
            "enableVideoInput": video_ok,
            "enableAudioInput": bool(config.get("enableAudioInput")),
            "enableWebSearch": bool(config.get("webSearchEnabled")),
            "enableTts": bool(config.get("enableTts")),
            "enableReasoning": bool(config.get("enableReasoning")),
            "enableFileRead": bool(config.get("enableFileRead")),
            "allowFileUpload": allow_upload,
            "allowDocumentUpload": allow_upload,
            "allowMediaUpload": allow_upload,
            "allowedFileMimeTypes": (IMAGE_MIMES + VIDEO_MIMES) if allow_upload else [],
            "maxImageBytes": MAX_IMAGE_BYTES,
            "maxVideoBytes": MAX_VIDEO_BYTES,
            "maxImageSizeLabel": _size_label(MAX_IMAGE_BYTES),
            "maxVideoSizeLabel": _size_label(MAX_VIDEO_BYTES),
            "maxAttachmentsPerMessage": MAX_ATTACHMENTS_PER_MESSAGE,
            "maxDocumentBytes": MAX_DOCUMENT_BYTES,
            "maxDocumentSizeLabel": _size_label(MAX_DOCUMENT_BYTES),
            "allowedDocumentExtensions": DOCUMENT_EXTENSIONS if allow_upload else [],
        }

    async def mention_options(self, agent_id: int) -> dict[str, Any]:
        """@ 提问的候选资源。

        只返回**该 Agent 实际绑定了**的资源 —— 没绑定的知识库出现在候选里，
        用户 @ 了却检索不到，比不显示更糟。
        """
        agent = await self._require(agent_id)
        config = agent.config or {}
        if not isinstance(config, dict):
            config = {}

        groups: list[dict[str, Any]] = []

        kb_ids = _parse_ids(config.get("knowledges"))
        if kb_ids:
            groups.append(
                {
                    "type": "knowledge",
                    "label": "知识库",
                    "items": [{"id": i, "name": f"知识库 {i}"} for i in kb_ids],
                }
            )
        tool_ids = _parse_ids(config.get("tools"))
        if tool_ids:
            details = await self.tool_details(agent_id)
            groups.append(
                {
                    "type": "tool",
                    "label": "工具",
                    "items": [{"id": d["id"], "name": d.get("displayName") or d["name"]}
                              for d in details],
                }
            )
        return {"agentId": str(agent.id), "agentVersionId": None, "groups": groups}

    # ------------------------------------------------------------------
    # 版本
    # ------------------------------------------------------------------

    async def get_version(self, agent_id: int, version: int) -> dict[str, Any]:
        row = (
            await self.db.execute(
                select(AgentVersion).where(
                    AgentVersion.agent_id == agent_id,
                    AgentVersion.version == version,
                    AgentVersion.deleted == 0,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="该版本不存在"
            )
        return {
            "id": str(row.id),
            "agentId": str(agent_id),
            "version": row.version,
            "status": row.status,
            "description": row.description,
            "config": row.config or {},
            "nodeCount": row.node_count,
            "edgeCount": row.edge_count,
            "publishTime": row.publish_time,
            "createTime": row.create_time,
        }

    async def restore_version(self, agent_id: int, version: int) -> None:
        """把某个已发布版本的 config 恢复到当前编辑态。

        只改 `agent.config`，不动 `status` —— 恢复是"编辑态回滚"，
        不等于重新发布（重新发布要走 publish）。
        """
        agent = await self._require(agent_id)
        detail = await self.get_version(agent_id, version)  # 复用 404 校验
        cfg = detail.get("config") or {}
        # workflow 型存在 config.graph，chat 型存在 config.payload
        new_config = cfg.get("graph") and {"graph": cfg["graph"]} or cfg
        agent.config = json.loads(json.dumps(new_config, ensure_ascii=False))
        agent.update_time = datetime.now()
        await self.db.commit()
        logger.info("Agent %s 恢复到版本 %s", agent_id, version)

    async def delete_version(self, agent_id: int, version: int) -> None:
        row = (
            await self.db.execute(
                select(AgentVersion).where(
                    AgentVersion.agent_id == agent_id,
                    AgentVersion.version == version,
                    AgentVersion.deleted == 0,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="该版本不存在"
            )
        row.deleted = 1
        row.update_time = datetime.now()
        await self.db.commit()

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    async def _require(self, agent_id: int) -> Agent:
        row = (
            await self.db.execute(
                select(Agent).where(Agent.id == agent_id, Agent.deleted == 0)
            )
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent 不存在")
        return row

    async def _ensure_name_free(self, name: str, exclude_id: int | None) -> None:
        stmt = select(func.count()).select_from(Agent).where(
            Agent.name == name, Agent.deleted == 0
        )
        if exclude_id is not None:
            stmt = stmt.where(Agent.id != exclude_id)
        if int((await self.db.execute(stmt)).scalar_one()) > 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=f"Agent 名称 {name} 已存在"
            )

    async def _unique_copy_name(self, base: str) -> str:
        """`原名-副本`，重名就 `-副本2`、`-副本3`…"""
        candidate = f"{base}-副本"
        n = 2
        while True:
            exists = (
                await self.db.execute(
                    select(func.count())
                    .select_from(Agent)
                    .where(Agent.name == candidate, Agent.deleted == 0)
                )
            ).scalar_one()
            if int(exists) == 0:
                return candidate
            candidate = f"{base}-副本{n}"
            n += 1


def _normalize_questions(raw: Any) -> Any:
    """推荐问题归一成列表（最多 3 条，Java 侧有这个限制）。

    三种输入都要能接：
    - `None` → None（表示"不动"，不是"清空"）
    - `'["问题1","问题2"]'` → 解析成列表
    - `'随便一句'`（不是合法 JSON）→ 当成单元素列表
      早期版本这里用 `_parse_json_field(default=None)`，解析失败返回 None，
      用户填的纯文本问题会被静默丢弃。
    """
    if raw is None:
        return None
    if isinstance(raw, list):
        return raw[:3]
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return [text]
        if isinstance(parsed, list):
            return parsed[:3]
        if isinstance(parsed, str):
            return [parsed]
        return None
    return None
