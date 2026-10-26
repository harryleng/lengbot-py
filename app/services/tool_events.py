"""工具事件的构造、截断与瘦身规则。

这个模块存在的理由：**同一份工具结果要往两个地方写，两处规则不同**。

    message.tool_events（JSONB）  前端渲染工具卡片用，历史列表里还要再瘦一层
    tool_calls.tool_output（TEXT）完整原文，前端点「查看结果」时按 id 回查

三条截断规则，阈值都抄自 Java，别自己改：

| 场景 | 阈值 | Java 出处 |
|---|---|---|
| SSE 推送 `tool_result.result` | 纯文本 2000 字，**JSON 不截** | `ToolEventGenerator.truncateForSse` |
| 历史列表 `tool_events` 的 result | 500 字内保留，超了删正文留 `resultTotalLength` | `MessageServiceImpl` |
| 大参数工具（写文件）的 args | 单字段超 120 字 → 前 20 字 + 标记 | `ToolArgsSanitizer` |

**「JSON 不截」不是优化，是正确性要求**：前端拿到 `result` 会 `JSON.parse`，
把 4000 字的 JSON 硬切一半会得到非法 JSON，前端解析抛异常后整个工具卡片渲染失败。
纯文本没有这个问题，才允许截。

对照 Java：`ToolEventGenerator` + `ToolArgsSanitizer` + `MessageServiceImpl` 里
散落的三段逻辑，这里合并成一个模块，一眼能看出三个阈值的关系。
"""

from __future__ import annotations

import json
from typing import Any

# SSE 推送时纯文本结果的截断长度（Java: ToolEventGenerator.truncateForSse）
SSE_TEXT_RESULT_LIMIT = 2000

# 历史列表瘦身阈值：≤ 此长度原样保留，> 则删正文只留长度（Java: TOOL_RESULT_INLINE_LIMIT）
INLINE_RESULT_LIMIT = 500

# 大参数字段的压缩阈值（Java: ToolArgsSanitizer.HISTORY_ARG_MAX_LENGTH）
ARG_COMPACT_LIMIT = 120

# 需要压缩参数的工具：只压「写文件类」，它们的 content 动辄几千字
# 注意：Java 侧叫 sandbox_write_file / sandbox_append_file，
# Python 侧这批工具改名成 local_*，两套名字都放进来以防库里有老数据。
LARGE_CONTENT_TOOLS = frozenset(
    {
        "local_write_file",
        "local_append_file",
        "local_write_file_binary",
        "sandbox_write_file",
        "sandbox_append_file",
        "sandbox_write_file_binary",
    }
)

TRUNCATED_ARG_MARK = "...(argument truncated for context view)"


def looks_like_json(text: str) -> bool:
    """粗判是不是 JSON —— 只看首字符，和 Java 的判据一致（不尝试 parse）。

    为什么不做完整 parse：结果可能本来就是半截非法 JSON（工具报错时常见），
    完整 parse 会失败然后走截断，反而把"非法但完整"的内容也切了。
    Java 的判断同样是 `startsWith("{") || startsWith("[")`。
    """
    stripped = text.lstrip()
    return stripped.startswith("{") or stripped.startswith("[")


def truncate_for_sse(result: str) -> str:
    """SSE 推送用的结果截断：JSON 原样返回，纯文本超 2000 字截断。"""
    if not result:
        return ""
    if looks_like_json(result):
        return result
    if len(result) <= SSE_TEXT_RESULT_LIMIT:
        return result
    return result[:SSE_TEXT_RESULT_LIMIT] + "..."


