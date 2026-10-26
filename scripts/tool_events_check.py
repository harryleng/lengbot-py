"""tool_events 规则自检 —— **不需要数据库**，纯逻辑。

覆盖三件容易写错的事：
1. JSON 结果不能被截断（截了前端 JSON.parse 就崩）
2. 历史瘦身阈值 500，边界要精确
3. 写文件类的大参数要压缩，非写文件类不能动

用法：python -m scripts.tool_events_check
"""

from __future__ import annotations

import json

from app.services.tool_events import (
    ARG_COMPACT_LIMIT,
    INLINE_RESULT_LIMIT,
    SSE_TEXT_RESULT_LIMIT,
    TRUNCATED_ARG_MARK,
    build_call_event,
    build_result_event,
    compact_args,
    compact_events_for_list,
    parse_args_for_db,
    truncate_for_sse,
)

PASS = "\033[32m✅\033[0m"
FAIL = "\033[31m❌\033[0m"
failures = 0


def check(label: str, actual: object, expected: object) -> None:
    global failures
    ok = actual == expected
    if not ok:
        failures += 1
    print(f"  {PASS if ok else FAIL} {label}")
    if not ok:
        print(f"     期望: {str(expected)[:200]}")
        print(f"     实际: {str(actual)[:200]}")


def check_true(label: str, cond: bool, detail: str = "") -> None:
    global failures
    if not cond:
        failures += 1
    print(f"  {PASS if cond else FAIL} {label}" + (f"  ({detail})" if not cond and detail else ""))


def main() -> None:
    print("=" * 70)
    print("1. truncate_for_sse —— JSON 不能截，纯文本按 2000 截")
    print("=" * 70)

    big_json = json.dumps({"rows": [{"v": i} for i in range(2000)]}, ensure_ascii=False)
    check_true(f"大 JSON ({len(big_json)} 字) 原样返回", truncate_for_sse(big_json) == big_json)
    try:
        json.loads(truncate_for_sse(big_json))
        check_true("截断后仍是合法 JSON", True)
    except json.JSONDecodeError:
        check_true("截断后仍是合法 JSON", False)

    big_arr = json.dumps(list(range(5000)))
    check_true(f"大数组 ({len(big_arr)} 字) 原样返回", truncate_for_sse(big_arr) == big_arr)

    big_text = "字" * (SSE_TEXT_RESULT_LIMIT + 500)
    got = truncate_for_sse(big_text)
    check("纯文本截到 2000 + '...'", len(got), SSE_TEXT_RESULT_LIMIT + 3)

    short = "短的"
    check("短纯文本原样", truncate_for_sse(short), short)
    check("空串原样", truncate_for_sse(""), "")

    # 前导空白的 JSON 也要认出来（lstrip 的意义）
    padded = "  {" + "a" * 5000 + "}"
    check_true("前导空格 + JSON 不截断", truncate_for_sse(padded) == padded)

    print("\n" + "=" * 70)
    print("2. compact_args —— 只压写文件类的大字段")
    print("=" * 70)

    big_content = "x" * 5000
    args = json.dumps({"path": "a.txt", "content": big_content}, ensure_ascii=False)
    got = json.loads(compact_args("local_write_file", args))
    check("path 未被压缩", got["path"], "a.txt")
    check_true("content 被压缩", got["content"].endswith(TRUNCATED_ARG_MARK))
    check("压缩后长度 ≈ 20 + 标记", len(got["content"]), 20 + len(TRUNCATED_ARG_MARK))

    # 非写文件类工具：原样返回（哪怕参数很大）
    search_args = json.dumps({"query": "y" * 5000})
    check("web_search 参数不压缩", compact_args("web_search", search_args), search_args)

    # 写文件类但字段不超阈值：也原样
    small_args = json.dumps({"path": "a.txt", "content": "x" * ARG_COMPACT_LIMIT})
    check("刚好等于阈值不压缩", compact_args("local_write_file", small_args), small_args)

    # 半截 JSON（模型被 maxTokens 截断）
    broken = '{"path": "a.txt", "content": "' + "z" * 300
    got_broken = compact_args("local_write_file", broken)
    check_true("半截 JSON 整体截断且带标记", got_broken.endswith(TRUNCATED_ARG_MARK))
    check_true("半截 JSON 不抛异常", isinstance(got_broken, str))

    check("空 args 返回 '{}'", compact_args("local_write_file", ""), "{}")

    print("\n" + "=" * 70)
    print("3. compact_events_for_list —— 历史瘦身阈值 500")
    print("=" * 70)

    def ev(result: str) -> dict:
        return {"type": "tool_result", "toolName": "t", "result": result, "toolCallId": "1"}

    exact = "a" * INLINE_RESULT_LIMIT
    out = compact_events_for_list([ev(exact)])
    check("刚好 500 字保留原文", out[0].get("result"), exact)
    check_true("刚好 500 字不写 resultTotalLength", "resultTotalLength" not in out[0])

    over = "a" * (INLINE_RESULT_LIMIT + 1)
    out = compact_events_for_list([ev(over)])
    check_true("501 字删掉 result", "result" not in out[0])
    check("501 字写入 resultTotalLength", out[0].get("resultTotalLength"), INLINE_RESULT_LIMIT + 1)
    check("toolCallId 保留（前端靠它回查）", out[0].get("toolCallId"), "1")

    # tool_call 事件不能被瘦身
    call_evt = {"type": "tool_call", "toolName": "t", "args": "{}", "toolCallId": "2"}
    out = compact_events_for_list([call_evt])
    check("tool_call 事件原样保留", out[0], call_evt)

    check("空列表原样", compact_events_for_list([]), [])
    check("None 原样", compact_events_for_list(None), None)

    print("\n" + "=" * 70)
    print("4. parse_args_for_db —— 落库形态")
    print("=" * 70)

    check("合法 JSON → dict", parse_args_for_db('{"a": 1}'), {"a": 1})
    check("合法数组 → list", parse_args_for_db("[1,2]"), [1, 2])
    check("空串 → {}", parse_args_for_db(""), {})
    check("非法 JSON → 原串（不丢数据）", parse_args_for_db("not json"), "not json")

    print("\n" + "=" * 70)
    print("5. build_call_event / build_result_event —— 字段名对齐前端")
    print("=" * 70)

    ce = build_call_event(
        tool_name="local_write_file",
        display_name="写文件",
        icon="📝",
        args_json='{"path":"a.txt","content":"' + "x" * 500 + '"}',
        content_offset=42,
        tool_call_id=123456789,
    )
    call_fields = {"type", "toolName", "displayName", "icon", "args", "contentOffset", "toolCallId"}
    check("call 事件字段集合", set(ce), call_fields)
    check("type", ce["type"], "tool_call")
    check("contentOffset 是 int", ce["contentOffset"], 42)
    check("toolCallId 是字符串（bigint 精度）", ce["toolCallId"], "123456789")

    re_ = build_result_event(
        tool_name="local_write_file",
        display_name="写文件",
        icon="📝",
        result='{"success": true}',
        content_offset=42,
        tool_call_id=123456789,
    )
    result_fields = {"type", "toolName", "displayName", "icon", "result", "contentOffset", "toolCallId"}
    check("result 事件字段集合", set(re_), result_fields)
    check("type", re_["type"], "tool_result")

    print("\n" + "=" * 70)
    if failures:
        print(f"{FAIL} 失败 {failures} 项")
        raise SystemExit(1)
    print(f"{PASS} 全部通过")


if __name__ == "__main__":
    main()
