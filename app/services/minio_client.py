"""MinIO 对象存储客户端。

对照 Java：`MinioUtil`（上传 / 下载 / 预签名 / 删除）。

**和 Java 版的一个关键差异**：`minio` SDK 是**同步**的，
而 FastAPI 跑在 asyncio 事件循环上。直接在协程里调同步 HTTP 会**阻塞整个事件循环**
（其他请求全部卡住，不是变慢而是完全停摆）。
所以每个调用都套 `asyncio.to_thread`。

对照 Java 的 `@Async` / 线程池：Java 的 MinIO SDK 调用本身就发生在
Tomcat 工作线程上，天然不阻塞；Python 只有一个事件循环，必须手动丢线程。

## 路径规则（对齐 Java `MinioUtil.generatePath`）

    knowledge/{knowledgeId}/doc/{uuid}.{ext}       原始文件
    knowledge/{knowledgeId}/parsed/{uuid}.md       解析后的 markdown
    knowledge/{knowledgeId}/images/{filename}      文档内嵌图片

真实库里的 `document.file_path` 就是第一行的形态，
比如 `knowledge/2092054348604030977/doc/f7f7b49af8624e39a1ef7177507c3a2a.pdf`。
**不要改路径规则** —— 老文档的 `file_path` 已经落库，改了就读不到。
"""

from __future__ import annotations

import asyncio
import io
import logging
import uuid
from datetime import timedelta
from typing import BinaryIO

from app.core.config import settings

logger = logging.getLogger(__name__)


def _client():
    """延迟构造 MinIO 客户端（避免 import 时就要求配置齐全）。"""
    from minio import Minio

    endpoint = settings.minio_endpoint
    # 配置里是完整 URL（http://host:port），SDK 只要 host:port
    secure = endpoint.startswith("https://")
    host = endpoint.replace("https://", "").replace("http://", "").rstrip("/")
    return Minio(
        host,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=secure,
    )


async def _run(fn, *args, **kwargs):
    """把同步 SDK 调用丢到线程池，避免阻塞事件循环。"""
    return await asyncio.to_thread(fn, *args, **kwargs)


async def ensure_bucket() -> None:
    """确保 bucket 存在。启动时调一次即可（不存在就建）。"""
    def _ensure() -> None:
        cli = _client()
        if not cli.bucket_exists(settings.minio_bucket):
            cli.make_bucket(settings.minio_bucket)
            logger.info("[MinIO] 已创建 bucket: %s", settings.minio_bucket)

    await _run(_ensure)


async def health() -> bool:
    """探活。给 `system_config` 的健康检查用。"""
    try:
        await _run(lambda: _client().bucket_exists(settings.minio_bucket))
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("[MinIO] 探活失败: %s", exc)
        return False


def generate_path(knowledge_id: int, filename: str) -> str:
    """生成对象存储路径（对齐 Java `MinioUtil.generatePath`）。

    文件名换成 uuid —— 原始文件名可能有中文、空格、`#`、`?`，
    进 object key 后预签名 URL 会被转义得面目全非，浏览器下载名也乱。
    真实文件名存在 `document.name` 里，回下载时用它。
    """
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "bin"
    return f"knowledge/{knowledge_id}/doc/{uuid.uuid4().hex}.{ext}"


def generate_markdown_path(knowledge_id: int, file_path: str) -> str:
    """从原始文件路径推 markdown 路径：`/doc/xxx.pdf` → `/parsed/xxx.md`。"""
    stem = file_path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    return f"knowledge/{knowledge_id}/parsed/{stem}.md"


async def upload_bytes(data: bytes, object_path: str, content_type: str = "application/octet-stream") -> None:
    def _put() -> None:
        _client().put_object(
            settings.minio_bucket,
            object_path,
            io.BytesIO(data),
            length=len(data),
            content_type=content_type,
        )

    await _run(_put)


async def upload_string(text: str, object_path: str, content_type: str = "text/markdown") -> None:
    await upload_bytes(text.encode("utf-8"), object_path, content_type)


async def upload_stream(stream: BinaryIO, object_path: str, length: int, content_type: str) -> None:
    def _put() -> None:
        _client().put_object(
            settings.minio_bucket, object_path, stream, length=length, content_type=content_type
        )

    await _run(_put)


