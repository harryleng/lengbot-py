"""文件工具：对齐 Agent 实际配置的 `local_*` 系列（7 个，含二进制读写）。

**返回结构照抄 Java 的 `LocalFileTool`**，一个字段都不改：

| 工具 | 返回 |
|---|---|
| `local_read_file` | `{path, content, size}` |
| `local_write_file` | `{path, size, success, mode:"overwrite"}` |
| `local_append_file` | `{path, size, success, mode:"append"}` |
| `local_list_dir` | `{dirPath, root, files:["a.txt","sub/"], total}` |
| `local_delete_file` | `{path, success}` |
| `local_read_file_binary` | `{path, size, base64, success}` |
| `local_write_file_binary` | `{path, size, success, mode:"overwrite-binary"}` |
| 出错 | `{success: false, error: "..."}` |

（早期版本这里用的是 `{ok, entries, bytes, created}` 这套自造字段，
2026-10-24 已改成和 Java 一致 —— 前端工具卡片和模型提示词都是按 Java 那套写的，
两套字段共存只会让"为什么这个工具返回的不一样"变成日常困惑。）

**安全要点（必须看懂）**：
所有路径都必须限制在工作区根目录内。用户或模型给出的 `../..` 这类路径
如果直接拼接执行，就等于把整台机器的文件系统开放给模型 —— 这是 AI Agent
最典型的安全事故。所以每个工具都走 `_safe_path()`：

    1. 先 resolve() 解析掉 `..` 和符号链接
    2. 再判断结果是否仍在 workspace 根目录之下
    3. 不在就抛错

`Path.is_relative_to` 是 Python 3.9+ 的方法，比字符串前缀比较可靠得多
（字符串比较会漏掉 `C:\\work2` 冒充 `C:\\work` 这种前缀攻击）。
"""

from __future__ import annotations

import base64
import binascii
import json
from pathlib import Path
from typing import Annotated

from app.core.config import settings
from app.tools.base import field, tool

# 与 Java LocalFileTool 的常量保持一致
MAX_READ_BYTES = 4 * 1024 * 1024  # 4MB
MAX_WRITE_BYTES = 10 * 1024 * 1024  # 10MB


def _root() -> Path:
    root = Path(settings.workspace_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _safe_path(rel_path: str) -> Path:
    """把相对路径解析成工作区内的绝对路径，越界直接拒绝。"""
    if not rel_path or not rel_path.strip():
        raise ValueError("路径不能为空")

    candidate = (Path(settings.workspace_root) / rel_path).resolve()
    root = _root()
    if not candidate.is_relative_to(root):
        raise PermissionError(f"路径越界：{rel_path!r} 解析后位于工作区之外（{candidate}），已拒绝")
    return candidate


def _ok(**kwargs: object) -> str:
    return json.dumps({"success": True, **kwargs}, ensure_ascii=False)


def _err(message: str) -> str:
    return json.dumps({"success": False, "error": message}, ensure_ascii=False)


# 注意：参数名故意用 camelCase（dirPath / path），
# 因为数据库里 input_schema 的参数名就是 camelCase ——
# 模型会严格按 schema 传参，函数参数名对不上就会 TypeError。
# 这是"契约优先于风格"的典型场景，PEP8 在这里让位。


@tool("local_list_dir", "列出白名单目录下的文件和子目录。传入相对路径如 data，不传则列出根目录。")
async def local_list_dir(
    dirPath: Annotated[str, field("相对于白名单根目录的目录路径，如 data；不传则列出根目录")],
) -> str:
    try:
        target = _safe_path(dirPath)
    except (ValueError, PermissionError) as exc:
        return _err(str(exc))
    if not target.exists():
        return _err(f"目录不存在: {dirPath}")
    if not target.is_dir():
        return _err(f"不是目录: {dirPath}")

    # Java 侧返回的是**字符串数组**，目录名带 "/" 后缀（"sub/"），不是对象数组
    files: list[str] = []
    for item in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name)):
        try:
            files.append(item.name + "/" if item.is_dir() else item.name)
        except OSError:
            continue

    return _ok(
        dirPath=dirPath or "(根目录)",
        root=str(_root()),
        files=files,
        total=len(files),
    )


@tool("local_read_file", "读取白名单目录下的文本文件。仅支持 UTF-8，最大 4MB。")
async def local_read_file(
    path: Annotated[str, field("相对于白名单根目录的文件路径，如 README.md 或 data/notes.txt")],
) -> str:
    try:
        target = _safe_path(path)
    except (ValueError, PermissionError) as exc:
        return _err(str(exc))
    if not target.is_file():
        return _err(f"文件不存在: {path}")

    size = target.stat().st_size
    if size > MAX_READ_BYTES:
        return _err(f"文件超过读取上限(4MB): {path} ({size} bytes)")

    try:
        content = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return _err("文件不是 UTF-8 文本，二进制文件请用 local_read_file_binary")
    return _ok(path=path, content=content, size=len(content))


