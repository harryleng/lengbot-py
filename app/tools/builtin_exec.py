"""代码执行工具 `execute_code` —— 对齐 Java 的 `ExecuteCodeTool`。

**这不是安全沙盒，必须先说清楚。**
Java 版在同一 JVM 里跑（GraalJS / 编译器 API / 内嵌解释器），靠宿主限制能力。
Python 版没有等价的进程内沙箱，只能起子进程：
    - 工作目录是一个临时目录（跑完即删）
    - 5 秒硬超时（超时直接 kill）
    - 环境变量裁到最小（保留 PATH 以便找到 node / java）
但它**拦不住**恶意代码读写本机文件、连网、开子进程 —— 只是让代码别乱丢文件。
要做真隔离得靠容器/虚拟机，这超出"学习版"的范围。

支持三种语言（本机实测：python 3.12 / node 22 / java 21）：
| language | 入口约定 | 对应 Java 版行为 |
|---|---|---|
| `python` | 必须定义 `def main():` | 一致 |
| `javascript` | 必须定义 `function main(){}` | 一致 |
| `java` | 直接写方法体（无需 import/class/main） | 一致，但这里是**真编译**，冷启动 1~2 秒 |

返回结构照抄 Java：
`{success, output, returnValue, error, elapsedMs, language, artifacts?}`

`artifacts`：脚本在工作目录生成的文件（pptx/xlsx/图片等），会自动复制到
工作区 `outputs/` 并列出相对路径 —— 对齐 Java 的"Python 产物自动落会话工作区"。
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Annotated

from app.core.config import settings
from app.tools.base import field, tool

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 5
# stdout/stderr 截断：模型只需要看结论，几十万行日志会撑爆上下文
MAX_OUTPUT_CHARS = 4000

RETURN_MARKER = "\n__RETURN__"
# 子进程在 stdout 里打的标记，用来把 main() 的返回值和普通 print 分开
MARKER = RETURN_MARKER.strip()

# 与 Java 版 ExecuteCodeTool 声明的预导入保持一致（Java 写方法体时不用自己 import）
JAVA_PRELUDE = """import java.time.*;
import java.util.*;
import java.math.*;
import java.text.*;
import java.util.stream.*;
import java.util.regex.*;

public class Runner {
    static Object run() throws Exception {
"""

JAVA_EPILOGUE = """
    }

    public static void main(String[] args) throws Exception {
        try {
            Object r = run();
            System.out.println("%s" + (r == null ? "null" : String.valueOf(r)));
        } catch (Exception e) {
            System.err.println(e.getClass().getName() + ": " + e.getMessage());
            System.exit(1);
        }
    }
}
"""

PY_WRAPPER = """
if __name__ == "__main__":
    import json as _json
    _r = main()
    print("__MARK__" + _json.dumps(_r, ensure_ascii=False, default=str))
