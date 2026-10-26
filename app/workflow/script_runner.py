"""工作流 script 节点的脚本执行器。

和 `tools/builtin_exec.py` 的 `execute_code` **不是同一套约定**，别混：

| | 入口签名 | 参数来源 | 用途 |
|---|---|---|---|
| `execute_code` 工具 | `def main():` 无参 | 无 | 模型临时算数 |
| `script` 节点 | `def main(params):` 收一个 dict | 节点配置的 inputParams | 工作流里做数据转换 |

对照 Java：`SandboxServiceImpl.executeCode(code, lang, params, timeoutMs)`，
入口约定 `main(params)`（GraalVmSandbox 注释原文：「优先 main(params)，否则取 eval 结果」）。

**参数为什么走文件而不是拼字符串**：params 里可能有任意中文、引号、换行、反斜杠。
拼进代码要过两层转义（JSON + 目标语言字符串），一处漏了就 SyntaxError，
而且报错位置指向 wrapper 不是用户脚本，根本查不出来。写 `params.json` 让脚本自己读，
零转义、零歧义。
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MARKER = "__SCRIPT_RETURN__"
PARAMS_FILE = "__params__.json"
# stdout 截断，防止脚本 print 几十万行把内存吃爆
MAX_OUTPUT_CHARS = 8000

# 脚本节点默认超时。比 execute_code 的 5 秒宽松 —— 工作流脚本常做批量数据
# 转换（解析大 JSON、算统计），5 秒不够；但仍要设上限防死循环。
DEFAULT_TIMEOUT_SEC = 30


@dataclass(slots=True)
class ScriptResult:
    success: bool
    value: Any = None
    error: str | None = None
    output: str = ""
    elapsed_ms: int = 0


PY_WRAPPER = """
if __name__ == "__main__":
    import json as _json
    try:
        with open("__PARAMS_FILE__", encoding="utf-8") as _f:
            _params = _json.load(_f)
    except Exception:
        _params = {}
    try:
        _r = main(_params)
    except TypeError:
        # 兼容无参写法 main()：契约是 main(params)，但别让用户为此卡住
        _r = main()
    print("__MARKER__" + _json.dumps(_r, ensure_ascii=False, default=str))
"""

JS_WRAPPER = """
;(function () {
  try {
    var fs = require('fs');
    var params = {};
    try {
      params = JSON.parse(fs.readFileSync('__PARAMS_FILE__', 'utf8'));
    } catch (e) { params = {}; }
    var r;
    try {
      r = (main.length === 0) ? main() : main(params);
    } catch (e) {
      if (e instanceof TypeError) { r = main(); } else { throw e; }
    }
    console.log('__MARKER__' + JSON.stringify(r === undefined ? null : r));
  } catch (e) {
    console.error(String((e && e.stack) || e));
    process.exit(1);
  }
})();
"""

JAVA_PRELUDE = """import java.time.*;
import java.util.*;
import java.math.*;
import java.text.*;
import java.util.stream.*;
import java.util.regex.*;

public class ScriptRunner {
    static Object run(Map<String, Object> params) throws Exception {
"""

JAVA_EPILOGUE = """
    }

