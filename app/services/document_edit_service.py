"""文档在线编辑（对照 Java `DocumentEditServiceImpl` + `DocumentVersionServiceImpl`）。

## 编辑面（MD/TXT/CSV 三种）

`EDIT_MODE_MAP`：md/txt/csv → editor，其余 unsupported —— PDF 这些是"解析产物"，
在线改了原文也进不了解析链路，所以不给编辑。

## 保存的五步（顺序敏感）

1. 状态校验：只有 completed/uploaded 能编辑（pending/processing 正在入库，
   这时候改文件会让入库读到半成品）；
2. **乐观锁**：`expectedHash` 与库里 `file_hash` 不一致 → 60014 冲突
   （两个人同时开编辑器，后保存的会覆盖先保存的，乐观锁是唯一防线）；
3. editMode 必须是 `editor`（前端篡改这个字段 = 想编辑不支持的类型，直接拒）；
4. **先快照当前版本再覆盖** —— 快照存 MinIO `doc-versions/{docId}/v{N}.txt`
   + `document_version` 一行；覆盖后 hash/version/lastEditTime 更新；
5. 异步触发全量重建（清分块 → 解析 → 向量化），保存成功不等于重建成功。

## 哈希口径

`calculateContentHash` 是 **MD5**（Java `DigestUtils.md5Hex`），不是 SHA ——
前端会把 editable-content 返回的 fileHash 原样放进 expectedHash 回传，
两边必须同算法。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import SessionLocal
from app.core.snowflake import new_id
from app.models.entities import Document, DocumentVersion
from app.services import minio_client
from app.services.knowledge_service import KnowledgeService

logger = logging.getLogger(__name__)

EDIT_MODE_MAP = {"md": "editor", "txt": "editor", "csv": "editor"}

# Java DOCUMENT_EDIT_CONFLICT=60014（HTTP 409）/ DOCUMENT_EDIT_UNSUPPORTED=60015
ERR_CONFLICT = 60014
ERR_UNSUPPORTED = 60015


def content_hash(content: str) -> str:
    return hashlib.md5(content.encode("utf-8")).hexdigest()


class DocumentEditService:
    def __init__(self, db: AsyncSession, user_id: int) -> None:
        self.db = db
        self.user_id = user_id
        self._kb = KnowledgeService(db, user_id)

    async def _get_doc(self, document_id: int) -> Document:
        doc = await self.db.get(Document, document_id)
        if doc is None or doc.deleted:
            raise LookupError("文档不存在")
        return doc

    @staticmethod
    def _target_path(doc: Document) -> str:
        # 优先覆盖已解析的 Markdown；没有则覆盖原始文件
        return doc.markdown_path if doc.markdown_path else doc.file_path

    async def get_editable_content(self, document_id: int) -> dict:
        doc = await self._get_doc(document_id)
        await self._kb.ensure_member(doc.knowledge_id)
        file_type = (doc.file_type or "").lower()
        edit_mode = EDIT_MODE_MAP.get(file_type, "unsupported")
        editable = file_type in EDIT_MODE_MAP

        content = ""
        if editable:
            content = await self._read_content(doc)

        return {
            "documentId": str(doc.id),
            "fileName": doc.name,
            "fileType": file_type,
            "editMode": edit_mode,
            "content": content,
            "fileHash": doc.file_hash,
            "editable": editable,
            "totalChars": len(content),
            "totalChunks": doc.chunk_count or 0,
        }

    async def _read_content(self, doc: Document) -> str:
        # 优先读已解析的 Markdown，读不到回退原文件（Java 同款两级回退）
        if doc.markdown_path:
            try:
                return (await minio_client.download_text(doc.markdown_path)) or ""
            except Exception as exc:  # noqa: BLE001
                logger.warning("[DocEdit] 读 markdown 失败 doc=%s，回退原文件: %s", doc.id, exc)
        try:
            return (await minio_client.download_text(doc.file_path)) or ""
        except Exception as exc:  # noqa: BLE001
            raise LookupError(f"读取文档内容失败: {exc}") from exc

    async def save_content(
        self, document_id: int, content: str, edit_mode: str, expected_hash: str | None
    ) -> dict:
        doc = await self._get_doc(document_id)
        await self._kb.ensure_role(doc.knowledge_id, "developer")

        if doc.status not in ("completed", "uploaded"):
            raise ValueError("文档状态不允许此操作")

        # 乐观锁：expectedHash 非空才校验（前端第一次保存可不传）
        if expected_hash and expected_hash != doc.file_hash:
            conflict = PermissionError("文档已被其他人修改，请刷新后重试")
            conflict.code = ERR_CONFLICT  # type: ignore[attr-defined]
            raise conflict

        file_type = (doc.file_type or "").lower()
        if EDIT_MODE_MAP.get(file_type) != edit_mode:
            bad = ValueError("该文件类型暂不支持在线编辑")
            bad.code = ERR_UNSUPPORTED  # type: ignore[attr-defined]
            raise bad

        # 4) 先快照当前版本（覆盖前备份）
        current_content = await self._read_content(doc)
        current_version = doc.version or 1
        await self._save_version(document_id, current_version, doc.file_hash, current_content)

        # 5) 覆盖内容
        target = self._target_path(doc)
        await minio_client.upload_string(content, target, "text/plain")

        # 6) 更新元数据
        new_hash = content_hash(content)
        doc.file_hash = new_hash
        doc.version = (doc.version or 1) + 1
        doc.last_edit_time = datetime.now()
        doc.update_time = datetime.now()
        await self.db.commit()

        # 7) 异步全量重建（独立 session；保存成功 ≠ 重建成功）
        asyncio.create_task(_rebuild(document_id))

        return {
            "documentId": str(document_id),
            "newHash": new_hash,
            "message": "保存成功，正在重新处理文档...",
        }

    async def _save_version(
        self, document_id: int, version: int, content_hash_value: str | None, content: str
    ) -> None:
        storage_path = f"doc-versions/{document_id}/v{version}.txt"
        await minio_client.upload_string(content, storage_path, "text/plain")
        now = datetime.now()
        self.db.add(
            DocumentVersion(
                id=new_id(),
                document_id=document_id,
                version=version,
                content_hash=content_hash_value,
                storage_path=storage_path,
                created_by=self.user_id,
                create_time=now,
            )
        )
        await self.db.commit()


async def _rebuild(document_id: int) -> None:
    """异步重建：独立 session 跑完整 ingest 管道。失败只 log（文档已保存）。"""
    try:
        from app.schemas.knowledge_doc import IngestIn
        from app.services.document_service import DocumentService

        async with SessionLocal() as db:
            doc = await db.get(Document, document_id)
            embedding_json = dict(doc.embedding_json) if doc and doc.embedding_json else {}
            dto = IngestIn(**{
                "chunkStrategy": embedding_json.get("chunkStrategy", "general"),
                "chunkSize": embedding_json.get("chunkSize", 512),
                "chunkOverlap": embedding_json.get("chunkOverlap", 10),
                "ocrEnabled": bool(embedding_json.get("ocrEnabled", False)),
            })
            await DocumentService(db, 0)._ingest_pipeline(doc, dto)
        logger.info("[DocEdit] 重建完成 doc=%s", document_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[DocEdit] 触发重建失败（文档已保存）doc=%s: %s", document_id, exc)


class DocumentVersionService:
    def __init__(self, db: AsyncSession, user_id: int) -> None:
        self.db = db
        self.user_id = user_id
        self._kb = KnowledgeService(db, user_id)

    async def list_versions(self, document_id: int) -> list[dict]:
        doc = await self.db.get(Document, document_id)
        if doc is None or doc.deleted:
            raise LookupError("文档不存在")
        await self._kb.ensure_member(doc.knowledge_id)
        stmt = (
            select(DocumentVersion)
            .where(DocumentVersion.document_id == document_id)
            .order_by(DocumentVersion.version.desc())
        )
        rows = (await self.db.execute(stmt)).scalars().all()
        return [
            {
                "id": str(v.id),
                "version": v.version,
                "contentHash": v.content_hash,
                "createTime": v.create_time,
            }
            for v in rows
        ]

    async def get_content(self, document_id: int, version_id: int) -> str:
        version = await self.db.get(DocumentVersion, version_id)
        if version is None or version.document_id != document_id:
            raise LookupError("版本不存在")
        doc = await self.db.get(Document, version.document_id)
        if doc is None:
            raise LookupError("文档不存在")
        await self._kb.ensure_member(doc.knowledge_id)
        try:
            return await minio_client.download_text(version.storage_path)
        except Exception as exc:  # noqa: BLE001
            raise LookupError(f"读取版本内容失败: {exc}") from exc

    async def rollback(self, document_id: int, version_id: int) -> None:
        version = await self.db.get(DocumentVersion, version_id)
        if version is None or version.document_id != document_id:
            raise LookupError("版本不存在")
        doc = await self.db.get(Document, document_id)
        if doc is None or doc.deleted:
            raise LookupError("文档不存在")
        await self._kb.ensure_role(doc.knowledge_id, "developer")

        content = await minio_client.download_text(version.storage_path)

        # 4) 回滚前先备份当前内容
        current = await DocumentEditService(self.db, self.user_id)._read_content(doc)
        next_version = (doc.version or 1) + 1
        await DocumentEditService(self.db, self.user_id)._save_version(
            document_id, doc.version or 1, doc.file_hash, current
        )

        # 5) 覆盖当前文档内容
        target = DocumentEditService._target_path(doc)
        await minio_client.upload_string(content, target, "text/plain")

        # 6) 更新元数据
        doc.file_hash = content_hash(content)
        doc.version = next_version
        doc.last_edit_time = datetime.now()
        doc.update_time = datetime.now()
        await self.db.commit()

        # 7) 触发重建
        asyncio.create_task(_rebuild(document_id))