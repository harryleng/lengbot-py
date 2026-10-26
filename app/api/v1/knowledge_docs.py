"""文档管理接口（18 个，对齐前端 `src/api/knowledge.js` 的文档相关调用）。

## 路由顺序：本文件全部是静态前缀，必须比 `knowledge.py` 先注册

`/knowledge/documents/{doc_id}` 里的 `documents` 如果后注册，
会被 `/knowledge/{knowledge_id}` 当成 knowledge_id（转 int 失败）→ 422/404。
`main.py` 的 include 顺序已经固定：`knowledge_docs` → `knowledge_qa` → `knowledge`。

## 两个返回二进制而不是 `Result` 的接口

- `GET /documents/{id}/download-file`：`Content-Disposition: attachment`
- `GET /documents/{id}/preview-file`：`Content-Disposition: inline`，供 iframe 渲染
- `GET /images/{kid}/{filename}`：代理文档内嵌图片

这三个**不能包 `Result`** —— 前端 `<iframe src="/api/knowledge/documents/1/preview-file">`
拿到 JSON 壳会显示空白。Java 用的是 `ResponseEntity<StreamingResponseBody>`，
这里用 `Response(content=..., media_type=...)`。

**为什么要有 preview-file 而不是直接用预签名 URL**：
MinIO 的预签名 URL 是跨域的，浏览器内嵌 PDF 时会被
`X-Frame-Options` / 广告拦截插件挡住；而且预签名 URL 带临时 token，
iframe 重新加载就换了 URL，浏览器的 PDF 阅读器状态全丢。
同源代理最稳。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, File, Query, Response, UploadFile

from app.api.deps import CurrentUserId, DbSession
from app.schemas.common import PageResult, Result
from app.schemas.knowledge_doc import (
    ChunkOut,
    DocumentOut,
    DownloadInfoOut,
    IngestIn,
    UrlPreviewOut,
    UrlSaveIn,
)
from app.services.document_service import DocumentService

router = APIRouter(prefix="/api/knowledge", tags=["knowledge-documents"])


def _svc(db: DbSession, user_id: CurrentUserId) -> DocumentService:
    return DocumentService(db, user_id)


# ----------------------------------------------------------------------
# 文档 CRUD
# ----------------------------------------------------------------------


@router.get(
    "/{knowledge_id}/documents",
    response_model=Result[PageResult[DocumentOut]],
    summary="文档列表（支持关键字 + 分页）",
)
async def list_documents(
    knowledge_id: int,
    db: DbSession,
    user_id: CurrentUserId,
    keyword: str | None = Query(None),
    page_num: int = Query(1, alias="pageNum"),
    page_size: int = Query(50, alias="pageSize"),
) -> Result[PageResult[DocumentOut]]:
    """`pageSize` 默认 50（Java 同）。

    前端 `loadDocuments` 只读 `res.data.records` / `res.data.total`，
    分页组件在文档多于一页时才会出现。
    """
    return Result.ok(
        await _svc(db, user_id).list_documents(knowledge_id, keyword, page_num, page_size)
    )


@router.post(
    "/{knowledge_id}/documents",
    response_model=Result[DocumentOut],
    summary="上传单个文档",
)
async def upload_document(
    knowledge_id: int,
    db: DbSession,
    user_id: CurrentUserId,
    file: Annotated[UploadFile, File(...)],
    ocr_enabled: bool = Query(False, alias="ocrEnabled"),
    force: str | None = Query(None),
) -> Result[DocumentOut]:
    """`ocrEnabled` / `force` 是 query 参数（对齐 Java `@RequestParam`）。

    前端：
        formData.append('file', file)
        request.post(url, formData, { params: { ocrEnabled } })
    `force` 三态：`overwrite` 覆盖 / `keep-both` 保留两份 / 不传则拒绝重复。
    """
    return Result.ok(await _svc(db, user_id).upload(knowledge_id, file, ocr_enabled, force))


@router.post(
    "/{knowledge_id}/documents/batch",
    response_model=Result[list[DocumentOut]],
    summary="批量上传（≤50 个 / ≤500MB）",
)
async def upload_documents(
    knowledge_id: int,
    db: DbSession,
    user_id: CurrentUserId,
    files: Annotated[list[UploadFile], File(...)],
    ocr_enabled: bool = Query(False, alias="ocrEnabled"),
    force: str | None = Query(None),
) -> Result[list[DocumentOut]]:
    return Result.ok(
        await _svc(db, user_id).upload_batch(knowledge_id, files, ocr_enabled, force)
    )


@router.get(
    "/documents/{doc_id}", response_model=Result[DocumentOut], summary="文档详情"
)
async def get_document(doc_id: int, db: DbSession, user_id: CurrentUserId) -> Result[DocumentOut]:
    return Result.ok(await _svc(db, user_id).get_document(doc_id))


@router.delete(
    "/documents/{doc_id}",
    response_model=Result[None],
    summary="删除文档（级联向量 + 分块 + 文件）",
)
async def delete_document(doc_id: int, db: DbSession, user_id: CurrentUserId) -> Result[None]:
    await _svc(db, user_id).delete_document(doc_id)
    return Result.ok(None)


# ----------------------------------------------------------------------
# 入库 / 分块
# ----------------------------------------------------------------------


@router.post(
    "/documents/{doc_id}/ingest",
    response_model=Result[DocumentOut],
    summary="入库：分块 + 向量化（同步执行）",
)
async def ingest_document(
    doc_id: int, dto: IngestIn, db: DbSession, user_id: CurrentUserId
) -> Result[DocumentOut]:
    """**同步执行**，Java 是丢队列异步。

    大文件（500 页 PDF）会跑几分钟，HTTP 请求一直挂着。
    前端 axios 默认没超时（`request.js` 里没设 timeout），所以能用；
    但浏览器/反代可能 60s 断连。真要生产用应该改成任务表 + 轮询，
    当前是学习版，如实同步执行反而更容易看到每一步的耗时和失败原因。
    """
    return Result.ok(await _svc(db, user_id).ingest(doc_id, dto))


@router.post(
    "/documents/{doc_id}/preview-chunks",
    response_model=Result[list[str]],
    summary="预览分块结果（不入库）",
)
async def preview_chunks(
    doc_id: int, dto: IngestIn, db: DbSession, user_id: CurrentUserId
) -> Result[list[str]]:
    return Result.ok(await _svc(db, user_id).preview_chunks(doc_id, dto))


@router.get(
    "/documents/{doc_id}/chunks",
    response_model=Result[list[ChunkOut]],
    summary="分块列表（含向量化状态）",
)
async def list_chunks(doc_id: int, db: DbSession, user_id: CurrentUserId) -> Result[list[ChunkOut]]:
    return Result.ok(await _svc(db, user_id).list_chunks(doc_id))


# ----------------------------------------------------------------------
# 预览 / 下载
# ----------------------------------------------------------------------


@router.get("/documents/{doc_id}/preview", response_model=Result[str], summary="文档内容预览（markdown）")
async def preview_document(doc_id: int, db: DbSession, user_id: CurrentUserId) -> Result[str]:
    return Result.ok(await _svc(db, user_id).preview_document(doc_id))


@router.get(
    "/documents/{doc_id}/download",
    response_model=Result[DownloadInfoOut],
    summary="下载信息（预签名 URL）",
)
async def download_info(
    doc_id: int, db: DbSession, user_id: CurrentUserId
) -> Result[DownloadInfoOut]:
    return Result.ok(await _svc(db, user_id).download_info(doc_id))


@router.get("/documents/{doc_id}/download-file", summary="代理下载（强制 attachment）")
async def download_file(doc_id: int, db: DbSession, user_id: CurrentUserId) -> Response:
    data, filename, ctype = await _svc(db, user_id).download_stream(doc_id)
    # 文件名里有中文必须 RFC 5987 编码，否则部分浏览器拿到乱码
    from urllib.parse import quote

    disposition = f"attachment; filename*=UTF-8''{quote(filename)}"
    return Response(
        content=data,
        media_type=ctype,
        headers={"Content-Disposition": disposition},
    )


@router.get("/documents/{doc_id}/preview-file", summary="同源内联预览（供 iframe 渲染）")
async def preview_file(doc_id: int, db: DbSession, user_id: CurrentUserId) -> Response:
    """和 download-file 唯一的差别是 `inline` 而不是 `attachment`。

    前端 `getDocumentPreviewStreamUrl(docId)` 返回的就是这个路径，
    直接塞进 `<iframe :src>`。
    """
    data, filename, ctype = await _svc(db, user_id).download_stream(doc_id)
    from urllib.parse import quote

    disposition = f"inline; filename*=UTF-8''{quote(filename)}"
    return Response(
        content=data,
        media_type=ctype,
        headers={
            "Content-Disposition": disposition,
            # 图片/PDF 内联渲染要允许缓存，否则 iframe 每次重载都重新拉
            "Cache-Control": "max-age=3600",
        },
    )


@router.get("/images/{knowledge_id}/{filename}", summary="代理文档内嵌图片")
async def get_knowledge_image(
    knowledge_id: int, filename: str, db: DbSession, user_id: CurrentUserId
) -> Response:
    """`preview_document` 返回的 markdown 里图片地址已被重写成这个路径。

    **路径穿越防护在 service 层**（`serve_image` 里拦 `/` `\\` `..`）。
    这里返回 404 而不是 400 —— 对浏览器来说"图没了"就是 404，
    给 400 会在控制台刷一堆红字。
    """
    result = await _svc(db, user_id).serve_image(knowledge_id, filename)
    if result is None:
        return Response(status_code=404)
    data, ctype = result
    return Response(content=data, media_type=ctype, headers={"Cache-Control": "max-age=604800"})


# ----------------------------------------------------------------------
# URL 抓取
# ----------------------------------------------------------------------


@router.post(
    "/{knowledge_id}/documents/preview-url",
    response_model=Result[UrlPreviewOut],
    summary="预览 URL 正文（不入库）",
)
async def preview_url(
    knowledge_id: int,
    db: DbSession,
    user_id: CurrentUserId,
    url: str = Query(...),
) -> Result[UrlPreviewOut]:
    return Result.ok(await _svc(db, user_id).preview_url(knowledge_id, url))


@router.post(
    "/{knowledge_id}/documents/save-url",
    response_model=Result[DocumentOut],
    summary="保存已预览的 URL 正文",
)
async def save_url(
    knowledge_id: int, dto: UrlSaveIn, db: DbSession, user_id: CurrentUserId
) -> Result[DocumentOut]:
    return Result.ok(await _svc(db, user_id).save_url(knowledge_id, dto))


@router.post(
    "/{knowledge_id}/documents/fetch-url",
    response_model=Result[DocumentOut],
    summary="抓取 URL 并直接入库",
)
async def fetch_url(
    knowledge_id: int,
    db: DbSession,
    user_id: CurrentUserId,
    url: str = Query(...),
) -> Result[DocumentOut]:
    return Result.ok(await _svc(db, user_id).fetch_url(knowledge_id, url))


@router.post(
    "/documents/{doc_id}/sync-url",
    response_model=Result[DocumentOut],
    summary="重新抓取 URL 文档（内容变化才重新入库）",
)
async def sync_url_document(
    doc_id: int, db: DbSession, user_id: CurrentUserId
) -> Result[DocumentOut]:
    return Result.ok(await _svc(db, user_id).sync_url(doc_id))
