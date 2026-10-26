"""工具抽象：一个 Python 函数 + 类型注解 = 一个带 JSON Schema 的 LLM 工具。

对照 Java 侧（`lengbot-tool/builtin/CalculatorTool.java`）：

    @Tool(name="calculator", description="...")
    public String calculate(
        @ToolParam(name="a", description="第一个操作数") double a,
        @ToolParam(name="b", description="第二个操作数") double b,
        @ToolParam(name="operation", description="...") String operation)

Python 版（同样的语义，但不用写两遍）：

    @tool("calculator", "执行基本数学运算")
    async def calculator(
        a: Annotated[float, Field(description="第一个操作数")],
        b: Annotated[float, Field(description="第二个操作数")],
        operation: Annotated[Literal["add", "subtract"], Field(description="运算类型")],
    ) -> str: ...

关键差异：**Java 要在注解和 input_schema 之间手工保持同步，Python 只有一处**。
参数名、类型、是否必填、enum 取值、description 全部从函数签名推出来，
不可能出现"改了实现忘了改 schema"这种漂移。
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, Any, get_args, get_origin, get_type_hints

from pydantic import BaseModel, Field, create_model
from pydantic.fields import FieldInfo

Handler = Callable[..., Awaitable[str]]


@dataclass(slots=True)
class ToolSpec:
    """一个可被 LLM 调用的工具。"""

    name: str
    description: str
    params_model: type[BaseModel]
    handler: Handler

    def openai_schema(self) -> dict[str, Any]:
        """转成 OpenAI 兼容的 tools 格式（`/chat/completions` 的 `tools` 字段）。

        为什么剥掉 `title`：Pydantic 会给每个 model 和字段加 `title`，
        对模型来说是噪音，还会白白多占 token。
        """
        schema = self.params_model.model_json_schema()
        _strip_titles(schema)
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": schema,
            },
        }

    def db_schema(self) -> dict[str, Any]:
        """给数据库 / 前端用的纯净 JSON Schema（不带 OpenAPI 风格的包装）。"""
        schema = self.params_model.model_json_schema()
        _strip_titles(schema)
        return schema


def _strip_titles(node: Any) -> None:
    """递归删掉 JSON Schema 里的 title 字段。"""
    if isinstance(node, dict):
        node.pop("title", None)
        for value in node.values():
            _strip_titles(value)
    elif isinstance(node, list):
        for item in node:
            _strip_titles(item)


def _build_params_model(fn: Callable[..., Any]) -> type[BaseModel]:
    """从函数签名造一个 Pydantic model。

    这是整个工具层最核心的 20 行 —— 类型注解 → JSON Schema 的魔法就在这里。
    Java 那套要靠运行时反射读 `@ToolParam` 注解，Python 靠 `get_type_hints` +
    `create_model`，更短也更可靠（类型注解是 IDE 和 mypy 都能校验的）。
    """
    hints = get_type_hints(fn, include_extras=True)
    sig = inspect.signature(fn)
    fields: dict[str, Any] = {}

    for pname, param in sig.parameters.items():
        if pname in ("self", "cls"):
            continue
        annotation = hints.get(pname, str)

        # Annotated[X, Field(description=...)] → 拆出基础类型和 Field 定义
        if get_origin(annotation) is Annotated:
            base, *metas = get_args(annotation)
            field_info = next((m for m in metas if isinstance(m, FieldInfo)), None)
            default = field_info if field_info is not None else ...
            fields[pname] = (base, default)
        elif param.default is inspect.Parameter.empty:
            fields[pname] = (annotation, ...)
        else:
            fields[pname] = (annotation, param.default)

    return create_model(f"{fn.__name__}_params", **fields)


def tool(name: str, description: str) -> Callable[[Handler], ToolSpec]:
    """装饰器：把一个 async 函数变成 ToolSpec。

    为什么要求 async：工具执行经常是 IO（查库、调 HTTP、读文件）。
    同步函数在 async 循环里会**阻塞整个事件循环**——
    这是 Java 程序员转 Python 最容易踩的坑（在 Spring 里阻塞一个线程无所谓，
    在 asyncio 里一个阻塞调用会卡住所有并发请求）。

    确实没有 IO 的工具（如 calculator）也要写成 async，
    代价几乎为零，换来的是"绝不可能意外阻塞"。
    """

    def decorator(fn: Handler) -> ToolSpec:
        if not inspect.iscoroutinefunction(fn):
            raise TypeError(
                f"工具 {name} 必须是 async 函数：同步函数会阻塞 asyncio 事件循环，"
                f"把整个服务的并发拖死。请改成 `async def`。"
            )
        params_model = _build_params_model(fn)
        return ToolSpec(
            name=name,
            description=description,
            params_model=params_model,
            handler=fn,
        )

    return decorator


def field(description: str, **kwargs: Any) -> Any:
    """语法糖：`Annotated[str, field("参数说明")]`。"""
    return Field(description=description, **kwargs)
