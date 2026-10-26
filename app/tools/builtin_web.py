"""联网搜索工具 `web_search` —— 对齐 Java 的 `WebSearchTool`（Tavily API）。

Java 侧 key 从 `lengbot.tavily.api-key` 读，未配置时返回一句中文提示。
Python 版改成读环境变量 `TAVILY_API_KEY`（见 `.env.example`），未配置时同样
返回 `{success: false, error: ...}` 而不是抛异常 —— 让模型知道"工具不可用"，
它会转而用自己的知识回答，而不是让整个对话失败。

返回结构照抄 Java：
    {query, answer, results: [{title, url, content, score?, rawContent?}], total}
"""

from __future__ import annotations

import json
import logging
from typing import Annotated

import httpx

from app.core.config import settings
from app.tools.base import field, tool

logger = logging.getLogger(__name__)

TAVILY_URL = "https://api.tavily.com/search"
# 单条网页正文塞进上下文的上限。Tavily 的 raw_content 动辄几万字，
# 5 条全塞进去就是十几万 token —— 只留前 2000 字做摘要足够。
MAX_RAW_CONTENT = 2000


def _err(message: str) -> str:
    return json.dumps({"success": False, "error": message}, ensure_ascii=False)


@tool(
    "web_search",
    "联网搜索互联网获取最新信息。当用户问题涉及时事、新闻、股票价格、天气、"
    "或任何需要最新数据的问题时调用此工具。",
)
async def web_search(
    query: Annotated[str, field("搜索关键词")],
    maxResults: Annotated[int, field("返回结果数量（默认5，最大10）")],
) -> str:
    api_key = (settings.tavily_api_key or "").strip()
    if not api_key:
        return _err(
            "联网搜索未配置：请在 lengbot-py 的 .env 里设置 TAVILY_API_KEY"
            "（申请地址 https://app.tavily.com）"
        )

    limit = min(max(int(maxResults or 5), 1), 10)

    body = {
        "query": query,
        "max_results": limit,
        "search_depth": "basic",
        "include_answer": True,
        "include_raw_content": True,
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                TAVILY_URL,
                json=body,
                headers={"Authorization": f"Bearer {api_key}"},
            )
    except httpx.HTTPError as exc:
        logger.warning("web_search 请求失败: %s", exc)
        return _err(f"搜索请求失败：{exc}")

    if resp.status_code != 200:
        logger.warning("web_search HTTP %s: %s", resp.status_code, resp.text[:300])
        return _err(f"搜索接口返回 HTTP {resp.status_code}")

    try:
        root = resp.json()
    except ValueError:
        return _err("搜索接口返回的不是合法 JSON")

    answer = root.get("answer")
    items: list[dict] = []
    for item in root.get("results") or []:
        entry = {
            "title": item.get("title") or "无标题",
            "url": item.get("url") or "",
            "content": item.get("content") or "",
        }
        if item.get("score") is not None:
            entry["score"] = item["score"]
        raw = item.get("raw_content")
        if raw and str(raw).strip():
            text = str(raw).strip()
            if len(text) > MAX_RAW_CONTENT:
                text = text[:MAX_RAW_CONTENT] + "…[已截断]"
            entry["rawContent"] = text
        items.append(entry)

    return json.dumps(
        {
            "success": True,
            "query": query,
            "answer": answer if isinstance(answer, str) and answer.strip() else None,
            "results": items,
            "total": len(items),
        },
        ensure_ascii=False,
    )


WEB_TOOLS = [web_search]
