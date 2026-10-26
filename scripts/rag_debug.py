"""诊断：为什么数据都在、却检索不到？

假设：入库时的 1024 维和查询时的 1024 维**不是同一种降维方式**。
Qwen3 系列是 Matryoshka 模型，降维有两条路：
    a) API 的 `dimensions=1024` → 模型内部做 MRL 投影（会重新归一化）
    b) 拿 4096 维后自己截前 1024 维 → 没有重新归一化，方向偏了

两者算出的余弦相似度可能差很远，表现为"检索结果完全不相关或为空"。
本脚本用同一段文本验证：哪种方式生成的向量和库里存的最接近（相似度应接近 1.0）。

跑法：
    uv run python -m scripts.rag_debug
"""

from __future__ import annotations

import asyncio
import math

import httpx
from sqlalchemy import text

from app.core.config import settings
from app.core.db import SessionLocal
from app.services.embedding_client import _resolve_provider

CHUNK_ID = 2108495203083931650


def cos(x: list[float], y: list[float]) -> float:
    dot = sum(i * j for i, j in zip(x, y, strict=True))
    nx = math.sqrt(sum(i * i for i in x))
    ny = math.sqrt(sum(j * j for j in y))
    return dot / (nx * ny) if nx and ny else 0.0


def normalize(v: list[float]) -> list[float]:
    n = math.sqrt(sum(i * i for i in v))
    return [x / n for x in v] if n else v


def parse_pgvector(raw: str) -> list[float]:
    return [float(x) for x in raw.strip("[]").split(",")]


async def embed_raw(texts: list[str], dimensions: int | None) -> list[list[float]]:
    base_url, api_key = await _resolve_provider()
    payload: dict = {"model": settings.embedding_model, "input": texts, "encoding_format": "float"}
    if dimensions:
        payload["dimensions"] = dimensions
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{base_url.rstrip('/')}/embeddings",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
        )
    resp.raise_for_status()
    return [d["embedding"] for d in resp.json()["data"]]


async def main() -> None:
    async with SessionLocal() as db:
        row = (
            await db.execute(
                text(
                    "SELECT c.content, e.vector::text AS vec, e.model_name, e.dimension "
                    "FROM chunk c JOIN embedding e ON e.chunk_id = c.id WHERE c.id = :cid"
                ),
                {"cid": CHUNK_ID},
            )
        ).first()
    if row is None:
        print(f"chunk {CHUNK_ID} 没有 embedding")
        return

    content, vec_text, model_name, dimension = row
    stored = parse_pgvector(vec_text)
    text_value = str(content)
    print(f"chunk 内容前 60 字: {text_value[:60]!r}")
    print(f"库里向量: model={model_name} dimension={dimension} 实际长度={len(stored)}")
    print(f"库里向量的模长: {math.sqrt(sum(x * x for x in stored)):.4f}（归一化后应接近 1）")
    print()

    print("=" * 70)
    print("三种方式生成查询向量，与库里存的算余弦相似度")
    print("=" * 70)

    # a) 原生 dimensions=1024
    v_1024 = (await embed_raw([text_value], 1024))[0]
    print(
        f"a) dimensions=1024        长度={len(v_1024):<5} 模长={math.sqrt(sum(x * x for x in v_1024)):.4f} "
        f"相似度={cos(v_1024, stored):.6f}"
    )

    # b) 4096 截断前 1024（不归一化）
    v_full = (await embed_raw([text_value], None))[0]
    v_trunc = v_full[:1024]
    print(
        f"b) 4096 截断前1024        长度={len(v_trunc):<5} 模长={math.sqrt(sum(x * x for x in v_trunc)):.4f} "
        f"相似度={cos(v_trunc, stored):.6f}"
    )

    # c) 4096 截断后重新归一化
    v_trunc_norm = normalize(v_trunc)
    norm_len = math.sqrt(sum(x * x for x in v_trunc_norm))
    print(
        f"c) 4096 截断+归一化       长度={len(v_trunc_norm):<5} 模长={norm_len:.4f} "
        f"相似度={cos(v_trunc_norm, stored):.6f}"
    )

    print()
    best = max(
        [("a", cos(v_1024, stored)), ("b", cos(v_trunc, stored)), ("c", cos(v_trunc_norm, stored))],
        key=lambda kv: kv[1],
    )
    print(f"最接近的是 {best[0]}，相似度 {best[1]:.6f}")
    if best[1] > 0.99:
        print("→ 说明入库用的就是这种方式，检索维度不匹配的原因找到了")
    else:
        print("→ 三种都不匹配，可能入库用了别的模型/别的参数，需要查入库代码")


if __name__ == "__main__":
    asyncio.run(main())
