"""会话文件树（对照 Java `SessionFileServiceImpl`，4 个接口）。

路径规范（`sessions/{sessionId}/` 下）：

    inputs/（用户上传，parsed/ 放解析产物）
    outputs/（AI 产出：images/、files/）
    workspace/（Agent 沙盒工作区）
    uploads/（历史上传，只读兼容）

三条硬规则：

1. **分区白名单**：路径必须落在上述四个分区下，`foo/bar` 这种直接 400 ——
   会话文件接口是把 objectKey 拼进 `sessions/{sessionId}/` 的，
   不做分区校验等于拿路径穿越扫整个 bucket。
2. **`..` 一票否决**（normalize 阶段就拒）。
3. **顶级分区目录不可删**（inputs/ outputs/ … 是结构，不是文件）；
   根路径固定返回三个目录（即使空也展示，与 Yuxi 对齐）。

MinIO 没有目录，"单层子条目"靠递归列举后从 key 中间段推导：
`inputs/parsed/a.md` 在列 `inputs/` 时表现为目录 `parsed/`。
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entities import ChatSession
from app.services import minio_client

logger = logging.getLogger(__name__)

INPUTS_DIR = "inputs"
OUTPUTS_DIR = "outputs"
WORKSPACE_DIR = "workspace"
UPLOADS_DIR = "uploads"
PARTITIONS = (INPUTS_DIR, OUTPUTS_DIR, WORKSPACE_DIR, UPLOADS_DIR)

MAX_TEXT_PREVIEW_BYTES = 512 * 1024

_TEXT_EXTS = {"txt", "md", "csv", "json", "html", "xml", "log", "py", "js", "java", "yml", "yaml"}
_IMAGE_EXTS = {"png", "jpg", "jpeg", "gif", "webp", "bmp", "svg"}
_VIDEO_EXTS = {"mp4", "webm", "mov"}


def normalize_relative_path(path: str | None) -> str:
    """剥首尾斜杠、反斜杠转正斜杠；`..` 一票否决。"""
    p = (path or "").strip().replace("\\", "/")
    while p.startswith("/"):
        p = p[1:]
    while p.endswith("/"):
        p = p[:-1]
    if ".." in p:
        raise ValueError("非法路径")
    return p


def ensure_allowed_partition(normalized: str) -> None:
    if not normalized:
        return
    for part in PARTITIONS:
        if normalized == part or normalized.startswith(part + "/"):
            return
    raise ValueError("路径不在允许的会话分区下")


def guess_mime(name: str) -> str:
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    table = {
        "txt": "text/plain", "md": "text/markdown", "csv": "text/csv",
        "json": "application/json", "html": "text/html", "xml": "application/xml",
        "pdf": "application/pdf",
        "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
        "gif": "image/gif", "webp": "image/webp", "bmp": "image/bmp",
        "mp4": "video/mp4", "webm": "video/webm", "mov": "video/quicktime",
    }
    return table.get(ext, "application/octet-stream")


def detect_preview_type(mime: str, name: str) -> str:
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if mime.startswith("text/") or ext in _TEXT_EXTS or mime == "application/json":
        return "markdown" if ext == "md" else "text"
    if mime.startswith("image/") or ext in _IMAGE_EXTS:
        return "image"
    if mime == "application/pdf" or ext == "pdf":
        return "pdf"
    if mime.startswith("video/") or ext in _VIDEO_EXTS:
        return "video"
    return "download"


class SessionFileService:
    def __init__(self, db: AsyncSession, user_id: int) -> None:
        self.db = db
        self.user_id = user_id

    async def _owned_session(self, session_id: int) -> ChatSession:
        row = await self.db.get(ChatSession, session_id)
        if row is None or row.deleted or row.user_id != self.user_id:
            raise LookupError("会话不存在")
        return row

    @staticmethod
    def _attachments(session: ChatSession) -> list[dict]:
        """chat_session.attachments 是 jsonb；兼容数组 / {"items":[...]} 两种历史形态。"""
        raw = session.attachments
        if isinstance(raw, dict) and isinstance(raw.get("items"), list):
            return [x for x in raw["items"] if isinstance(x, dict)]
        if isinstance(raw, list):
            return [x for x in raw if isinstance(x, dict)]
        return []

    async def list_directory(self, session_id: int, path: str) -> dict:
        session = await self._owned_session(session_id)
        normalized = normalize_relative_path(path)

        if not normalized:
            # 根路径：三个固定目录（即使空也展示，与 Yuxi 对齐）+ uploads 兼容
            entries = [
                {"name": d, "path": d, "directory": True, "size": None,
                 "mimeType": None, "modifiedAt": None, "source": None,
                 "objectKey": None, "previewUrl": None, "fileName": None}
                for d in (INPUTS_DIR, OUTPUTS_DIR, WORKSPACE_DIR)
            ]
            has_uploads = await minio_client.exists(
                f"sessions/{session_id}/{UPLOADS_DIR}/"
            )
            if has_uploads:
                entries.append(
                    {"name": UPLOADS_DIR, "path": UPLOADS_DIR, "directory": True,
                     "size": None, "mimeType": None, "modifiedAt": None, "source": None,
                     "objectKey": None, "previewUrl": None, "fileName": None}
                )
            return {"entries": entries, "stats": self._stats(session)}

        ensure_allowed_partition(normalized)
        prefix = f"sessions/{session_id}/{normalized}/"
        object_keys = await minio_client.list_objects(prefix)

        # 附件索引：objectKey → 附件记录（enrich 用）
        att_index = {
            a.get("objectKey"): a for a in self._attachments(session) if a.get("objectKey")
        }

        # 从完整 key 推导单层子条目
        dirs: dict[str, dict] = {}
        files: list[dict] = []
        for key in object_keys:
            rel = key[len(f"sessions/{session_id}/"):]
            name = rel[len(normalized) + 1:] if rel.startswith(normalized + "/") else rel
            if not name or name == ".keep":
                continue
            if "/" in name:
                dir_name = name.split("/")[0]
                if dir_name not in dirs:
                    dirs[dir_name] = self._entry(dir_name, f"{normalized}/{dir_name}", True)
                continue
            files.append(self._entry(name, f"{normalized}/{name}", False, key, att_index))

        entries = list(dirs.values()) + files
        # 目录优先，名称字典序（大小写不敏感）
        entries.sort(key=lambda e: (not e["directory"], (e["name"] or "").lower()))
        return {"entries": entries, "stats": self._stats(session)}

    @staticmethod
    def _entry(name: str, path: str, directory: bool,
               object_key: str | None = None,
               att_index: dict[str, dict] | None = None) -> dict:
        att = (att_index or {}).get(object_key)
        return {
            "name": name,
            "path": path,
            "directory": directory,
            "size": None,
            "mimeType": guess_mime(name) if not directory else None,
            "modifiedAt": None,
            "source": (att.get("source") if att else None),
            "objectKey": object_key,
            "previewUrl": None,
            "fileName": (att.get("fileName") if att else None),
        }

    @staticmethod
    def _stats(session: ChatSession) -> dict:
        attachments = SessionFileService._attachments(session)
        user = sum(
            1 for a in attachments
            if a.get("source") == "user_upload" or a.get("source") == "user"
        )
        ai = sum(
            1 for a in attachments
            if a.get("source") and str(a.get("source")).startswith("ai_")
        )
        return {"total": len(attachments), "userUpload": user, "aiGenerated": ai}

    async def read_content(self, session_id: int, path: str) -> dict:
        await self._owned_session(session_id)
        normalized = normalize_relative_path(path)
        ensure_allowed_partition(normalized)
        object_key = f"sessions/{session_id}/{normalized}"

        vo: dict = {
            "path": normalized,
            "objectKey": object_key,
            "mimeType": None,
            "size": None,
            "previewType": "unsupported",
            "supported": False,
            "content": None,
            "previewUrl": None,
            "message": None,
        }

        if not await minio_client.exists(object_key):
            vo["message"] = "文件不存在或已被删除"
            return vo

        name = normalized.rsplit("/", 1)[-1]
        mime = guess_mime(name)
        preview_type = detect_preview_type(mime, name)
        vo["mimeType"] = mime
        vo["previewType"] = preview_type

        if preview_type in ("text", "markdown"):
            data = await minio_client.download_bytes(object_key)
            vo["size"] = len(data)
            if len(data) > MAX_TEXT_PREVIEW_BYTES:
                vo["supported"] = True
                vo["message"] = "文件过大，建议下载后查看"
                vo["previewUrl"] = await minio_client.presigned_url(object_key, mime)
                return vo
            vo["supported"] = True
            vo["content"] = data.decode("utf-8", errors="replace")
            return vo

        # image / pdf / video / 其他二进制：全部走预签名 URL
        vo["supported"] = True
        vo["previewUrl"] = await minio_client.presigned_url(object_key, mime)
        return vo

    async def get_download_url(self, session_id: int, path: str) -> str:
        await self._owned_session(session_id)
        normalized = normalize_relative_path(path)
        ensure_allowed_partition(normalized)
        object_key = f"sessions/{session_id}/{normalized}"
        name = normalized.rsplit("/", 1)[-1]
        return await minio_client.presigned_url(object_key, guess_mime(name))

    async def delete_file(self, session_id: int, path: str) -> None:
        await self._owned_session(session_id)
        normalized = normalize_relative_path(path)
        ensure_allowed_partition(normalized)
        # 顶级分区是结构不是文件（Java 同款："不允许删除顶级目录"）
        if normalized in PARTITIONS:
            raise ValueError("不允许删除顶级目录")
        object_key = f"sessions/{session_id}/{normalized}"
        await minio_client.delete(object_key)

        # 同步从 attachments 索引里摘掉（Java deleteFile 尾部同款）
        session = await self._owned_session(session_id)
        items = self._attachments(session)
        remaining = [a for a in items if a.get("objectKey") != object_key]
        if len(remaining) != len(items):
            session.attachments = remaining  # JSONB 整体重新赋值触发脏检查
            session.update_time = datetime.now()
            await self.db.commit()
