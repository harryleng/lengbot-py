"""对话附件（对照 Java `ChatAttachmentServiceImpl`，核心路径）。

Java 的完整链路里文档附件要过 Tika 解析 + 内容安全扫描 + 解析结果缓存；
Python 版保留**上传面**的核心约束（Agent 能力开关、大小上限、类型分类、
MinIO 存储、归属校验），文档解析只对 md/txt/csv 直读文本 ——
其他格式不给解析文本但**不阻断上传**（Java 同款：附 warning，文件仍可引用）。

路径规则（SessionStoragePath）：

- 带会话：`sessions/{sessionId}/inputs/{attachmentId}_{safeName}`（会话级，可被文件树看到）
- 未带会话：`chat/{agentId}/temp/{attachmentId}_{safeName}`（缓冲区，发送后归档）

两个路径前缀同时是 **delete 的归属校验依据**：
`sessions/` 开头校验会话归属，`chat/` 开头校验 agentId 一致，其他前缀一律拒删。
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entities import Agent
from app.services import minio_client

logger = logging.getLogger(__name__)

MAX_IMAGE_BYTES = 1 * 1024 * 1024
MAX_VIDEO_BYTES = 5 * 1024 * 1024
MAX_DOCUMENT_BYTES = 5 * 1024 * 1024
MAX_PARSED_TEXT_CHARS = 32_000

IMAGE_MIMES = {"image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp"}
VIDEO_MIMES = {"video/mp4", "video/webm", "video/quicktime"}
TEXT_EXTS = {"md", "txt", "csv", "json", "html"}


def _classify(mime: str, ext: str) -> str:
    if mime in IMAGE_MIMES:
        return "image"
    if mime in VIDEO_MIMES:
        return "video"
    return "document"


def _limit_for(type_name: str) -> int:
    return {
        "image": MAX_IMAGE_BYTES,
        "video": MAX_VIDEO_BYTES,
        "document": MAX_DOCUMENT_BYTES,
    }[type_name]


def _safe_name(name: str) -> str:
    """只保留文件名部分，剥掉路径分隔符（防 objectKey 注入）。"""
    name = (name or "file").replace("\\", "/").split("/")[-1]
    return name or "file"


class ChatAttachmentService:
    def __init__(self, db: AsyncSession, user_id: int) -> None:
        self.db = db
        self.user_id = user_id

    async def upload(
        self,
        agent_id: int,
        session_id: int | None,
        file_name: str,
        mime: str,
        data: bytes,
    ) -> dict:
        agent = await self.db.get(Agent, agent_id)
        if agent is None or agent.deleted:
            raise LookupError("Agent 不存在")

        # Agent 能力开关（allowFileUpload），配置在 agent.config
        config = agent.config if isinstance(agent.config, dict) else {}
        if config.get("allowFileUpload") is not True:
            raise PermissionError("当前 Agent 未开启附件上传")

        mime = mime or "application/octet-stream"
        type_name = _classify(mime, "")
        limit = _limit_for(type_name)
        if len(data) > limit:
            raise ValueError(f"{type_name} 不能超过 {limit // 1024 // 1024}MB")
        if not data:
            raise ValueError("请选择文件")

        attachment_id = uuid.uuid4().hex
        safe = _safe_name(file_name)
        if session_id:
            object_key = f"sessions/{session_id}/inputs/{attachment_id}_{safe}"
        else:
            object_key = f"chat/{agent_id}/temp/{attachment_id}_{safe}"

        await minio_client.upload_bytes(data, object_key, mime)

        warning = None
        parsed_text = None
        ext = safe.rsplit(".", 1)[-1].lower() if "." in safe else ""
        if type_name == "document":
            if ext in TEXT_EXTS:
                try:
                    parsed_text = (await minio_client.download_text(object_key)) or ""
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[ChatAttachment] 解析文本失败: %s", exc)
            if not parsed_text:
                # 不阻断上传（Java 同款）：附 warning，文件仍可作为附件引用
                warning = (
                    "未能从该文件中提取文本，可能是加密文档或扫描件；"
                    "文件已上传，但对话中无法引用其文本内容"
                )

        out = {
            "id": attachment_id,
            "type": type_name,
            "mimeType": mime,
            "objectKey": object_key,
            "fileName": file_name,
            "previewUrl": None,
            "warning": warning,
        }
        if parsed_text:
            out["parsedText"] = parsed_text[:MAX_PARSED_TEXT_CHARS]
        try:
            out["previewUrl"] = await minio_client.presigned_url(object_key, mime)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[ChatAttachment] 生成预览 URL 失败: %s", exc)
        return out

    async def delete(self, agent_id: int, session_id: int | None, attachment: dict) -> None:
        object_key = (attachment or {}).get("objectKey") or ""
        if not object_key.strip():
            raise ValueError("无效的附件")
        # 归属校验：路径前缀即信任边界（Java validateAttachmentOwnership 同款）
        if object_key.startswith("sessions/"):
            sid = int(object_key.split("/")[1])
            if session_id is not None and sid != session_id:
                raise ValueError("附件与会话不匹配")
        elif object_key.startswith("chat/"):
            # chat/{agentId}/temp/{id}_{name} —— agentId 是第 2 段
            parts = object_key.split("/")
            try:
                aid = int(parts[1])
            except (IndexError, ValueError) as exc:
                raise ValueError("无效的附件路径") from exc
            if agent_id is None or aid != agent_id:
                raise ValueError("附件与 Agent 不匹配")
        else:
            raise ValueError("不允许删除该附件")
        await minio_client.delete(object_key)

    async def refresh_previews(self, attachments: list[dict]) -> list[dict]:
        out: list[dict] = []
        for att in attachments or []:
            object_key = (att or {}).get("objectKey") or ""
            row = dict(att)
            if object_key:
                try:
                    row["previewUrl"] = await minio_client.presigned_url(
                        object_key, att.get("mimeType")
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[ChatAttachment] 刷新预览失败: %s", exc)
            out.append(row)
        return out
