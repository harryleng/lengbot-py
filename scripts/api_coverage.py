"""前端接口覆盖率检查：前端 `src/api/*.js` 里调的路由，后端实现了几个。

为什么需要这个脚本：Python 版是逐步对齐 Java 的，前端却一上来就调全部接口。
不扫一遍的话，只能靠"点页面 → 撞 404 → 补一个"这样一个个撞出来，
既慢又容易漏（有些接口只在特定操作时才触发，比如导出、置顶）。

用法：
    uv run python -m scripts.api_coverage          # 汇总缺失清单
    uv run python -m scripts.api_coverage --full   # 连已实现的一起列
"""

from __future__ import annotations

import argparse
import re
from collections import defaultdict
from pathlib import Path

from app.main import app

# 前端目录是 lengbot-py 的同级兄弟：C:\home\lengbot\lengbot\lengbot-ui
UI_API_DIR = Path(__file__).resolve().parents[2] / "lengbot" / "lengbot-ui" / "src" / "api"

# 抓 request.get('/x') / request.post(`/x/${id}/y`) / request.delete(...)
CALL_RE = re.compile(
    r"request\.(get|post|put|delete|patch)\(\s*[`'\"](/[^`'\"]*)[`'\"]",
    re.IGNORECASE,
)
# 模板变量 ${id} / ${agentId} → 统一成 {param}
VAR_RE = re.compile(r"\$\{[^}]+\}")


def backend_routes() -> dict[str, set[str]]:
    """{归一化路径: {方法}}，只取 /api 开头的业务路由。"""
    routes: dict[str, set[str]] = defaultdict(set)
    for r in app.routes:
        path = getattr(r, "path", "")
        methods = getattr(r, "methods", None)
        if not path.startswith("/api") or not methods:
            continue
        norm = re.sub(r"\{[^}]+\}", "{param}", path)
        for m in methods - {"HEAD", "OPTIONS"}:
            routes[norm].add(m.upper())
    return routes


def frontend_calls() -> dict[str, dict[str, set[str]]]:
    """{归一化路径: {方法: {来源文件}}}"""
    calls: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for f in sorted(UI_API_DIR.glob("*.js")):
        text = f.read_text(encoding="utf-8", errors="ignore")
        for method, raw in CALL_RE.findall(text):
            path = VAR_RE.sub("{param}", raw.split("?")[0].rstrip("/")) or "/"
            calls["/api" + path][method.upper()].add(f.name)
    return calls


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--full", action="store_true", help="同时列出已实现的接口")
    args = parser.parse_args()

    if not UI_API_DIR.exists():
        print(f"找不到前端目录：{UI_API_DIR}")
        return

    be = backend_routes()
    fe = frontend_calls()

    ok: list[tuple[str, str, str]] = []
    missing: list[tuple[str, str, str]] = []
    for path, methods in sorted(fe.items()):
        for method, files in sorted(methods.items()):
            src = ",".join(sorted(files))
            if method in be.get(path, set()):
                ok.append((method, path, src))
            else:
                missing.append((method, path, src))

    if args.full:
        print(f"=== 已实现（{len(ok)}）===")
        for m, p, s in ok:
            print(f"  {m:<6} {p:<52} {s}")

    print(f"\n=== 前端在用、后端没有（{len(missing)}）===")
    for m, p, s in missing:
        print(f"  {m:<6} {p:<52} {s}")

    total = len(ok) + len(missing)
    pct = (len(ok) / total * 100) if total else 0
    print(f"\n覆盖率：{len(ok)}/{total}（{pct:.0f}%）")


if __name__ == "__main__":
    main()
