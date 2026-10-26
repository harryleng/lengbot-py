"""探测 LLM 供应商当前是否可用（主要看 429 限流是否解除）。

用法：python -m scripts.llm_probe
"""
from __future__ import annotations

import asyncio

import httpx
from sqlalchemy import text

from app.core.db import SessionLocal
from app.services.llm_client import _chat_completions_url


async def main() -> None:
    async with SessionLocal() as s:
        prs = (
            await s.execute(
                text(
                    "SELECT p.id, p.name, p.base_url, p.api_key, p.config, m.model_id "
                    "FROM model_provider p JOIN model m ON m.provider_id = p.id "
                    "WHERE p.deleted = 0 AND m.deleted = 0 AND m.type = 'llm' ORDER BY p.id"
                )
            )
        ).mappings().all()

    for r in prs:
        cfg = r["config"] or {}
        model = cfg.get("modelId") or r["model_id"]
        print(f"\n[{r['id']}] {r['name']}  model={model}  base={r['base_url']}")
        if not r["api_key"]:
            print("   无 api_key，跳过")
            continue
        url = _chat_completions_url(r["base_url"])
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": "回复两个字：可用"}],
            "max_tokens": 32,
            "stream": False,
        }
        try:
            async with httpx.AsyncClient(timeout=60) as c:
                resp = await c.post(
                    url, json=payload, headers={"Authorization": f"Bearer {r['api_key']}"}
                )
            print(f"   HTTP {resp.status_code}: {resp.text[:260]}")
        except Exception as e:  # noqa: BLE001
            print(f"   异常: {type(e).__name__}: {e}")


if __name__ == "__main__":
    asyncio.run(main())