@tool("local_write_file", "写入（覆盖）白名单目录下的文本文件，父目录自动创建。仅 UTF-8，最大 10MB。")
async def local_write_file(
    path: Annotated[str, field("相对于白名单根目录的文件路径，如 notes/draft.md 或 output.txt")],
    content: Annotated[str, field("要写入的完整文本内容")],
) -> str:
    try:
        target = _safe_path(path)
    except (ValueError, PermissionError) as exc:
        return _err(str(exc))
    if target.is_dir():
        return _err(f"目标是目录，不能写入: {path}")

    data = content.encode("utf-8")
    if len(data) > MAX_WRITE_BYTES:
        return _err(f"内容超过写入上限(10MB): {path}")

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return _ok(path=path, size=len(data), mode="overwrite")


@tool("local_append_file", "向白名单目录下的文本文件追加内容（不存在则创建）。仅 UTF-8，最大 10MB。")
async def local_append_file(
    path: Annotated[str, field("相对于白名单根目录的文件路径，如 notes/draft.md")],
    content: Annotated[str, field("要追加的文本内容")],
) -> str:
    try:
        target = _safe_path(path)
    except (ValueError, PermissionError) as exc:
        return _err(str(exc))
    if target.is_dir():
        return _err(f"目标是目录，不能写入: {path}")

    data = content.encode("utf-8")
    existing = target.stat().st_size if target.exists() else 0
    if existing + len(data) > MAX_WRITE_BYTES:
        return _err(f"追加后超过写入上限(10MB): {path}")

    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("ab") as fh:
        fh.write(data)
    return _ok(path=path, size=existing + len(data), mode="append")


@tool("local_delete_file", "删除白名单目录下的文件。目录不会被删除。")
async def local_delete_file(
    path: Annotated[str, field("要删除的文件路径")],
) -> str:
    try:
        target = _safe_path(path)
    except (ValueError, PermissionError) as exc:
        return _err(str(exc))
    if not target.exists():
        return _err(f"文件不存在: {path}")
    if target.is_dir():
        return _err("这是目录，本工具只删文件")

    target.unlink()
    return _ok(path=path)


@tool(
    "local_read_file_binary",
    "读取白名单目录下的二进制文件，返回文件字节的 Base64 编码。"
    "用于 pptx/xlsx/docx/pdf/图片等，最大 4MB。",
)
async def local_read_file_binary(
    path: Annotated[str, field("相对于白名单根目录的文件路径，如 output/report.pptx")],
) -> str:
    try:
        target = _safe_path(path)
    except (ValueError, PermissionError) as exc:
        return _err(str(exc))
    if not target.is_file():
        return _err(f"文件不存在: {path}")

    size = target.stat().st_size
    if size > MAX_READ_BYTES:
        return _err(f"文件超过读取上限(4MB): {path} ({size} bytes)")

    data = target.read_bytes()
    return _ok(
        path=path,
        size=len(data),
        base64=base64.b64encode(data).decode("ascii"),
    )


@tool(
    "local_write_file_binary",
    "写入（覆盖）白名单目录下的二进制文件。content 为文件字节的 Base64 编码（可含换行，会自动去除），"
    "父目录自动创建，最大 10MB（解码后）。用于落地 pptx/xlsx/docx/pdf/图片。",
)
async def local_write_file_binary(
    path: Annotated[str, field("相对于白名单根目录的文件路径，如 output/report.pptx")],
    content: Annotated[str, field("文件字节的 Base64 编码（不是普通文本）")],
) -> str:
    try:
        target = _safe_path(path)
    except (ValueError, PermissionError) as exc:
        return _err(str(exc))
    if target.is_dir():
        return _err(f"目标是目录，不能写入: {path}")

    # Base64 常被模型按行断开传输，空白字符必须先剔掉；validate=True 会拒绝换行，
    # 所以先清洗再用 validate=True 抓真正的非法字符。
    cleaned = "".join(content.split())
    # 兼容 data URL 前缀：data:image/png;base64,xxxx
    if cleaned.startswith("data:") and "," in cleaned:
        cleaned = cleaned.split(",", 1)[1]

    try:
        data = base64.b64decode(cleaned, validate=True)
    except (binascii.Error, ValueError) as exc:
        return _err(f"content 不是合法的 Base64：{exc}")

    if len(data) > MAX_WRITE_BYTES:
        return _err(f"解码后超过写入上限(10MB): {path} ({len(data)} bytes)")

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return _ok(path=path, size=len(data), mode="overwrite-binary")


FILE_TOOLS = [
    local_list_dir,
    local_read_file,
    local_write_file,
    local_append_file,
    local_delete_file,
    local_read_file_binary,
    local_write_file_binary,
]
