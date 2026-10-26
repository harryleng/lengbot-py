"""从 lengbot-ui 的 api 层提取接口清单，生成学习看板。

为什么这么做：前端 36 个 api 文件、381 个调用点，等于一份**现成的需求文档**——
路径、方法、参数名全写着。照着它写 Python 路由，比翻 43 个 Controller 快得多。

用法：
    python scripts/export_api_todo.py > docs/API_TODO.md
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

UI_API_DIR = Path(r"C:\home\lengbot\lengbot\lengbot-ui\src\api")

# 匹配 request.get('/path') / request.post(`/path`) 等
PATTERN = re.compile(
    r"""request\.(get|post|put|delete|patch)\(\s*[`'"]([^`'"]+)[`'"]""",
    re.IGNORECASE,
)

# 按功能分组：用 api 文件名当分组名
GROUP_HINTS = {
    "chat": "聊天核心（阶段 1，优先）",
    "chatSession": "会话管理（阶段 0 已做三个读接口）",
    "agent": "Agent 配置",
    "knowledge": "知识库 RAG（阶段 3）",
    "workflow": "工作流（阶段 4）",
    "tool": "工具",
    "skill": "Skill",
    "model": "模型供应商",
    "prompt": "提示词",
    "eval": "评测",
    "memory": "记忆",
}


def guess_group(filename: str) -> str:
    """按前端 api 文件名分组，并附上阶段标签。

    直接按文件分最实用：一个 api 文件通常对应一个页面/一块功能，
    迁完一个文件就可以去前端点一遍验证。
    """
    stem = Path(filename).stem
    for key, label in GROUP_HINTS.items():
        if key.lower() in stem.lower():
            return f"{stem}　→　{label}"
    return stem


def main() -> int:
    if not UI_API_DIR.exists():
        print(f"找不到前端 api 目录: {UI_API_DIR}", file=sys.stderr)
        return 1

    groups: dict[str, list[tuple[str, str, str]]] = {}
    total = 0

    for js in sorted(UI_API_DIR.glob("*.js")):
        text = js.read_text(encoding="utf-8", errors="ignore")
        for method, path in PATTERN.findall(text):
            # 把模板变量 ${id} 标出来，提醒需要路径参数
            norm = re.sub(r"\$\{[^}]+\}", "{id}", path)
            groups.setdefault(guess_group(js.name), []).append((method.upper(), norm, js.name))
            total += 1

    print("# 接口 TODO 清单\n")
    print(f"从 `{UI_API_DIR}` 自动提取，共 **{total}** 个调用点。\n")
    print("> 生成后请人工核对：同一条路径可能被不同页面调用多次。\n")

    for group in sorted(groups, key=lambda g: -len(groups[g])):
        items = groups[group]
        print(f"\n## {group}（{len(items)}）\n")
        print("| 方法 | 路径 | 来源文件 |")
        print("|---|---|---|")
        seen: set[tuple[str, str]] = set()
        for method, path, src in sorted(items):
            key = (method, path)
            if key in seen:
                continue
            seen.add(key)
            print(f"| {method} | `{path}` | {src} |")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