async def download_bytes(object_path: str) -> bytes:
    def _get() -> bytes:
        response = _client().get_object(settings.minio_bucket, object_path)
        try:
            return response.read()
        finally:
            response.close()
            response.release_conn()

    return await _run(_get)


async def download_text(object_path: str) -> str:
    return (await download_bytes(object_path)).decode("utf-8", errors="replace")


async def exists(object_path: str) -> bool:
    try:
        await _run(lambda: _client().stat_object(settings.minio_bucket, object_path))
        return True
    except Exception:  # noqa: BLE001
        return False


async def list_objects(prefix: str) -> list[str]:
    """列出 prefix 下的所有对象 key（递归）。对齐 Java `MinioUtil.listObjects`。

    Skill 的文件树、ZIP 导出、草稿提交全靠它 —— MinIO 没有"目录"，
    `skills/foo/` 下的文件是靠 key 前缀匹配出来的。
    返回的是**完整 key**（含 prefix），调用方自己截相对路径。
    """
    def _list() -> list[str]:
        return [
            obj.object_name
            for obj in _client().list_objects(settings.minio_bucket, prefix=prefix, recursive=True)
        ]

    return await _run(_list)


async def copy_object(src: str, dst: str) -> None:
    """服务端拷贝对象。

    MinIO **没有移动语义**（S3 协议本身就是 copy + delete）。
    Skill 的"草稿 → 正式目录"就是靠它：先逐个 copy，再删草稿。
    """
    def _copy() -> None:
        # minio>=7.2 的 `copy_object` 第三个参数**只接受 CopySource 对象**，
        # 传 `"/bucket/src"` 字符串会抛 `ValueError: source must be CopySource type`。
        #（老版本 SDK 收字符串，网上大部分示例还是老写法。）
        from minio.commonconfig import CopySource

        _client().copy_object(
            settings.minio_bucket,
            dst,
            CopySource(settings.minio_bucket, src),
        )

    await _run(_copy)


async def delete(object_path: str | None) -> None:
    """删除对象。**失败只记日志不抛** —— 删文档时对象没了不算错误，
    反而是目标状态。抛异常会让"删文档"整体失败，用户重试也删不掉。"""
    if not object_path:
        return
    try:
        await _run(lambda: _client().remove_object(settings.minio_bucket, object_path))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[MinIO] 删除失败（忽略）: %s err=%s", object_path, exc)


async def presigned_url(object_path: str, content_type: str | None = None, expires_hours: int = 1) -> str:
    """生成预签名下载 URL。

    `content_type` 要显式传：不传时浏览器拿到 `application/octet-stream`
    会**强制下载**而不是内联预览。PDF 想在线看必须传 `application/pdf`。
    """
    def _sign() -> str:
        kwargs: dict = {"expires": timedelta(hours=expires_hours)}
        if content_type:
            kwargs["response_headers"] = {"response-content-type": content_type}
        return _client().presigned_get_object(settings.minio_bucket, object_path, **kwargs)

    return await _run(_sign)


# 扩展名 → MIME。常见格式写死，未知的走 octet-stream。
_CONTENT_TYPES = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "doc": "application/msword",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "xls": "application/vnd.ms-excel",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "ppt": "application/vnd.ms-powerpoint",
    "txt": "text/plain; charset=utf-8",
    "md": "text/markdown; charset=utf-8",
    "csv": "text/csv; charset=utf-8",
    "json": "application/json",
    "html": "text/html; charset=utf-8",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "bmp": "image/bmp",
    "gif": "image/gif",
}


def public_url(object_path: str) -> str:
    """永久公开 URL：`{endpoint}/{bucket}/{path}`（Java `MinioUtil.getPublicUrl`）。

    头像、markdown 回读这类"URL 直接进库"的场景用它，不是预签名 URL ——
    预签名会过期，存进 users.avatar 之后图片就 403 了。
    """
    base = settings.minio_endpoint.rstrip("/")
    return f"{base}/{settings.minio_bucket}/{object_path.lstrip('/')}"


def content_type_of(file_type: str | None) -> str:
    return _CONTENT_TYPES.get((file_type or "").lower().lstrip("."), "application/octet-stream")
