"""逆向：库里那 161 条向量到底是用哪个模型生成的？

背景：库里 `model_name` 写的是 `Qwen/Qwen3-VL-Embedding-8B`，
但用这个模型重新 embed 同一段文本，和库里向量的余弦相似度只有 **0.012**（几乎正交）。
说明两者不在同一个向量空间 —— 而库里向量彼此是自洽的（相邻 chunk 相似度 0.90）。

本脚本拿同一个模型的若干变体逐个试，看哪个能对上（相似度应 > 0.99）。

跑法：
    uv run python -m scripts.rag_model_probe
"""

from __future__ import annotations

import asyncio
import math

import httpx
from sqlalchemy import text

from app.core.db import SessionLocal
from app.services.embedding_client import _resolve_provider

CHUNK_ID = 2108495203083931650

CANDIDATES: list[tuple[str, int | None]] = [
    ("Qwen/Qwen3-VL-Embedding-8B", 1024),
    ("Qwen/Qwen3-Embedding-8B", 1024),
    ("Qwen/Qwen3-Embedding-4B", 1024),
    ("Qwen/Qwen3-Embedding-0.6B", 1024),
    ("BAAI/bge-m3", None),
    ("BAAI/bge-large-zh-v1.5", None),
    ("netease-youdao/bce-embedding-base_v1", None),
    ("Pro/BAAI/bge-m3", None),
]


def cos(x: list[float], y: list[float]) -> float:
    if len(x) != len(y):
        return -1.0
    dot = sum(i * j for i, j in zip(x, y, strict=True))
    nx = math.sqrt(sum(i * i for i in x))
    ny = math.sqrt(sum(j * j for j in y))
    return dot / (nx * ny) if nx and ny else 0.0


def parse_pgvector(raw: str) -> list[float]:
    return [float(x) for x in raw.strip("[]").split(",")]


async def main() -> None:
    async with SessionLocal() as db:
        row = (
            await db.execute(
                text(
                    "SELECT c.content, e.vector::text FROM chunk c "
                    "JOIN embedding e ON e.chunk_id = c.id WHERE c.id = :cid"
                ),
                {"cid": CHUNK_ID},
            )
        ).first()
    if row is None:
        print("找不到样本")
        return
    content, vec_text = str(row[0]), row[1]
    stored = parse_pgvector(vec_text)
    print(f"库里向量长度={len(stored)}，样本内容前 40 字: {content[:40]!r}\n")

    base_url, api_key = await _resolve_provider()
    url = f"{base_url.rstrip('/')}/embeddings"

    print(f"{'模型':<40}{'维度':<8}{'相似度':<12}结论")
    print("-" * 78)
    async with httpx.AsyncClient(timeout=120) as client:
        for model, dims in CANDIDATES:
            payload: dict = {"model": model, "input": [content], "encoding_format": "float"}
            if dims:
                payload["dimensions"] = dims
            try:
                resp = await client.post(
                    url,
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    json=payload,
                )
            except Exception as exc:  # noqa: BLE001
                print(f"{model:<40}{'-':<8}{'-':<12}请求异常 {exc}")
                continue
            if resp.status_code != 200:
                msg = resp.text[:60].replace("\n", " ")
                print(f"{model:<40}{'-':<8}{'-':<12}HTTP {resp.status_code} {msg}")
                continue
            vec = resp.json()["data"][0]["embedding"]
            sim = cos(vec, stored)
            verdict = "✅ 对上了！" if sim > 0.99 else ("接近" if sim > 0.8 else "")
            print(f"{model:<40}{len(vec):<8}{sim:<12.6f}{verdict}")


if __name__ == "__main__":
    asyncio.run(main())
