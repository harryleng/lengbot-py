"""Embedding 客户端。

## 一个必须知道的坑：元数据是错的

库里 `embedding.model_name` 字段写的是 `Qwen/Qwen3-VL-Embedding-8B`，
但实测反推（`scripts/rag_model_probe.py`）证明那 161 条向量是
**`BAAI/bge-large-zh-v1.5`** 生成的 —— 余弦相似度 **1.000000**，
而用 Qwen3-VL 重新 embed 同一段文本只有 **0.012**（几乎正交）。

后果：任何"按 model_name 选查询模型"的逻辑都会查不到任何东西，
而且**不会报错**，只是返回空列表。这是最坏的一类 bug —— 静默失败。

三个必须对齐的东西，错一个就检索不到：

1. **模型**：以实测为准（bge-large-zh-v1.5），不要信元数据字段
2. **维度**：bge 原生 1024，不需要 `dimensions` 参数；
   如果换成 Qwen3 系列（默认 4096），必须传 `dimensions=1024`，否则 `<=>` 直接报维度不匹配
3. **归一化**：pgvector 的 `<=>` 是余弦距离，API 返回的向量已归一化（模长 1.0），
   自己截断降维后必须重新归一化，否则方向会偏

对照 Java：`EmbeddingService` / `EmbeddingMapper` 那一层。
"""

from __future__ import annotations

import asyncio
import logging

import httpx
from sqlalchemy import text

from app.core.config import settings
from app.core.db import SessionLocal

logger = logging.getLogger(__name__)

_provider_cache: tuple[str, str] | None = None


async def _resolve_provider() -> tuple[str, str]:
    global _provider_cache
    if _provider_cache is not None:
        return _provider_cache
    async with SessionLocal() as db:
        row = (
            await db.execute(
                text("SELECT base_url, api_key FROM model_provider WHERE id = :id"),
                {"id": settings.embedding_provider_id},
            )
        ).first()
    if row is None or not row[0] or not row[1]:
        raise RuntimeError(f"embedding provider {settings.embedding_provider_id} 未配置 base_url/api_key")
    _provider_cache = (row[0], row[1])
    return _provider_cache


def clear_provider_cache() -> None:
    """改了 provider 配置后调用（否则要重启）。"""
    global _provider_cache
    _provider_cache = None


# ⚠️ 供应商会**间歇性**返回 400（code 20015 "The parameter is invalid"）。
#
# 这个回报极具误导性：同样的字符串上一次失败、下一次就成功（实测
# '三十天内完成报销流程。' 连测两次，一次 ERR 一次 OK），
# 而且**短文本也会中**（只有 11 个字，远远没到 512 token 上限）。
# 结论：这不是内容问题，是**突发 QPS 限流用了错误的错误码**。
#
# 后果很实际：跑文档入库时连续几十次调用，随机有一批被打回，
# 文档变成 failed，用户以为是自己的文档有问题。
# 所以这里做指数退避重试；最多 3 次总用时 < 5s，在用户可接受范围内。
EMBED_MAX_RETRIES = 3
EMBED_BACKOFF_BASE = 0.8


async def embed(texts: list[str]) -> list[list[float]]:
    """把一批文本向量化。

    对**幂等的 GET-like 调用**做重试是安全的：同一个输入会得到同一个向量，
    重复请求只是多花一次配额，不会写脏数据。
    """
    if not texts:
        return []
    base_url, api_key = await _resolve_provider()
    url = f"{base_url.rstrip('/')}/embeddings"
    payload: dict = {
        "model": settings.embedding_model,
        "input": texts,
        "encoding_format": "float",
    }
    if settings.embedding_use_dimensions:
        payload["dimensions"] = settings.embedding_dimension

    last_err: Exception | None = None
    async with httpx.AsyncClient(timeout=60) as client:
        for attempt in range(EMBED_MAX_RETRIES):
            try:
                resp = await client.post(
                    url,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
            except httpx.HTTPError as exc:
                # 网络层错误同样重试：远程库/网关偶发断连之前出现过（DNS 解析到废 IP）
                last_err = exc
                resp = None
            if resp is not None and resp.status_code == 200:
                return _parse_embedding_response(resp.json())
            if resp is not None:
                last_err = RuntimeError(
                    f"embedding 接口返回 {resp.status_code}: {resp.text[:300]}"
                )
            if attempt < EMBED_MAX_RETRIES - 1:
                wait = EMBED_BACKOFF_BASE * (2**attempt)
                logger.warning(
                    "[embedding] 第 %s 次失败，%.1fs 后重试：%s",
                    attempt + 1,
                    wait,
                    str(last_err)[:160],
                )
                await asyncio.sleep(wait)

    raise last_err or RuntimeError("embedding 调用失败")


def _parse_embedding_response(data: dict) -> list[list[float]]:
    vectors = [item["embedding"] for item in sorted(data.get("data") or [], key=lambda d: d.get("index", 0))]
    for v in vectors:
        if len(v) != settings.embedding_dimension:
            raise RuntimeError(
                f"embedding 维度异常：期望 {settings.embedding_dimension}，实际 {len(v)}。"
                f"检查模型的 dimensions 参数是否和库里的向量维度一致。"
            )
    return vectors


async def embed_query(text_value: str) -> list[float]:
    vectors = await embed([text_value])
    return vectors[0]


def to_pgvector(vector: list[float]) -> str:
    """转成 pgvector 能接受的字符串字面量：'[0.1,0.2,...]'。

    为什么要转：asyncpg 不认识 pgvector 类型，
    只能作为字符串传进去再由 SQL 里 `CAST(:vec AS vector)` 转换。
    """
    return "[" + ",".join(f"{x:.8f}" for x in vector) + "]"