def compact_args(tool_name: str, args_json: str) -> str:
    """压缩工具参数里的超大字符串字段（写文件类的 content）。

    只对 LARGE_CONTENT_TOOLS 生效，其它工具原样返回 —— 参数本身很小，
    压了反而让人看不懂模型到底传了什么。

    为什么必须压：这份 args 会进 `message.tool_events` 并随历史消息回灌给模型，
    一个 8000 字的 write_file content 会让每一轮对话都多背 8000 字。
    Java 注释里管这叫「对标 Yuxi summary L1」。
    """
    if not args_json:
        return "{}"
    if tool_name not in LARGE_CONTENT_TOOLS:
        return args_json
    try:
        parsed = json.loads(args_json)
    except (json.JSONDecodeError, TypeError):
        # 模型输出被 maxTokens 截断时 args 可能是半截 JSON，没法解析 —— 整体截断
        return _truncate_raw(args_json)
    if not isinstance(parsed, dict):
        return _truncate_raw(args_json)

    changed = False
    for key, value in parsed.items():
        if isinstance(value, str) and len(value) > ARG_COMPACT_LIMIT:
            parsed[key] = value[:20] + TRUNCATED_ARG_MARK
            changed = True
    if not changed:
        return args_json
    return json.dumps(parsed, ensure_ascii=False)


def _truncate_raw(raw: str) -> str:
    if len(raw) <= ARG_COMPACT_LIMIT:
        return raw
    return raw[:20] + TRUNCATED_ARG_MARK


def parse_args_for_db(args_json: str) -> dict[str, Any] | list[Any] | str:
    """把参数 JSON 串转成能写进 JSONB 列的值。

    Java 侧 `tool_input` 声明成 String，但列是 JSONB —— 靠 JsonbTypeHandler
    交给 PG 解析，所以库里存的是**对象**而不是"包在字符串里的 JSON"。
    Python 这边直接存解析后的对象，落库形态才一致。

    解析失败就存原始字符串（JSONB 允许存字符串标量），不丢数据。
    """
    if not args_json:
        return {}
    try:
        return json.loads(args_json)
    except (json.JSONDecodeError, TypeError):
        return args_json


def build_call_event(
    *,
    tool_name: str,
    display_name: str,
    icon: str,
    args_json: str,
    content_offset: int,
    tool_call_id: int,
) -> dict[str, Any]:
    """构造 `tool_call` 事件（进 tool_events + 推 SSE，同一份数据）。

    字段名逐字对齐 Java `appendToolCallStart`：前端按白名单取字段，
    名字差一个字母这张卡片就不渲染，且不会报错（静默丢弃）。
    """
    return {
        "type": "tool_call",
        "toolName": tool_name,
        "displayName": display_name,
        "icon": icon,
        "args": compact_args(tool_name, args_json),
        "contentOffset": content_offset,
        # 前端拿它回查完整结果：/api/chat/sessions/tool-calls/{id}/result
        "toolCallId": str(tool_call_id),
    }


def build_result_event(
    *,
    tool_name: str,
    display_name: str,
    icon: str,
    result: str,
    content_offset: int,
    tool_call_id: int,
) -> dict[str, Any]:
    """构造 `tool_result` 事件。result 已按 SSE 规则截断过。"""
    return {
        "type": "tool_result",
        "toolName": tool_name,
        "displayName": display_name,
        "icon": icon,
        "result": truncate_for_sse(result),
        "contentOffset": content_offset,
        "toolCallId": str(tool_call_id),
    }


def compact_events_for_list(events: Any) -> Any:
    """历史列表瘦身：把超长 result 正文换成 `resultTotalLength`。

    规则（照抄 Java）：
    - result 长度 ≤ 500 → 原样保留，前端直接渲染，省一次请求
    - result 长度 > 500 → 删掉 `result`，写 `resultTotalLength: N`
      前端发现没有 result 但有 toolCallId，就调详情接口按需拉

    为什么短结果也保留：详情接口在 `tool_output` 为空时会返回空串，
    前端"静默失败"（看不到任何提示），短结果保留能规避这类幽灵问题。
    """
    if not isinstance(events, list) or not events:
        return events
    out: list[Any] = []
    for evt in events:
        if not isinstance(evt, dict) or evt.get("type") != "tool_result":
            out.append(evt)
            continue
        result = evt.get("result")
        text = "" if result is None else str(result)
        if len(text) <= INLINE_RESULT_LIMIT:
            out.append(evt)
            continue
        trimmed = {k: v for k, v in evt.items() if k != "result"}
        trimmed["resultTotalLength"] = len(text)
        out.append(trimmed)
    return out