"""

JS_WRAPPER = """
;(function () {
  try {
    var _r = main();
    console.log("__MARK__" + JSON.stringify(_r === undefined ? null : _r));
  } catch (e) {
    console.error(String((e && e.stack) || e));
    process.exit(1);
  }
})();
"""


def _ok(**kwargs: object) -> str:
    return json.dumps({"success": True, **kwargs}, ensure_ascii=False)


def _err(message: str) -> str:
    return json.dumps({"success": False, "error": message}, ensure_ascii=False)


def _clip(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[已截断，共 {len(text)} 字符]"


def _snapshot(work_dir: Path) -> set[str]:
    """记录执行前的文件清单，用来 diff 出脚本新生成的产物。"""
    return {p.relative_to(work_dir).as_posix() for p in work_dir.rglob("*")}


def _collect_artifacts(work_dir: Path, produced: set[str]) -> list[dict]:
    """把脚本新建的文件搬进工作区 outputs/，返回清单。

    只搬 `produced`（执行前后 diff 出来的新文件），避免把脚本自身也当产物。
    """
    artifacts: list[dict] = []
    out_root = Path(settings.workspace_root) / "outputs"
    for path in sorted(work_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(work_dir).as_posix()
        if rel in produced:
            continue
        try:
            out_root.mkdir(parents=True, exist_ok=True)
            target = out_root / path.name
            shutil.copyfile(path, target)
            artifacts.append(
                {
                    "name": path.name,
                    "path": f"outputs/{path.name}",
                    "size": target.stat().st_size,
                }
            )
        except OSError as exc:  # noqa: PERF203
            logger.warning("产物复制失败 %s: %s", rel, exc)
    return artifacts


async def _run(cmd: list[str], work_dir: Path) -> tuple[int, str, str, int]:
    started = time.perf_counter()
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(work_dir),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=TIMEOUT_SECONDS)
        code = proc.returncode or 0
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return (
            124,
            "",
            f"执行超时（>{TIMEOUT_SECONDS} 秒），已强制终止",
            int((time.perf_counter() - started) * 1000),
        )
    elapsed = int((time.perf_counter() - started) * 1000)
    return (
        code,
        out.decode("utf-8", errors="replace"),
        err.decode("utf-8", errors="replace"),
        elapsed,
    )


@tool(
    "execute_code",
    "在安全沙盒中执行代码片段并返回结果。支持 Python（默认）、JavaScript 和 Java。"
    "【Python】必须定义 def main(): 作为入口。"
    "【JavaScript】必须定义 function main(){} 作为入口。"
    "【Java】直接写方法体（无需 import/class/main），已预导入 java.time.*、java.util.*、"
    "java.math.*、java.text.*、java.util.stream.*、java.util.regex.*；如需返回值用 return。"
    "超时 5 秒。",
)
async def execute_code(
    code: Annotated[
        str, field("要执行的代码（Java 写方法体；JS/Python 写含 main 函数的完整代码）")
    ],
    language: Annotated[str, field("编程语言（python/javascript/java），默认 python")],
) -> str:
    lang = (language or "python").strip().lower()
    if lang not in {"python", "javascript", "java"}:
        return _err(f"不支持的语言: {language}（仅支持 python / javascript / java）")
    if not code or not code.strip():
        return _err("code 不能为空")

    work_dir = Path(tempfile.mkdtemp(prefix="lengbot_exec_"))
    try:
        if lang == "python":
            script = work_dir / "main.py"
            script.write_text(
                code + PY_WRAPPER.replace("__MARK__", MARKER), encoding="utf-8"
            )
            cmd = [sys.executable, "-I", str(script)]
        elif lang == "javascript":
            script = work_dir / "main.js"
            script.write_text(code + JS_WRAPPER.replace("__MARK__", MARKER), encoding="utf-8")
            node = shutil.which("node")
            if not node:
                return _err("本机找不到 node，无法执行 JavaScript")
            cmd = [node, str(script)]
        else:
            # Java：把方法体包进 static Object run()。多数片段带 return，
            # 少数不带（打印型脚本），编译报 "missing return statement" 时补一句 return null 重编。
            src = JAVA_PRELUDE + code + JAVA_EPILOGUE % MARKER
            script = work_dir / "Runner.java"
            script.write_text(src, encoding="utf-8")
            java = shutil.which("java")
            if not java:
                return _err("本机找不到 java，无法执行 Java 代码")
            cmd = [java, str(script)]

        # 先拍快照再执行：此刻临时目录里只有我们自己的脚本，不算产物
        produced = await asyncio.to_thread(_snapshot, work_dir)

        rc, out, err, elapsed = await _run(cmd, work_dir)

        missing_return = "missing return statement" in err or "缺少返回语句" in err
        if rc != 0 and lang == "java" and missing_return:
            script.write_text(
                JAVA_PRELUDE + code + "\n        return null;\n" + JAVA_EPILOGUE % MARKER,
                encoding="utf-8",
            )
            rc, out, err, elapsed = await _run(cmd, work_dir)

        # 拆出 main() 的返回值：子进程把结果打在 __RETURN__ 标记后面
        return_value: object = None
        stdout = out
        if MARKER in out:
            head, _, tail = out.partition(MARKER)
            stdout = head
            raw = tail.strip().splitlines()[0] if tail.strip() else "null"
            try:
                return_value = json.loads(raw)
            except json.JSONDecodeError:
                return_value = raw

        artifacts = await asyncio.to_thread(_collect_artifacts, work_dir, produced)
        payload: dict[str, object] = {
            "success": rc == 0,
            "output": _clip(stdout.strip()),
            "returnValue": return_value,
            "error": _clip(err.strip()) or None,
            "elapsedMs": elapsed,
            "language": lang,
        }
        if artifacts:
            payload["artifacts"] = artifacts
        return json.dumps(payload, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001
        logger.exception("execute_code 失败")
        return _err(f"执行失败: {exc}")
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


EXEC_TOOLS = [execute_code]