    public static void main(String[] args) throws Exception {
        Map<String, Object> params = new HashMap<>();
__PARAMS_INIT__
        try {
            Object r = run(params);
            System.out.println("%s" + (r == null ? "null" : String.valueOf(r)));
        } catch (Exception e) {
            System.err.println(e.getClass().getName() + ": " + e.getMessage());
            System.exit(1);
        }
    }
}
"""


def _clip(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return text[:MAX_OUTPUT_CHARS] + f"\n...[已截断，原文共 {len(text)} 字符]"


class _JavaParamsBuilder:
    """把 Python dict 生成成 Java 语句（而不是靠 Jackson 解析 JSON）。

    为什么不用 Jackson：用户机器上只有 JDK，没有 Spring 的 jar。
    一开始这版就是 `new ObjectMapper().readValue(...)`，编译直接报
    「程序包 com.fasterxml.jackson.databind 不存在」。

    为什么不用 `Map.of(...)` / `List.of(...)`：这两个**不接受 null 值**，
    而 params 里 None 很常见（某个字段没取到值），一遇上就 NPE。
    所以老老实实生成 `new HashMap<>()` + `put()` / `new ArrayList<>()` + `add()`，
    null 照写不误。

    嵌套结构用临时变量 p0/p1/p2 逐层构造，避免把表达式写成一行天书。
    """

    def __init__(self) -> None:
        self.lines: list[str] = []
        self._n = 0

    def _next(self) -> str:
        self._n += 1
        return f"__p{self._n}"

    def expr(self, value: Any, indent: str = "        ") -> str:
        """返回一个 Java 表达式（标量为字面量，容器为临时变量名）。"""
        if value is None:
            return "null"
        # bool 要在 int 之前判断：Python 里 True 是 int 的子类
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, int):
            if -(2**31) <= value < 2**31:
                return str(value)
            return f"{value}L"
        if isinstance(value, float):
            if value != value or value in (float("inf"), float("-inf")):  # NaN / Inf
                return "null"  # Java 里没有 NaN 字面量，退化成 null
            return repr(value)
        if isinstance(value, str):
            # json.dumps 产生的转义是 Java 字符串字面量的子集
            # （\" \\ \n \r \t \b \f \uXXXX 都合法），可以直接拼进源码。
            # ensure_ascii=True 让中文变成 \uXXXX，避开 javac 编码问题。
            return json.dumps(value, ensure_ascii=True)

        if isinstance(value, (list, tuple)):
            var = self._next()
            self.lines.append(f"{indent}List<Object> {var} = new ArrayList<>();")
            for item in value:
                self.lines.append(f"{indent}{var}.add({self.expr(item, indent)});")
            return var

        if isinstance(value, dict):
            var = self._next()
            self.lines.append(f"{indent}Map<String, Object> {var} = new HashMap<>();")
            for k, v in value.items():
                self.lines.append(
                    f"{indent}{var}.put({json.dumps(str(k), ensure_ascii=True)}, "
                    f"{self.expr(v, indent)});"
                )
            return var

        # 兜底：其他类型字符串化
        return json.dumps(str(value), ensure_ascii=True)


def _java_params_init(params: dict[str, Any]) -> str:
    """生成填 params 的语句块（放在 main 里、调用 run 之前）。"""
    builder = _JavaParamsBuilder()
    for key, value in (params or {}).items():
        val_expr = builder.expr(value, indent="        ")
        builder.lines.append(
            f'        params.put({json.dumps(str(key), ensure_ascii=True)}, {val_expr});'
        )
    return "\n".join(builder.lines) if builder.lines else "        // (无参数)"


async def _exec(cmd: list[str], work_dir: Path, timeout_sec: float) -> tuple[int, str, str, int]:
    started = time.perf_counter()
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(work_dir),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout_sec)
        code = proc.returncode or 0
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, "", f"脚本执行超时（>{timeout_sec:g} 秒），已强制终止", int(
            (time.perf_counter() - started) * 1000
        )
    return (
        code,
        out.decode("utf-8", errors="replace"),
        err.decode("utf-8", errors="replace"),
        int((time.perf_counter() - started) * 1000),
    )


def _split_return(stdout: str) -> tuple[str, str | None]:
    """从 stdout 里切出 `__SCRIPT_RETURN__<json>` 那行，返回 (普通输出, 返回串)。"""
    ret: str | None = None
    kept: list[str] = []
    for line in stdout.splitlines():
        if line.startswith(MARKER):
            ret = line[len(MARKER) :]
        else:
            kept.append(line)
    return "\n".join(kept), ret


async def run_script(
    code: str,
    language: str,
    params: dict[str, Any] | None = None,
    timeout_sec: float = DEFAULT_TIMEOUT_SEC,
) -> ScriptResult:
    """执行脚本，把返回值解析成 Python 对象。

    失败一律以 `ScriptResult(success=False, error=...)` 返回，不抛异常 ——
    是否中断工作流由调用方（script 节点）按 `errorStrategy` 决定，
    执行器不该替业务做这个判断。
    """
    lang = (language or "javascript").strip().lower()
    if not code or not code.strip():
        return ScriptResult(success=False, error="脚本内容不能为空")
    if lang not in {"python", "javascript", "java"}:
        return ScriptResult(
            success=False,
            error=f"不支持的语言: {language}（支持 python / javascript / java；"
            f"Java 版还有 groovy，Python 版没有 Groovy 运行时）",
        )

    work_dir = Path(tempfile.mkdtemp(prefix="lengbot_script_"))
    try:
        (work_dir / PARAMS_FILE).write_text(
            json.dumps(params or {}, ensure_ascii=False, default=str), encoding="utf-8"
        )

        if lang == "python":
            script = work_dir / "script.py"
            wrapper = PY_WRAPPER.replace("__PARAMS_FILE__", PARAMS_FILE).replace(
                "__MARKER__", MARKER
            )
            script.write_text(code + wrapper, encoding="utf-8")
            # -I 隔离模式：不读用户 site-packages / PYTHONPATH，避免环境污染
            cmd = [sys.executable, "-I", str(script)]
        elif lang == "javascript":
            node = shutil.which("node")
            if not node:
                return ScriptResult(success=False, error="本机找不到 node，无法执行 JavaScript")
            script = work_dir / "script.js"
            wrapper = JS_WRAPPER.replace("__PARAMS_FILE__", PARAMS_FILE).replace(
                "__MARKER__", MARKER
            )
            script.write_text(code + wrapper, encoding="utf-8")
            cmd = [node, str(script)]
        else:
            javac = shutil.which("javac")
            java = shutil.which("java")
            if not javac or not java:
                return ScriptResult(success=False, error="本机找不到 javac/java")
            src = (
                JAVA_PRELUDE
                + code
                + (JAVA_EPILOGUE.replace("__PARAMS_INIT__", _java_params_init(params or {})) % MARKER)
            )
            script = work_dir / "ScriptRunner.java"
            script.write_text(src, encoding="utf-8")
            code_rc, _, compile_err, _ = await _exec([javac, str(script)], work_dir, 60)
            if code_rc != 0:
                return ScriptResult(success=False, error=f"Java 编译失败: {_clip(compile_err)}")
            cmd = [java, "-cp", str(work_dir), "ScriptRunner"]

        rc, stdout, stderr, elapsed = await _exec(cmd, work_dir, timeout_sec)
        plain, ret = _split_return(stdout)

        if rc != 0:
            detail = _clip(stderr.strip() or plain.strip() or f"退出码 {rc}")
            return ScriptResult(
                success=False, error=detail, output=_clip(plain), elapsed_ms=elapsed
            )

        if ret is None:
            # 脚本跑完但没返回值：按 Java 语义当作 null，不算失败
            return ScriptResult(success=True, value=None, output=_clip(plain), elapsed_ms=elapsed)

        try:
            value = json.loads(ret)
        except json.JSONDecodeError:
            # 返回了非 JSON（比如 Java 返回 "1.0"）—— 原样当字符串
            value = ret
        return ScriptResult(success=True, value=value, output=_clip(plain), elapsed_ms=elapsed)
    except Exception as exc:  # noqa: BLE001
        logger.exception("脚本执行异常")
        return ScriptResult(success=False, error=f"{type(exc).__name__}: {exc}")
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
