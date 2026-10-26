"""Embedding 连通性诊断：确认供应商、维度、以及库里存的是不是同一个模型。

为什么需要这个脚本：向量检索最容易出的问题是**查询用的模型和入库用的模型不一致**，
表现是"检索结果完全不相关"，但不会报任何错（维度对了就能算距离）。
所以先确认三件事：API 能通、维度对得上、model_name 和库里一致。
"""

from __future__ import annotations

import asyncio

import httpx
from sqlalchemy import text

from app.core.db import SessionLocal

EMBEDDING_PROVIDER_ID = 2094705335903604737  # 嵌入模型-千问（SiliconFlow）
EMBEDDING_MODEL = "Qwen/Qwen3-VL-Embedding-8B"


async def main() -> None:
    async with SessionLocal() as db:
        row = (
            await db.execute(
                text("SELECT base_url, api_key FROM model_provider WHERE id = :id"),
                {"id": EMBEDDING_PROVIDER_ID},
            )
        ).first()
        if row is None:
            print("找不到 embedding provider")
            return
        base_url, api_key = row
        print(f"provider base_url = {base_url}")
        print(f"model = {EMBEDDING_MODEL}")

        stored = (
            await db.execute(
                text("SELECT model_name, dimension, count(*) FROM embedding GROUP BY 1,2 ORDER BY 3 DESC")
            )
        ).all()
        print("\n库里已存的向量:")
        for name, dim, cnt in stored:
            same = "✅ 一致" if name == EMBEDDING_MODEL else "⚠️ 不同模型"
            print(f"  {name:<32} dim={dim:<6} count={cnt:<5} {same}")

    url = f"{base_url.rstrip('/')}/embeddings"
    payload = {"model": EMBEDDING_MODEL, "input": ["向量检索是什么", "如何做红烧肉"]}
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            url,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
        )
    print(f"\nPOST {url} → {resp.status_code}")
    if resp.status_code != 200:
        print(resp.text[:500])
        return

    data = resp.json()["data"]
    dims = [len(d["embedding"]) for d in data]
    print(f"返回条数={len(data)} 维度={dims}")

    # 顺带验证这两个句子的相似度是否合理（同一模型下"向量检索"两句应该更像）
    import math

    a, b = (d["embedding"] for d in data)

    def cos(x: list[float], y: list[float]) -> float:
        dot = sum(i * j for i, j in zip(x, y, strict=True))
        return dot / (math.sqrt(sum(i * i for i in x)) * math.sqrt(sum(j * j for j in y)))

    print(f"两条测试文本的余弦相似度 = {cos(a, b):.4f}（应该偏低，说明向量有区分度）")


if __name__ == "__main__":
    asyncio.run(main())
