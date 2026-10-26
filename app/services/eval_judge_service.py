"""LLM-as-judge：调用评估器给一条样本打分。

对照 Java `EvalChatServiceImpl.callEvaluator` + `callPrompt`。

### 三个必须照抄的行为

**1. 追加固定的系统指令。**
评估器的 prompt 只写"请评估相似度…"，没告诉模型怎么**输出**。
Java 在末尾硬拼一段：

    「按照Json格式返回评估结果。例如
     {"score":"0.85","reason":"..."}
     只返回Json字符串，不要有其他任何内容。」

少了这段，模型会返回"我认为这两句话相似度大概是 0.8 分，因为…"，
正则能抓出来但成功率不稳定。带上之后基本是纯 JSON。

**2. 解析要能从 markdown 代码块里抠 JSON。**
模型很爱返回：
    ```json
    {"score": 0.85, "reason": "..."}
    ```
    直接 `json.loads` 会炸。Java 的 `extractJson` 做了两层：
    先去 ``` 围栏，再取第一个 `{` 到最后一个 `}`。

**3. 解析失败不抛异常，返回 0 分 + 原因。**
实验跑 20 条，中间一条解析失败就整批崩掉，用户白等。Java 也是兜底成
`score=0, reason="评分结果解析失败: <原文>"` —— 让用户能看到模型到底吐了什么。

### score 归一化

模型可能返回 `0.85` / `"0.85"` / `85` / `8.5`（当百分制）。Java 只用
`BigDecimal` 接，所以 `85` 会被原样存成 85 分 —— 在 0~1 的量纲里看着像 bug。
这里**只做 0~1 区间的宽松归一**：>1 且 <=100 时除以 100，再夹到 0~1。
超出 100 或负数直接夹到边界（宁可给 0/1，也不给一个 85 的分数污染平均分）。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.eval import EvalScoreOut
from app.services.llm_client import LlmConfig, stream_chat

logger = logging.getLogger(__name__)

# 追加到评估器 prompt 末尾的固定指令（逐字对齐 Java `EVALUATOR_SYSTEM_PROMPT`）
EVALUATOR_SYSTEM_PROMPT = (
    "\n\n按照Json格式返回评估结果。例如\n"
    '{"score":"0.85","reason":"回答基本正确，准确回答了用户关于人工智能的问题。"}\n'
    "只返回Json字符串，不要有其他任何内容。"
)

_SCORE_MIN = 0.0
_SCORE_MAX = 1.0


def _bad(msg: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=msg)


def extract_json(content: str | None) -> str:
    """从模型输出里抠 JSON（兼容 markdown 围栏）。"""
    if not content:
        return "{}"
    text = content.strip()
    if text.startswith("```"):
        first_nl = text.find("\n")
        last_fence = text.rfind("```")
        if first_nl > 0 and last_fence > first_nl:
            text = text[first_nl + 1 : last_fence].strip()
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return text[start : end + 1]
    return text


def normalize_score(raw: Any) -> float:
    """把模型给的分数夹到 0~1。

    兼容 `0.85` / `"0.85"` / `85`（百分制）/ `"8.5"`（十分制）。
    规则：
    - 无法转数字 → 0.0
    - 1 < x <= 10 → 除以 10（十分制）
    - 10 < x <= 100 → 除以 100（百分制）
    - 其余夹到 [0, 1]
    """
    try:
        value = float(raw)
    except (TypeError, ValueError):
        # 可能是 `"0.85分"` 这种带单位的
        match = re.search(r"-?\d+(?:\.\d+)?", str(raw or ""))
        if not match:
            return 0.0
        try:
            value = float(match.group())
        except ValueError:
            return 0.0

    if 1 < value <= 10:
        value = value / 10
    elif 10 < value <= 100:
        value = value / 100
    return max(_SCORE_MIN, min(_SCORE_MAX, value))


def parse_score_result(content: str | None) -> EvalScoreOut:
    """解析成 `{score, reason}`。**解析失败不抛异常**（见模块 docstring）。"""
    payload = extract_json(content)
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        logger.warning("评估结果不是合法 JSON：%s", str(content)[:200])
        return EvalScoreOut(score=0.0, reason=f"评分结果解析失败: {content or ''}")
    if not isinstance(data, dict):
        return EvalScoreOut(score=0.0, reason=f"评分结果解析失败: {content or ''}")
    return EvalScoreOut(
        score=normalize_score(data.get("score")),
        reason=str(data.get("reason") or "") or None,
    )


def render_template(template: str | None, variables: dict[str, Any] | str | None) -> str:
    """把 `{{name}}` 替换成变量值。

    Java 只做**字符串替换**，不解析嵌套。这里照抄，但多了两个保护：
    - 变量值是 None → 空串（`str(None)` 会渲染成 "None"，很难看）
    - 变量值是 dict/list → JSON 序列化（直接 str() 会得到 Python repr，
      单引号在 JSON 语境里是错的）

    变量 map 传字符串时尝试 parse，失败就当没有变量（不报错）。
    """
    if not template:
        return ""
    if isinstance(variables, str):
        try:
            variables = json.loads(variables) if variables.strip() else {}
        except json.JSONDecodeError:
            logger.warning("变量 map 不是合法 JSON，跳过替换")
            return template
    if not isinstance(variables, dict):
        return template

    result = template
    for key, value in variables.items():
        if value is None:
            text = ""
        elif isinstance(value, (dict, list)):
            text = json.dumps(value, ensure_ascii=False)
        else:
            text = str(value)
        result = result.replace("{{" + str(key) + "}}", text)
    return result


class EvalJudgeService:
    """评估器调用 + 被测对象的 LLM 调用。

    `db` 只用于取 provider 配置（base_url / api_key），不写库。
    """

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # ------------------------------------------------------------------
    async def _provider(self, provider_id: Any) -> tuple[str, str]:
        from app.workflow.nodes import _resolve_provider

        try:
            pid = int(str(provider_id))
        except (TypeError, ValueError) as exc:
            raise _bad(f"providerId 不是合法数字：{provider_id}") from exc
        resolved = await _resolve_provider(pid)
        if not resolved:
            raise _bad(f"模型供应商 {pid} 不存在或未配置 API Key")
        return resolved

    @staticmethod
    def _parse_config(model_config: Any) -> dict[str, Any]:
        if isinstance(model_config, dict):
            return model_config
        if isinstance(model_config, str) and model_config.strip():
            try:
                parsed = json.loads(model_config)
            except json.JSONDecodeError:
                return {}
            return parsed if isinstance(parsed, dict) else {}
        return {}

    async def _complete(self, model_config: Any, prompt: str) -> str:
        """一次性调用（非流式语义，但底层用 stream_chat 拼完整结果）。

        为什么不复用 `stream_chat` 的流式子集：评测不需要逐字展示，
        但它内部已经把 SSE 解析、usage、异常都处理好了，拼起来最省事，
        也避免再写一份 HTTP 客户端。
        """
        cfg_raw = self._parse_config(model_config)
        base_url, api_key = await self._provider(
            cfg_raw.get("providerId") or cfg_raw.get("modelId")
        )
        model = str(cfg_raw.get("modelId") or "")
        if not model:
            raise _bad("模型配置缺少 modelId")

        cfg = LlmConfig(
            base_url=base_url,
            api_key=api_key,
            model=model,
            max_tokens=int(cfg_raw.get("maxTokens") or 1024),
            temperature=cfg_raw.get("temperature"),
            top_p=cfg_raw.get("topP"),
        )
        parts: list[str] = []
        async for ev in stream_chat(cfg, [{"role": "user", "content": prompt}]):
            if ev.kind == "text":
                parts.append(ev.text)
        return "".join(parts)

    # ------------------------------------------------------------------
    async def judge(
        self, model_config: Any, prompt_template: str | None, variables: Any
    ) -> EvalScoreOut:
        """评估器打分：填变量 → 追加输出指令 → 调用 → 解析。"""
        rendered = render_template(prompt_template, variables) + EVALUATOR_SYSTEM_PROMPT
        content = await self._complete(model_config, rendered)
        return parse_score_result(content)

    async def generate(
        self, model_config: Any, prompt_template: str | None, variables: Any
    ) -> str:
        """被测对象产出（把 prompt 跑一遍拿回答）。

        **不追加系统指令** —— 这里要的是「用户看到的那段回答」原文，
        追加了 "只返回 JSON" 就把被测对象变成另一个评估器了。
        """
        rendered = render_template(prompt_template, variables)
        return await self._complete(model_config, rendered)
