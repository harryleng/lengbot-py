"""工具执行器：解析参数 → Pydantic 校验 → 调 handler → 截断结果。

**最重要的一条设计**：工具执行失败**不抛异常**，而是把错误信息当成工具结果
返回给模型，让它自己纠正。

对比一下两种做法：
    A. 抛异常 → 整个对话中断，用户看到"模型调用失败"（糟糕）
    B. 返回 {"ok": false, "error": "path 参数缺失"} → 模型看到后重试一次就能补上（正确）

Agent 的健壮性很大程度就来自 B。唯一的例外是"取消"，那个必须真的中断。

对照 Java：`ToolNodeProcessor` / `ToolExecutor` 那一层。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import ValidationError

from app.tools.base import ToolSpec

logger = logging.getLogger(__name__)

# 工具结果进上下文时的截断长度。
# 对齐 Java：`summaryToolResultTokenLimit` 默认 500 token × 4 ≈ 2000 字符。
# 为什么必须截：一次 web_search 或读文件可能返回几万字符，
# 几条下来就把上下文窗口撑爆，而且模型根本读不完。
MAX_TOOL_RESULT_CHARS = 2000


def _truncate(text: str, limit: int = MAX_TOOL_RESULT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[已截断，原文共 {len(text)} 字符]"


def _error(message: str) -> str:
    return json.dumps({"ok": False, "error": message}, ensure_ascii=False)


async def execute_tool_call(
    spec: ToolSpec | None,
    tool_name: str,
    arguments_json: str,
) -> tuple[str, bool]:
    """执行一次工具调用。

    @return (结果文本, 是否成功)
    """
    if spec is None:
        return _error(f"未知工具: {tool_name}（该工具尚未实现，或未在此 Agent 上启用）"), False

    # 1. 解析参数 JSON
    raw = arguments_json.strip() or "{}"
    try:
        args: dict[str, Any] = json.loads(raw)
        if not isinstance(args, dict):
            return _error(f"工具参数必须是 JSON 对象，实际收到 {type(args).__name__}"), False
    except json.JSONDecodeError as exc:
        return _error(f"工具参数不是合法 JSON: {exc}. 收到的原文: {raw[:200]}"), False

    # 2. Pydantic 校验（enum / 类型 / 必填都在这一步拦住）
    try:
        params = spec.params_model(**args)
    except ValidationError as exc:
        detail = "; ".join(f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in exc.errors())
        return _error(f"参数校验失败: {detail}"), False

    # 3. 执行
    try:
        result = await spec.handler(**params.model_dump())
    except Exception as exc:  # noqa: BLE001
        logger.warning("工具 %s 执行异常: %s", tool_name, exc)
        return _error(f"工具执行异常: {type(exc).__name__}: {exc}"), False

    return result if result is not None else "", True


def result_for_context(result: str, *, tool_name: str = "") -> str:
    """工具结果送进上下文前的处理。

    为什么单独一个函数：推给前端的（tool_result 事件）和送进模型的
    （role=tool 消息）**截断策略可以不同** —— 前端展示要尽量完整，
    模型上下文要尽量省。目前两者都用 2000，将来要调只改这里。
    """
    if not result:
        return _error(f"工具 {tool_name} 没有返回内容")
    return _truncate(result)
