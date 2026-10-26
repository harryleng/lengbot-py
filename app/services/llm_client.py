"""OpenAI 兼容协议的流式客户端（支持 function calling）。

为什么不用 openai 官方 SDK：lengbot 里的供应商五花八门（DeepSeek / 硅基流动 /
腾讯云 / DashScope compatible-mode），它们都实现了 `/chat/completions` 这个协议，
直接 httpx 发请求最省心，不用被 SDK 的版本和参数校验绑架。

对照 Java：相当于 `io.agentscope.extensions.model.openai.OpenAIChatModel` 那一层。

**流式 tool_calls 是本文件最需要看懂的地方**：
模型不是一次性返回完整的工具调用，而是分片吐：
    chunk1: {"tool_calls":[{"index":0,"id":"call_abc",
                            "function":{"name":"calculator","arguments":"{\"a\":"}}]}
    chunk2: {"tool_calls":[{"index":0,"function":{"arguments":"10,\"b\":5"}}]}
    chunk3: {"tool_calls":[{"index":0,"function":{"arguments":",\"operation\":\"add\"}"}}]}
必须按 `index` 累积拼接 arguments，否则拿到的是残缺 JSON。
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class LlmConfig:
    base_url: str
    api_key: str
    model: str
    max_tokens: int = 2048
    temperature: float | None = None
    top_p: float | None = None


@dataclass(slots=True)
class ToolCall:
    """模型请求调用某个工具。arguments 是**原始 JSON 字符串**（尚未解析）。"""

    id: str
    name: str
    arguments: str


@dataclass(slots=True)
class StreamEvent:
    """流式事件。三种 kind：

    - `text`：正文增量
    - `tool_calls`：本轮模型决定调工具（只在这一轮结束时产出一次）
    - `usage`：token 用量（通常最后一块才有）
    """

    kind: Literal["text", "tool_calls", "usage"]
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict[str, Any] | None = None


def _chat_completions_url(base_url: str) -> str:
    """拼出 /chat/completions 的完整地址。

    各家 base_url 写法不统一，实测（2026-10-24 逐个打过）：
        https://api.deepseek.com                             → 补 /v1（200）
        https://api.siliconflow.cn/v1                        → 已带 v1
        https://dashscope.aliyuncs.com/compatible-mode/v1    → 已带 v1
        https://api.lkeap.cloud.tencent.com/plan/v3          → **已带 v3**，再补 /v1 就 404

    所以判据不是"是不是 v1"，而是"路径末尾有没有版本号 /v<数字>"。
    腾讯云那家的 v3 就是它的兼容端点，key 只在 /plan/v3 下被授权
    （实测 /v1 返回 401 not authorized），拼错不只是 404，是根本调不通。
    """
    url = base_url.rstrip("/")
    if "/compatible-mode" in url or re.search(r"/v\d+$", url):
        return f"{url}/chat/completions"
    return f"{url}/v1/chat/completions"


async def stream_chat(
    cfg: LlmConfig,
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None = None,
    # 这个 timeout 是传给 httpx 的读超时，不是"本函数整体执行时限"。
    # 不适合用 asyncio.timeout 包裹 —— 流式响应本来就要跑很久，
    # 整体超时会把正常的长回答切断。
    timeout: float = 300.0,  # noqa: ASYNC109
) -> AsyncIterator[StreamEvent]:
    """流式调用，产出 StreamEvent。

    异常语义：调用方拿到异常后应该发 `[STATUS]{"type":"error"}` 再收尾，
    不要直接把堆栈吐给前端 —— 前端会把堆栈当正文渲染出来。
    """
    payload: dict[str, Any] = {
        "model": cfg.model,
        "messages": messages,
        "stream": True,
        "max_tokens": cfg.max_tokens,
        "stream_options": {"include_usage": True},
    }
    if tools:
        payload["tools"] = tools
        # 让模型自己决定是否调工具；强制调用会破坏普通问答。
        payload["tool_choice"] = "auto"
    if cfg.temperature is not None:
        payload["temperature"] = cfg.temperature
    if cfg.top_p is not None:
        payload["top_p"] = cfg.top_p

    headers = {
        "Authorization": f"Bearer {cfg.api_key}",
        "Content-Type": "application/json",
    }
    url = _chat_completions_url(cfg.base_url)

    # index → 累积中的工具调用（OpenAI 可能一次返回多个，用 index 区分）
    pending: dict[int, dict[str, str]] = {}

    async with (
        httpx.AsyncClient(timeout=timeout) as client,
        client.stream("POST", url, json=payload, headers=headers) as resp,
    ):
        if resp.status_code >= 400:
            body = (await resp.aread()).decode("utf-8", errors="replace")[:500]
            raise RuntimeError(f"模型接口返回 {resp.status_code}: {body}")

        async for line in resp.aiter_lines():
            if not line or not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                obj = json.loads(data)
            except json.JSONDecodeError:
                continue

            usage = obj.get("usage")
            choices = obj.get("choices") or []

            if not choices:
                if usage:
                    yield StreamEvent("usage", usage=usage)
                continue

            delta = choices[0].get("delta") or {}

            for tc in delta.get("tool_calls") or []:
                idx = int(tc.get("index") or 0)
                slot = pending.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["name"] = fn["name"]
                if fn.get("arguments"):
                    # 关键：arguments 是**增量字符串**，必须拼
                    slot["arguments"] += fn["arguments"]

            content = delta.get("content")
            if content:
                yield StreamEvent("text", text=content)

            if usage:
                yield StreamEvent("usage", usage=usage)

    if pending:
        calls = [
            ToolCall(
                id=slot["id"] or f"call_{idx}",
                name=slot["name"],
                arguments=slot["arguments"] or "{}",
            )
            for idx, slot in sorted(pending.items())
        ]
        # 过滤掉没有名字的脏数据（某些供应商会在流末尾发空 tool_calls）
        calls = [c for c in calls if c.name]
        if calls:
            yield StreamEvent("tool_calls", tool_calls=calls)
