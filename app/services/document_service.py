"""文档服务：上传 / 解析 / 分块 / 向量化 / 预览 / 下载 / URL 抓取。

对照 Java `DocumentServiceImpl`（1476 行）。这里拆成三块：
- 本文件：业务编排
- `doc_parser.py`：文件 → 文本
- `chunking.py`：文本 → 分块

## 入库流水线（这是整个知识库的核心）

    upload        原始文件 → MinIO，document 落库（status=uploading）
      ↓
    ingest        document.status = pending
      ↓
    parse         读 MinIO 原文 → extract_text → 扫描件则 OCR → markdown 回写 MinIO
      ↓
    chunk         chunking.split → 写 chunk 表（status=chunked）
      ↓
    vectorize     批量 embed → 写 embedding 表 → chunk.status=vectorized
      ↓
    complete      document.status=completed，刷新知识库统计

每一步失败都要把 `document.status` 置为 `failed` 并写 `error_message`。
**漏了这步前端会永久轮询** —— `KnowledgeDetail.vue` 里
`PROCESSING_STATUSES = {uploading, pending, processing}`，
状态卡在这三个值里，1 秒一次的轮询永远不会停。

## 一个必须知道的事实：向量表是 `embedding` 不是 `chunk.vector`

`embedding` 表独立于 `chunk`，通过 `chunk_id` 关联，还有 `qa_pair_id` 列。
**唯一索引带条件**：`uk_embedding_chunk_id ON (chunk_id) WHERE chunk_id IS NOT NULL`。
所以插入前不用手动查重，`ON CONFLICT` 也能用，但条件是 `WHERE` 那种半唯一
需要显式写冲突列 —— 这里直接用"先删后插"最简单，符合 Java 的
`deleteByDocumentId` + `batchSaveVectors` 两步。

## model_name 必须写对

库里 161 条老向量的 `model_name` 写的是 `Qwen/Qwen3-VL-Embedding-8B`，
但实测是 `bge-large-zh-v1.5`。新写向量时**必须写实际用的模型名**，
不能沿用历史脏值 —— 否则以后做"按模型重建索引"会漏掉这批。
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime
from typing import Any

from fastapi import HTTPException, UploadFile
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.snowflake import new_id
from app.models.entities import Chunk, Document, Knowledge
from app.schemas.common import PageResult
from app.schemas.knowledge_doc import (
    ChunkOut,
    DocumentOut,
    DownloadInfoOut,
    IngestIn,
    UrlPreviewOut,
    UrlSaveIn,
)
from app.services import chunking, doc_parser, minio_client
from app.services.embedding_client import embed, to_pgvector
from app.services.knowledge_service import KnowledgeService

logger = logging.getLogger(__name__)

# 单文件上限 100MB（Java `MAX_FILE_SIZE`）
MAX_FILE_SIZE = 100 * 1024 * 1024
# 批量上传：文件数 ≤ 50、总大小 ≤ 500MB（Java 同）
MAX_BATCH_COUNT = 50
MAX_BATCH_BYTES = 500 * 1024 * 1024
# 每批 embed 的条数。Java 用 50，实测 50 条中文（每条 500 字）约 3.5s，
# 太大容易撞供应商的超时（腾讯云 30s）。
EMBED_BATCH_SIZE = 50

# 单条文本送去 embedding 的 token 预算上限。
#
# **这是实测出来的硬限制，不是抄 Java 也不是拍脑袋**。
#
# bge-large-zh-v1.5 的 `max_position_embeddings = 512`。但
# **分块用的 token 计数和模型 tokenizer 不是同一套口径**：
# `chunking.count_tokens` 是正则口径（中文按字、英文按词、标点不算），
# 而 BPE tokenizer 会把标点、换行、`##` 这类符号单独算一个 token。
# 实测换算比例约 **1 : 1.2~1.27**（正则口径 420 → 模型侧约 520~530）。
#
# 实测数据（同一段 markdown 混排文本，递增截断）：
#   正则 200 / 257 字符 → OK
#   正则 400 / 509 字符 → OK
#   正则 420 / 536 字符 → OK
#   正则 440 / 565 字符 → 400 code 20015 The parameter is invalid
#
# 取 **400** 作为安全值，留约 5% 余量应对纯符号/英文密集的文本
# （英文单词的 token/字符比更差，`internationalization` 一个词 4-5 token）。
#
# 超过这个值不是"块更大"，而是**整批向量化 400 失败、文档变 failed**。
EMBED_MAX_TOKENS = 400


def _now() -> datetime:
    return datetime.now()


class DocumentService:
    def __init__(self, db: AsyncSession, user_id: int) -> None:
        self.db = db
        self.user_id = user_id
        self._kb = KnowledgeService(db, user_id)

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    async def _get_doc(self, doc_id: int) -> Document:
        doc = (
            await self.db.execute(
                select(Document).where(Document.id == doc_id, Document.deleted == 0)
            )
        ).scalar_one_or_none()
        if doc is None:
            raise HTTPException(status_code=404, detail="文档不存在")
        return doc

    @staticmethod
    def _to_out(doc: Document) -> DocumentOut:
        return DocumentOut(
            id=str(doc.id),
            knowledge_id=str(doc.knowledge_id),
            user_id=str(doc.user_id),
            name=doc.name,
            file_type=doc.file_type,
            file_size=doc.file_size,
            file_path=doc.file_path,
            markdown_path=doc.markdown_path,
            chunk_count=doc.chunk_count or 0,
            token_count=doc.token_count or 0,
            status=doc.status,
            error_message=doc.error_message,
            duplicate_rate=float(doc.duplicate_rate) if doc.duplicate_rate is not None else None,
            embedding_json=doc.embedding_json,
            metadata=doc.metadata_,
            version=doc.version,
            last_edit_time=doc.last_edit_time,
            create_time=doc.create_time,
            update_time=doc.update_time,
        )

    async def list_documents(
        self, knowledge_id: int, keyword: str | None, page_num: int, page_size: int
    ) -> PageResult[DocumentOut]:
        await self._kb.ensure_member(knowledge_id)
        cond = [Document.knowledge_id == knowledge_id, Document.deleted == 0]
        if keyword:
            cond.append(Document.name.ilike(f"%{keyword}%"))

        total = (
            await self.db.execute(select(func.count()).select_from(Document).where(*cond))
        ).scalar_one()
        rows = (
            (
                await self.db.execute(
                    select(Document)
                    .where(*cond)
                    .order_by(Document.create_time.desc())
                    .offset((page_num - 1) * page_size)
                    .limit(page_size)
                )
            )
            .scalars()
            .all()
        )
        return PageResult.of([self._to_out(d) for d in rows], total, page_num, page_size)

    async def get_document(self, doc_id: int) -> DocumentOut:
        doc = await self._get_doc(doc_id)
        await self._kb.ensure_member(doc.knowledge_id)
        return self._to_out(doc)

    async def list_chunks(self, doc_id: int) -> list[ChunkOut]:
        doc = await self._get_doc(doc_id)
        await self._kb.ensure_member(doc.knowledge_id)
        rows = (
            (
                await self.db.execute(
                    select(Chunk)
                    .where(Chunk.document_id == doc_id)
                    .order_by(Chunk.chunk_index)
                )
            )
            .scalars()
            .all()
        )
        return [
            ChunkOut(
                id=str(c.id),
                document_id=str(c.document_id),
                content=c.content,
                chunk_index=c.chunk_index,
                token_count=c.token_count,
                status=c.status,
                metadata=c.metadata_,
                create_time=c.create_time,
            )
            for c in rows
        ]

    # ------------------------------------------------------------------
    # 上传
    # ------------------------------------------------------------------

    async def _check_duplicate(
        self, knowledge_id: int, file_hash: str, force: str | None
    ) -> list[Document]:
        """重复检测 + force 语义。

        - `force=overwrite` → 返回**所有**同哈希副本，调用方先删再传
        - `force=keep-both` → 返回 []（放行，允许同内容多份）
        - 默认 / `force` 为空 → 抛 400，错误信息里带旧文档名

        ⚠️ **刻意不用 `scalar_one_or_none()`**：
        一旦库里有 N 份同哈希副本（`keep-both` 连点几次就有了），
        `scalar_one_or_none` 会抛 `MultipleResultsFound` → 500，
        用户看到的是"服务器错误"而不是"文件已存在"。
        Java 用的 `getOne()` 其实有同样的毛病（超过 1 条抛 TooManyResultsException），
        这里按"返回列表 + overwrite 全删"处理，语义也更准：
        「覆盖」就是要让这份内容在库里只剩一份。
        """
        rows = (
            await self.db.execute(
                select(Document)
                .where(
                    Document.knowledge_id == knowledge_id,
                    Document.file_hash == file_hash,
                    Document.deleted == 0,
                )
                .order_by(Document.create_time.asc())
            )
        ).scalars().all()
        existing = list(rows)
        if not existing:
            return []
        if force == "keep-both":
            return []
        if force == "overwrite":
            return existing
        name = existing[0].name
        extra = f"（另有 {len(existing) - 1} 份同内容副本）" if len(existing) > 1 else ""
        raise HTTPException(
            status_code=400,
            detail=f"该文件已存在于知识库中：{name}{extra}（可传 force=overwrite 覆盖或 keep-both 保留两份）",
        )

    async def upload(
        self, knowledge_id: int, file: UploadFile, ocr_enabled: bool = False, force: str | None = None
    ) -> DocumentOut:
        await self._kb.ensure_role(knowledge_id, "developer")

        filename = file.filename or "unnamed"
        file_type = doc_parser.detect_file_type(filename)
        if not doc_parser.is_supported(file_type):
            raise HTTPException(status_code=400, detail=f"不支持的文件类型：.{file_type}")

        data = await file.read()
        if len(data) > MAX_FILE_SIZE:
            raise HTTPException(
                status_code=400, detail=f"文件超过 {MAX_FILE_SIZE // 1024 // 1024}MB 限制"
            )

        file_hash = hashlib.md5(data).hexdigest()
        overwrite_targets = await self._check_duplicate(knowledge_id, file_hash, force)
        for target in overwrite_targets:
            logger.info(
                "[文档上传] 覆盖模式，删旧文档 docId=%s name=%s",
                target.id,
                target.name,
            )
            await self.delete_document(target.id)

        object_path = minio_client.generate_path(knowledge_id, filename)
        await minio_client.upload_bytes(data, object_path, minio_client.content_type_of(file_type))

        now = _now()
        doc = Document(
            id=new_id(),
            knowledge_id=knowledge_id,
            user_id=self.user_id,
            name=filename,
            file_path=object_path,
            file_type=file_type,
            file_size=len(data),
            file_hash=file_hash,
            chunk_count=0,
            token_count=0,
            status="uploading",
            metadata_={"ocrEnabled": ocr_enabled},
            version=1,
            create_time=now,
            update_time=now,
            deleted=0,
        )
        self.db.add(doc)
        await self.db.commit()
        await self.db.refresh(doc)

        # 同步入库。Java 丢 Redis 队列，Python 直接 inline ——
        # 好处是错误能直接返回给用户；坏处是大文件会卡住这个 HTTP 请求。
        if settings.ingest_inline:
            await self._ingest_pipeline(
                doc, IngestIn(chunkStrategy="general", chunkSize=512, chunkOverlap=10)
            )
            await self.db.refresh(doc)

        return self._to_out(doc)

    async def upload_batch(
        self,
        knowledge_id: int,
        files: list[UploadFile],
        ocr_enabled: bool = False,
        force: str | None = None,
    ) -> list[DocumentOut]:
        if len(files) > MAX_BATCH_COUNT:
            raise HTTPException(status_code=400, detail=f"批量上传不能超过 {MAX_BATCH_COUNT} 个文件")
        total_bytes = 0
        for f in files:
            # UploadFile.size 在部分 ASGI 实现里是 None，用 seek 兜底要读一遍；
            # 这里只在有值时累加，最后在 upload() 里逐个校验单文件上限。
            total_bytes += getattr(f, "size", None) or 0
        if total_bytes > MAX_BATCH_BYTES:
            raise HTTPException(
                status_code=400, detail=f"批量上传总大小不能超过 {MAX_BATCH_BYTES // 1024 // 1024}MB"
            )

        results: list[DocumentOut] = []
        for f in files:
            # 逐个处理：一个失败不影响其他（Java 也是 for 循环里直接 add，
            # 但 Java 抛异常会中断循环，这里同样抛 —— 保持一致，
            # 因为"批量上传只成功一半"比"整体失败"更难处理）
            results.append(await self.upload(knowledge_id, f, ocr_enabled, force))
        return results

    # ------------------------------------------------------------------
    # 入库流水线
    # ------------------------------------------------------------------

    async def ingest(self, doc_id: int, dto: IngestIn) -> DocumentOut:
        """手动触发入库（重新入库也走这里）。"""
        doc = await self._get_doc(doc_id)
        await self._kb.ensure_role(doc.knowledge_id, "developer")
        await self._ingest_pipeline(doc, dto)
        await self.db.refresh(doc)
        return self._to_out(doc)

    async def _ingest_pipeline(self, doc: Document, dto: IngestIn) -> None:
        """完整入库：解析 → 分块 → 向量化 → 完成。

        每一步失败都写 `status=failed` + `error_message`，然后**抛出去**。
        `upload()` 里没有 try/except —— 让异常冒到接口层，
        前端能看到具体错误；文档状态已经落库，不会卡在中间态。
        """
        doc.status = "pending"
        doc.error_message = None
        doc.embedding_json = dto.model_dump(by_alias=True)
        doc.update_time = _now()
        await self.db.commit()

        try:
            # 1) 清旧数据（重新入库时）
            await self._clear_chunks(doc.id)

            # 2) 解析
            doc.status = "processing"
            await self.db.commit()
            content = await self._resolve_content(doc, dto.ocr_enabled)

            if not content or not content.strip():
                raise ValueError(
                    "文档解析结果为空。可能是扫描版 PDF（请开启 OCR）、加密文档或文件损坏"
                )

            # 3) 回写 markdown（preview 和 preview-chunks 都读它，
            #    不回写的话重新入库后预览还是旧内容）
            md_path = doc.markdown_path or minio_client.generate_markdown_path(
                doc.knowledge_id, doc.file_path or ""
            )
            await minio_client.upload_string(content, md_path, "text/markdown; charset=utf-8")
            doc.markdown_path = md_path

            # 4) 分块
            #
            # **分块上限被 embedding 模型卡住**：用户可以在 UI 里填 2000，
            # 但 bge-large-zh-v1.5 只有 512 上下文。填 2000 的结果不是"块更大"，
            # 而是**向量化整批 400 失败**，文档变成 failed。
            # 这里不报错、不拒绝，而是**静默夹到安全值** ——
            # 因为用户填 2000 的意图是"少切几块"，夹到 480 仍满足这个意图；
            # 报错让他去猜"为什么 2000 不行"反而更糟。
            effective_size = min(dto.chunk_size, EMBED_MAX_TOKENS)
            if effective_size < dto.chunk_size:
                logger.info(
                    "[文档入库] 分块大小由 %s 夹到 %s（embedding 模型上限）",
                    dto.chunk_size, effective_size,
                )
            params = chunking.ChunkParams(
                chunk_token_num=effective_size,
                overlapped_percent=dto.chunk_overlap,
                delimiter=dto.chunk_delimiter or "\n",
                collapse_whitespace=bool(dto.collapse_whitespace),
                text_clean_rules=dto.text_clean_rules,
            )
            blocks = chunking.split_blocks(content, dto.chunk_strategy, params)
            if not blocks:
                raise ValueError(
                    f"分块结果为空：所有片段都低于最小 {chunking.MIN_CHUNK_TOKENS} token，"
                    "请调小分块大小或检查文档内容"
                )

            # 5) 写 chunk
            chunk_rows: list[Chunk] = []
            total_tokens = 0
            for idx, block in enumerate(blocks):
                text_content = chunking.normalize_chunk_content(
                    block.content, params.collapse_whitespace
                )
                tokens = chunking.count_tokens(text_content)
                chunk_rows.append(
                    Chunk(
                        id=new_id(),
                        document_id=doc.id,
                        knowledge_id=doc.knowledge_id,
                        content=text_content,
                        chunk_index=idx,
                        token_count=tokens,
                        status="chunked",
                        parent_content=block.parent_content,
                        metadata_={},
                        create_time=_now(),
                        update_time=_now(),
                    )
                )
                total_tokens += tokens
            self.db.add_all(chunk_rows)
            doc.chunk_count = len(chunk_rows)
            doc.token_count = total_tokens
            await self.db.commit()

            # 6) 向量化
            ok_cnt, fail_cnt = await self._vectorize(doc, chunk_rows)

            if ok_cnt == 0 and chunk_rows:
                # 一条向量都没有 yet标 completed 是最坏的结果：
                # 前端绿勾、检索永远空、控制台无报错
                msg = (
                    f"全部 {fail_cnt} 个分块向量化失败，请检查 embedding 模型配置"
                    f"（当前 {settings.embedding_model}）或稍后重试"
                )
                doc.status = "failed"
                doc.error_message = msg
                doc.update_time = _now()
                await self.db.commit()
                logger.error("[文档入库] %s docId=%s", msg, doc.id)
                raise HTTPException(status_code=502, detail=msg)

            if fail_cnt:
                logger.warning(
                    "[文档入库] 部分失败 docId=%s 成功=%s 失败=%s", doc.id, ok_cnt, fail_cnt
                )

            doc.status = "completed"
            doc.error_message = None
            doc.update_time = _now()
            await self.db.commit()

            # 7) 统计（用真实值重算，不靠增量 —— 重新入库时增量会翻倍）
            await self._refresh_kb_stats(doc.knowledge_id)
            logger.info(
                "[文档入库] 完成 docId=%s chunks=%s tokens=%s strategy=%s",
                doc.id, len(chunk_rows), total_tokens, dto.chunk_strategy,
            )
        except ValueError as exc:
            # 业务级失败（解析空 / 分块空 / 不支持的格式）→ 400，
            # 让前端弹出可操作的提示，而不是 500 堆栈
            logger.warning("[文档入库] 业务失败 docId=%s err=%s", doc.id, exc)
            doc.status = "failed"
            doc.error_message = str(exc)[:2000]
            doc.update_time = _now()
            await self.db.commit()
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            logger.error("[文档入库] 失败 docId=%s err=%s", doc.id, exc)
            doc.status = "failed"
            doc.error_message = str(exc)[:2000]
            doc.update_time = _now()
            await self.db.commit()
            raise

    async def _resolve_content(self, doc: Document, ocr_enabled: bool | None) -> str:
        """读原文 → 解析 → 扫描件补 OCR。

        **优先级**：已有 markdown → 直接读（省一次解析）；
        否则读原文解析。这与 Java 的 `processDocumentWithProgress` 一致。

        扫描件判定必须做：否则扫描版 PDF 只有目录页那点字，
        分块出来 3 块，用户以为入库成功但检索永远查不到。
        """
        # 1) 已有 markdown 直接用（重新入库时要重新解析，所以这里只在
        #    markdown 比原文新时才用 —— 简化处理：有就用，和 Java 一致）
        if doc.markdown_path and await minio_client.exists(doc.markdown_path):
            cached = await minio_client.download_text(doc.markdown_path)
            if cached.strip():
                return cached

        if not doc.file_path:
            raise ValueError("文档缺少存储路径，无法解析")

        raw = await minio_client.download_bytes(doc.file_path)

        # 2) 图片 / 扫描件 → OCR
        is_image = doc_parser.is_image_type(doc.file_type)
        scanned = False
        if not is_image and doc.file_type == "pdf":
            # 判定需要文件，先落临时文件
            import tempfile
            from pathlib import Path

            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                tmp.write(raw)
                tmp_path = Path(tmp.name)
            try:
                scanned = doc_parser.is_scanned_pdf(doc.file_type, tmp_path)
            finally:
                tmp_path.unlink(missing_ok=True)

        if is_image or scanned:
            if ocr_enabled is False:
                # 用户明确关掉 OCR：如实说明为什么解析不出内容，
                # 而不是返回空串让他猜
                raise ValueError(
                    "该文档是图片/扫描件，需要 OCR。请在入库配置里勾选「OCR 识别」后重试"
                )
            from app.tools.builtin_ocr import ocr_bytes

            ocr_text = await ocr_bytes(raw, doc.file_type or "png")
            if is_image:
                return ocr_text
            # 扫描 PDF：把 PDF 文本层（目录页）和 OCR 正文拼一起
            base = ""
            import tempfile
            from pathlib import Path

            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                tmp.write(raw)
                tmp_path = Path(tmp.name)
            try:
                base = doc_parser.extract_text(tmp_path, "pdf")
            finally:
                tmp_path.unlink(missing_ok=True)
            return f"{base}\n\n---\n\n{ocr_text}" if base.strip() else ocr_text

        # 3) 普通解析
        import tempfile
        from pathlib import Path

        suffix = f".{doc.file_type}" if doc.file_type else ".bin"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(raw)
            tmp_path = Path(tmp.name)
        try:
            return doc_parser.extract_text(tmp_path, doc.file_type)
        finally:
            tmp_path.unlink(missing_ok=True)

    async def _vectorize(self, doc: Document, chunks: list[Chunk]) -> tuple[int, int]:
        """批量向量化，返回 `(成功数, 失败数)`。

        单条失败只标记那个 `chunk.status='failed'`，不中断整批。
        为什么不中断：一本书 500 个 chunk，中间有一条超长被供应商拒了，
        整批放弃等于白烧 499 次调用。标记失败让用户能看到"哪些块没成功"，
        重新入库时只补这些。

        **全失败必须让调用方知道**：否则文档会被标成 completed，
        但库里一条向量都没有 —— 检索永远返回空，且不报任何错。
        """
        kb = (
            await self.db.execute(select(Knowledge).where(Knowledge.id == doc.knowledge_id))
        ).scalar_one_or_none()
        model_name = (kb.embedding_model if kb else None) or settings.embedding_model

        stat_ok = 0
        stat_fail = 0

        for start in range(0, len(chunks), EMBED_BATCH_SIZE):
            batch = chunks[start : start + EMBED_BATCH_SIZE]
            # 送模型前截断到安全 token 预算（见 EMBED_MAX_TOKENS 注释）。
            # 只截**送去 embed 的文本**，`chunk.content` 保持原文不动 ——
            # 库里存的必须是完整内容，否则检索命中后给模型的上下文是残的。
            texts = [_clip_for_embedding(c.content) for c in batch]
            vectors = await _embed_texts_resilient(texts)

            ok_rows: list[dict[str, Any]] = []
            failed_ids: list[int] = []
            for chunk, vector in zip(batch, vectors, strict=True):
                if vector is None:
                    failed_ids.append(chunk.id)
                    continue
                ok_rows.append(
                    {
                        "id": new_id(),
                        "chunk_id": chunk.id,
                        "qa_pair_id": None,
                        "model_name": model_name,
                        "dimension": len(vector),
                        "vector": to_pgvector(vector),
                        "knowledge_id": doc.knowledge_id,
                    }
                )

            if ok_rows:
                # 先删后插：唯一索引 `uk_embedding_chunk_id` 是条件索引，
                # ON CONFLICT 需要显式指定索引谓词，容易写歪。
                # 删除再插最简单，也符合 Java 的 deleteByDocumentId + batchSave。
                await self.db.execute(
                    text("DELETE FROM embedding WHERE chunk_id = ANY(:ids)"),
                    {"ids": [r["chunk_id"] for r in ok_rows]},
                )
                await self.db.execute(
                    text(
                        """
                        INSERT INTO embedding
                            (id, chunk_id, qa_pair_id, model_name, dimension, vector, knowledge_id)
                        VALUES
                            (:id, :chunk_id, :qa_pair_id, :model_name, :dimension,
                             CAST(:vector AS vector), :knowledge_id)
                        """
                    ),
                    ok_rows,
                )
                await self.db.execute(
                    update(Chunk)
                    .where(Chunk.id.in_([r["chunk_id"] for r in ok_rows]))
                    .values(status="vectorized", update_time=_now())
                )

            if failed_ids:
                await self.db.execute(
                    update(Chunk)
                    .where(Chunk.id.in_(failed_ids))
                    .values(status="failed", update_time=_now())
                )
            stat_ok += len(ok_rows)
            stat_fail += len(failed_ids)
            await self.db.commit()

        return stat_ok, stat_fail

    async def _clear_chunks(self, doc_id: int) -> None:
        """删旧分块 + 对应向量。重新入库前必调，否则 chunk_count 翻倍。"""
        await self.db.execute(
            text("DELETE FROM embedding WHERE chunk_id IN (SELECT id FROM chunk WHERE document_id = :did)"),
            {"did": doc_id},
        )
        await self.db.execute(delete(Chunk).where(Chunk.document_id == doc_id))
        await self.db.commit()

    async def _refresh_kb_stats(self, knowledge_id: int) -> None:
        """用真实 COUNT 重算知识库统计。

        **不用增量**（Java 的 `updateStats(+1)`）：重新入库时
        增量会累加两次，统计越用越离谱。前端有「修复统计」按钮
        就是因为这个漂移在 Java 侧真实发生过。
        """
        row = (
            await self.db.execute(
                select(
                    func.count(Document.id),
                    func.coalesce(func.sum(Document.token_count), 0),
                ).where(Document.knowledge_id == knowledge_id, Document.deleted == 0)
            )
        ).one()
        chunk_count = (
            await self.db.execute(
                select(func.count(Chunk.id)).where(Chunk.knowledge_id == knowledge_id)
            )
        ).scalar_one()
        await self.db.execute(
            update(Knowledge)
            .where(Knowledge.id == knowledge_id)
            .values(
                document_count=int(row[0] or 0),
                chunk_count=int(chunk_count or 0),
                total_tokens=int(row[1] or 0),
                update_time=_now(),
            )
        )
        await self.db.commit()

    # ------------------------------------------------------------------
    # 预览
    # ------------------------------------------------------------------

    async def preview_document(self, doc_id: int) -> str:
        """读 markdown 供前端渲染。**不做 MinIO 图片 URL 重写**（见 docstring 末尾）。"""
        doc = await self._get_doc(doc_id)
        await self._kb.ensure_member(doc.knowledge_id)

        if doc.markdown_path and await minio_client.exists(doc.markdown_path):
            content = await minio_client.download_text(doc.markdown_path)
            return self._rewrite_image_urls(content, doc.knowledge_id)

        content = await self._resolve_content(doc, True)
        return self._rewrite_image_urls(content, doc.knowledge_id)

    @staticmethod
    def _rewrite_image_urls(content: str, knowledge_id: int) -> str:
        """把 MinIO 直连图片 URL 换成本后端代理地址。

        为什么要重写：MinIO 预签名 URL 带临时 token，过期后 markdown 里的
        图片全部裂开。代理地址 `/api/knowledge/images/{kid}/{filename}`
        每次请求现签，永远有效。

        真实 markdown 里的形态可能是完整 URL（`http://localhost:9100/lengbot/knowledge/..`）
        也可能是相对 key，两种都换。
        """
        if not content:
            return content
        prefix_full = (
            f"{settings.minio_endpoint.rstrip('/')}/{settings.minio_bucket}"
            f"/knowledge/{knowledge_id}/images/"
        )
        prefix_key = f"/knowledge/{knowledge_id}/images/"
        proxy = f"/api/knowledge/images/{knowledge_id}/"
        return content.replace(prefix_full, proxy).replace(prefix_key, proxy)

    async def preview_chunks(self, doc_id: int, dto: IngestIn) -> list[str]:
        """预览分块结果（不入库）。

        必须走**和入库同一条链**（同样的解析 + 同样的分块参数），
        否则用户看到的预览和实际入库结果不一致，调参数时会被误导。
        """
        doc = await self._get_doc(doc_id)
        await self._kb.ensure_member(doc.knowledge_id)

        content = await self._resolve_content(doc, dto.ocr_enabled)
        if not content or not content.strip():
            return []

        # 和入库同一条链：同样夹到 embedding 上限，否则"预览看着 2000 一块、
        # 入库实际 480 一块"，用户调参永远调不准
        params = chunking.ChunkParams(
            chunk_token_num=min(dto.chunk_size, EMBED_MAX_TOKENS),
            overlapped_percent=dto.chunk_overlap,
            delimiter=dto.chunk_delimiter or "\n",
            collapse_whitespace=bool(dto.collapse_whitespace),
            text_clean_rules=dto.text_clean_rules,
        )
        texts = chunking.split_texts(content, dto.chunk_strategy, params)
        return [
            chunking.normalize_chunk_content(t, params.collapse_whitespace) for t in texts
        ]

    # ------------------------------------------------------------------
    # 下载
    # ------------------------------------------------------------------

    async def download_info(self, doc_id: int) -> DownloadInfoOut:
        doc = await self._get_doc(doc_id)
        await self._kb.ensure_member(doc.knowledge_id)
        if not doc.file_path:
            raise HTTPException(status_code=404, detail="文档没有存储文件")
        ctype = minio_client.content_type_of(doc.file_type)
        url = await minio_client.presigned_url(doc.file_path, ctype)
        return DownloadInfoOut(
            url=url,
            file_type=doc.file_type or "",
            file_name=doc.name,
            content_type=ctype,
        )

    async def download_stream(self, doc_id: int) -> tuple[bytes, str, str]:
        """返回 (bytes, 文件名, content_type)。供 `download-file` / `preview-file` 用。"""
        doc = await self._get_doc(doc_id)
        await self._kb.ensure_member(doc.knowledge_id)
        if not doc.file_path:
            raise HTTPException(status_code=404, detail="文档没有存储文件")
        data = await minio_client.download_bytes(doc.file_path)
        return data, doc.name, minio_client.content_type_of(doc.file_type)

    async def serve_image(self, knowledge_id: int, filename: str) -> tuple[bytes, str] | None:
        """代理知识库文档内嵌图片。

        `filename` 来自 URL path，**必须防路径穿越** ——
        攻击者传 `../../../etc/passwd` 会读到任意对象。
        这里只允许纯文件名（不含 `/` 和 `..`）。
        """
        await self._kb.ensure_member(knowledge_id)
        if "/" in filename or "\\" in filename or ".." in filename:
            return None
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if ext not in {"png", "jpg", "jpeg", "webp", "bmp", "gif", "svg"}:
            return None
        path = f"knowledge/{knowledge_id}/images/{filename}"
        if not await minio_client.exists(path):
            return None
        data = await minio_client.download_bytes(path)
        return data, minio_client.content_type_of(ext)

    # ------------------------------------------------------------------
    # 删除
    # ------------------------------------------------------------------

    async def delete_document(self, doc_id: int) -> None:
        """级联删除：向量 → 分块 → MinIO 文件 → 软删记录 → 刷新统计。

        顺序有讲究：先删向量再删分块。反过来会留孤儿 embedding
        （chunk 没了但 vector 还在，占空间且检索时 join 不到）。
        """
        doc = await self._get_doc(doc_id)
        await self._kb.ensure_role(doc.knowledge_id, "developer")

        await self._clear_chunks(doc_id)
        await minio_client.delete(doc.file_path)
        await minio_client.delete(doc.markdown_path)

        # 图谱台账（Neo4j 侧不归 Python 管，只清 PG 记录）
        await self.db.execute(
            text("UPDATE graph_document SET deleted = 1, update_time = :now WHERE document_id = :did"),
            {"did": doc_id, "now": _now()},
        )

        doc.deleted = 1
        doc.update_time = _now()
        await self.db.commit()

        await self._refresh_kb_stats(doc.knowledge_id)

    # ------------------------------------------------------------------
    # URL 抓取
    # ------------------------------------------------------------------

    async def preview_url(self, knowledge_id: int, url: str) -> UrlPreviewOut:
        await self._kb.ensure_member(knowledge_id)
        result = await _fetch_url_safe(url)
        return UrlPreviewOut(
            url=result.url,
            title=result.title,
            content=result.content,
            preview_html=result.preview_html,
            suggested_file_name=result.suggested_file_name,
            content_length=len(result.content),
            description=result.description,
        )

    async def save_url(self, knowledge_id: int, dto: UrlSaveIn) -> DocumentOut:
        await self._kb.ensure_role(knowledge_id, "developer")
        return await self._persist_url(
            knowledge_id, dto.url, dto.title, dto.content, dto.sync_config
        )

    async def fetch_url(self, knowledge_id: int, url: str) -> DocumentOut:
        await self._kb.ensure_role(knowledge_id, "developer")
        result = await _fetch_url_safe(url)
        return await self._persist_url(
            knowledge_id, url, result.title, result.content, None
        )

    async def _persist_url(
        self,
        knowledge_id: int,
        url: str,
        title: str | None,
        content: str,
        sync_config: dict | None,
    ) -> DocumentOut:
        """把网页正文存成 `.txt` 文档。

        为什么不存成 `.md`：正文是纯文本（`web_fetch` 用 readability 抽的），
        存 md 会让用户以为有格式。`.txt` 语义准确。
        """
        if not content or not content.strip():
            raise HTTPException(status_code=400, detail="网页内容为空，无法保存")

        file_hash = hashlib.md5(content.encode("utf-8")).hexdigest()
        existing = await self._check_duplicate(knowledge_id, file_hash, None)
        if existing is not None:  # _check_duplicate 已抛过，这里是 keep-both 分支保护
            pass

        filename = _build_url_filename(title, url)
        object_path = minio_client.generate_path(knowledge_id, filename)
        await minio_client.upload_string(content, object_path, "text/plain; charset=utf-8")

        metadata: dict[str, Any] = {
            "sourceUrl": url,
            "title": title or "",
            "fetchedAt": int(datetime.now().timestamp() * 1000),
        }
        if sync_config:
            if sync_config.get("headers"):
                metadata["headers"] = sync_config["headers"]
            if sync_config.get("syncInterval"):
                metadata["syncInterval"] = sync_config["syncInterval"]

        now = _now()
        doc = Document(
            id=new_id(),
            knowledge_id=knowledge_id,
            user_id=self.user_id,
            name=filename,
            file_path=object_path,
            file_type="txt",
            file_size=len(content.encode("utf-8")),
            file_hash=file_hash,
            chunk_count=0,
            token_count=0,
            status="uploaded",
            metadata_=metadata,
            version=1,
            create_time=now,
            update_time=now,
            deleted=0,
        )
        self.db.add(doc)
        await self.db.commit()
        await self.db.refresh(doc)

        if settings.ingest_inline:
            await self._ingest_pipeline(
                doc, IngestIn(chunkStrategy="general", chunkSize=512, chunkOverlap=10)
            )
            await self.db.refresh(doc)
        return self._to_out(doc)

    async def sync_url(self, doc_id: int) -> DocumentOut:
        """重新抓取 URL 来源文档。内容没变就不重新入库（省一次向量化）。

        Java 也是这个判断（`if (!Objects.equals(oldHash, newHash))` 才重新入库）——
        网页同步通常是定时任务，每次都全量重嵌会白烧大量 token。
        """
        doc = await self._get_doc(doc_id)
        await self._kb.ensure_role(doc.knowledge_id, "developer")

        meta = doc.metadata_ or {}
        source_url = meta.get("sourceUrl") if isinstance(meta, dict) else None
        if not source_url:
            raise HTTPException(status_code=400, detail="该文档不是 URL 来源，无法同步")

        headers = meta.get("headers") if isinstance(meta, dict) else None
        result = await _fetch_url_safe(source_url, headers)

        if not doc.file_path:
            raise HTTPException(status_code=400, detail="文档缺少存储路径")

        new_hash = hashlib.md5(result.content.encode("utf-8")).hexdigest()
        if new_hash == doc.file_hash:
            logger.info("[URL同步] 内容未变化，跳过重新入库 docId=%s", doc_id)
            return self._to_out(doc)

        await minio_client.upload_string(result.content, doc.file_path, "text/plain; charset=utf-8")
        doc.file_hash = new_hash
        doc.file_size = len(result.content.encode("utf-8"))
        doc.version = (doc.version or 1) + 1
        meta = dict(meta) if isinstance(meta, dict) else {}
        meta["fetchedAt"] = int(datetime.now().timestamp() * 1000)
        doc.metadata_ = meta
        doc.update_time = _now()
        await self.db.commit()

        await self._ingest_pipeline(
            doc, IngestIn(chunkStrategy="general", chunkSize=512, chunkOverlap=10)
        )
        await self.db.refresh(doc)
        return self._to_out(doc)


def _clip_for_embedding(text: str, max_tokens: int = EMBED_MAX_TOKENS) -> str:
    """把送去 embedding 的文本截到安全 token 预算内。

    截断策略：先按 token 边界取前 `max_tokens` 个 token，
    再回退到最近的句子结束符（。！？.!?\\n）。原因：
    在 token 中间截断会留下半个词，模型编码时这个残词会污染向量方向。
    回退到句末最多损失 20 个 token，换来向量质量的稳定。

    极端情况（整段没有句号）直接按 token 截。
    """
    if chunking.count_tokens(text) <= max_tokens:
        return text

    clipped = chunking.hard_split_by_tokens(text, max_tokens)[0] if text else ""

    # 回退到最后一个句子边界（最多回退 10%，避免整段退化）
    floor = int(len(clipped) * 0.9)
    for idx in range(len(clipped) - 1, floor - 1, -1):
        if clipped[idx] in "。！？.!?\n":
            return clipped[: idx + 1]
    return clipped


async def _fetch_url_safe(url: str, headers: dict[str, str] | None = None):
    """抓 URL，把 SSRF 拦截转成 **400**。

    `SsrfBlocked` 是普通 Exception，不转的话 Starlette 按 500 返回：
    前端以为是服务端炸了（还会重试），实际是用户填了个内网地址 ——
    典型的"错误码掩盖了真实原因"。安全的 URL 也一样：网络失败本来就该 400/502，
    这里只对**明确被判定为非法目标**的路径做降级。
    """
    from app.services.web_fetch import SsrfBlocked, fetch_url

    try:
        return await fetch_url(url, headers)
    except SsrfBlocked as exc:
        raise HTTPException(status_code=400, detail=f"不允许抓取该地址：{exc}") from exc


# 单条 embed 失败后的缩短策略。
# 供应商不会告诉你 input 里是哪一条有问题，只给一个笼统的
# `code:20015 The parameter is invalid`，所以只能：
#   整批失败 → 逐条重试（隔离罪魁）→ 对仍失败的那条按比例缩短再试
# 比例 0.8、最多 5 级：能把正则口径 400 token 逐步压到 164，
# 足以覆盖 "数字/符号密集 → 真实 tokenizer 膨胀 1.5 倍" 的极端情况。
EMBED_RETRY_RATIO = 0.8
EMBED_RETRY_STEPS = 5
# 缩短到这个 token 数还没成功就放弃（再短也没检索价值了）
EMBED_MIN_RETRY_TOKENS = 40


async def _embed_one_resilient(text: str) -> list[float] | None:
    """单条 embed，失败就缩短重试。彻底救不回来返回 None。"""
    cur = text
    for step in range(EMBED_RETRY_STEPS):
        try:
            return (await embed([cur]))[0]
        except Exception as exc:  # noqa: BLE001
            tokens = chunking.count_tokens(cur)
            nxt = int(tokens * EMBED_RETRY_RATIO)
            if nxt < EMBED_MIN_RETRY_TOKENS:
                logger.error(
                    "[向量化] 单条反复失败，放弃：tokens=%s err=%s", tokens, exc
                )
                return None
            pieces = chunking.hard_split_by_tokens(cur, nxt)
            if not pieces:
                logger.error("[向量化] 截断后为空，放弃：tokens=%s", tokens)
                return None
            logger.warning(
                "[向量化] 单条被拒（第 %s 次），%s → %s token 后重试", step + 1, tokens, nxt
            )
            cur = pieces[0]
    return None


async def _embed_texts_resilient(texts: list[str]) -> list[list[float] | None]:
    """批量 embed，**一条坏文本不能拖垮整批**。

    为什么要这一层：`EMBED_MAX_TOKENS=400` 是按 **正则口径** 夹的，
    而模型用的是 BPE tokenizer。内容里一旦数字/英文/标点密集，
    换算比会从 1:1.2 恶化到 1:1.5+，400 依然可能超限 —— 而供应商的表现是
    **整个 batch 400**，且 message 里不指出是哪一条。

    不处理的话：一个坏块 → 整批失败 → 整个文档 failed → 用户看到"入库失败"，
    完全不知道只是一段序号炸了。
    """
    try:
        return await embed(texts)  # type: ignore[return-value]
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[向量化] 整批失败（%s 条），降级逐条重试：%s", len(texts), exc
        )

    return [await _embed_one_resilient(t) for t in texts]


def _build_url_filename(title: str | None, url: str) -> str:
    """网页标题 → 安全的文件名。去掉 `<>:"/\\|?*` 这些 Windows 保留字符。"""
    import re

    base = (title or "").strip()
    if not base:
        # 退到 URL 的最后一段
        base = url.rstrip("/").rsplit("/", 1)[-1].split("?")[0] or "webpage"
    base = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", base)[:80].strip()
    return f"{base or 'webpage'}.txt"
