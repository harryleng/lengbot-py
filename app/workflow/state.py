"""工作流执行状态与变量替换。

变量语法是 `{{name}}`（Handlebars 风格），例如 retrieval 节点的
`inputVariable: "{{query}}"` 表示"用变量 query 的值去检索"。

对照 Java：`NodeExecutionContext` 里的 variables / nodeOutputs / 变量解析器。

**为什么变量要分两层**：
- `variables`：全局作用域，节点之间传值（`{{query}}`、`{{api_1.body}}`）
- `node_outputs`：每个节点的原始输出，便于调试和回放

只保留一层也能跑，但出了问题不知道"这个值是哪个节点产出的"。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_VAR_PATTERN = re.compile(r"\{\{\s*([A-Za-z0-9_.\-]+)\s*\}\}")


@dataclass(slots=True)
class ExecState:
    variables: dict[str, Any] = field(default_factory=dict)
    node_outputs: dict[str, Any] = field(default_factory=dict)
    step: int = 0
    current: str | None = None

    def set(self, key: str, value: Any) -> None:
        self.variables[key] = value

    def lookup(self, path: str) -> Any:
        """支持 `a.b.c` 逐层取值（node_outputs 里的嵌套结构）。"""
        if path in self.variables:
            return self.variables[path]
        parts = path.split(".")
        # 先试 node_outputs[node_id][field]
        if len(parts) >= 2:
            node_id = parts[0]
            if node_id in self.node_outputs:
                cur: Any = self.node_outputs[node_id]
                for p in parts[1:]:
                    if isinstance(cur, dict) and p in cur:
                        cur = cur[p]
                    else:
                        return None
                return cur
        return None


def render(template: Any, state: ExecState) -> Any:
    """把 `{{var}}` 替换成实际值。

    两种情况要区分：
    - 整个字符串就是 `{{var}}` → **返回原始类型**（可能是 list/dict，
      比如把整个检索结果传给下一个节点）
    - 模板里嵌在文字中（`"问题：{{query}}"`）→ 返回字符串
    """
    if not isinstance(template, str):
        return template

    whole = _VAR_PATTERN.fullmatch(template.strip())
    if whole:
        value = state.lookup(whole.group(1))
        return "" if value is None else value

    def _repl(m: re.Match[str]) -> str:
        value = state.lookup(m.group(1))
        return "" if value is None else str(value)

    return _VAR_PATTERN.sub(_repl, template)


def render_dict(data: dict[str, Any], state: ExecState) -> dict[str, Any]:
    """递归渲染 dict 里的所有字符串值（节点配置里到处都是变量）。"""
    out: dict[str, Any] = {}
    for k, v in data.items():
        if isinstance(v, str):
            out[k] = render(v, state)
        elif isinstance(v, dict):
            out[k] = render_dict(v, state)
        elif isinstance(v, list):
            out[k] = [render(i, state) if isinstance(i, str) else i for i in v]
        else:
            out[k] = v
    return out
