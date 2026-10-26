"""内置工具实现。

对照 Java：`lengbot-tool/builtin/` 下的 26 个 builtin 工具。
这里只实现少数几个用于打通链路 —— 其余工具（sandbox_*、write_todos、
knowledge 检索等）依赖沙箱/待办/向量检索，属于阶段 2 后半和阶段 3。

**输出格式约定**：工具返回值一律是字符串，且优先返回 JSON。
原因：模型读 JSON 比读自然语言更稳（字段名明确、不会歧义），
前端工具面板也需要结构化数据来渲染。这一点和 Java 侧完全一致。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated, Literal

from app.tools.base import field, tool

_OPERATION_SYMBOL = {
    "add": "+",
    "subtract": "-",
    "multiply": "×",
    "divide": "÷",
}


@tool(
    "calculator",
    "执行基本数学运算，包括加减乘除。当用户需要精确数学计算时调用此工具。",
)
async def calculator(
    a: Annotated[float, field("第一个操作数")],
    b: Annotated[float, field("第二个操作数")],
    operation: Annotated[
        Literal["add", "subtract", "multiply", "divide"],
        field("运算类型：add（加）、subtract（减）、multiply（乘）、divide（除）"),
    ],
) -> str:
    """与 Java `CalculatorTool.calculate` 行为一致的实现。

    注意 `Literal` 的用法：它会自动生成 JSON Schema 的 `enum`，
    模型只能从这四个值里选 —— 比在 description 里写"只能是 add/subtract/..."
    可靠得多，省下一轮"模型传了个不认识的字符串然后报错"的来回。
    """
    op = operation.lower()
    if op == "add":
        result = a + b
    elif op == "subtract":
        result = a - b
    elif op == "multiply":
        result = a * b
    else:
        if b == 0:
            raise ArithmeticError("除数不能为零")
        result = a / b

    # 整数就不带小数点，对齐 Java 的格式化规则
    formatted = str(int(result)) if result == int(result) and abs(result) < 1e16 else f"{result:.6g}"

    return json.dumps(
        {
            "expression": f"{a} {_OPERATION_SYMBOL[op]} {b}",
            "operation": op,
            "operands": [a, b],
            "result": formatted,
        },
        ensure_ascii=False,
    )


@tool("get_current_time", "获取当前日期和时间。用户问现在几点、今天几号时调用。")
async def get_current_time(
    timezone: Annotated[str, field("时区名，如 Asia/Shanghai；不传则用服务器本地时区")] = "",
) -> str:
    """阶段 2 用来验证链路的最简工具。

    刻意保留了 `timezone` 参数（哪怕可以不带），
    这样能测到"可选参数"在 schema 里长什么样。
    """
    if timezone:
        try:
            from zoneinfo import ZoneInfo

            now = datetime.now(ZoneInfo(timezone))
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"无效时区 {timezone!r}: {exc}") from exc
    else:
        now = datetime.now()

    return json.dumps(
        {
            "iso": now.isoformat(timespec="seconds"),
            "date": now.strftime("%Y-%m-%d"),
            "time": now.strftime("%H:%M:%S"),
            "weekday": ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][now.weekday()],
            "timezone": timezone or "local",
        },
        ensure_ascii=False,
    )


@tool("text_stats", "统计一段文本的字数、行数和字符数。")
async def text_stats(
    text: Annotated[str, field("要统计的文本内容")],
) -> str:
    """演示最简单的单参数工具。"""
    lines = text.splitlines() or [text]
    return json.dumps(
        {
            "chars": len(text),
            "chars_no_space": len("".join(text.split())),
            "lines": len(lines),
            "words": len(text.split()),
        },
        ensure_ascii=False,
    )


# 所有内置工具的清单。注册表从这里取。
BUILTIN_TOOLS = [calculator, get_current_time, text_stats]
