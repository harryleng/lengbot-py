"""Skill 文件存储：MinIO 上 `skills/` 与 `skill_drafts/` 的读写。

对照 Java：`SkillStorageService`（610 行）。职责三块：

1. **SKILL.md 的 frontmatter 解析 / 生成** —— Skill 的元数据存在 markdown
   头部的 YAML 里，不存 DB（DB 只存 `contentHash` 做变更比对）。
2. **ZIP 导入两阶段** —— 先解压到草稿目录（可预览、可放弃），确认后再
   copy 到正式目录。两阶段是为了让用户看到"这个 ZIP 里到底是什么"再决定。
3. **文件树 / 读写 / 导出** —— 前端 SkillDetail 的左侧文件树就靠这些。

## 与 Java 的一个刻意差异：ZIP 顶层目录剥离

Java 的 `normalizeZipEntryName` 只处理"一层"的情况：

    my-skill/SKILL.md        → SKILL.md          （剥掉了）
    my-skill/scripts/a.py    → my-skill/scripts/a.py  （没剥掉！）

因为它判断 `rest.contains("/")` 才保留原样。结果同一个 ZIP 里 SKILL.md 跑到
根目录、其他文件还在 `my-skill/` 下 —— 导入完文件树是散的。

这里改成**统一剥离共同顶层目录**（所有条目都在同一个顶层目录下时才剥），
行为一致且符合"去掉顶层目录前缀"的注释本意。
"""

from __future__ import annotations

import io
import logging
import re
import uuid
import zipfile
from typing import Any

import yaml
from fastapi import HTTPException, status

from app.services import minio_client

logger = logging.getLogger(__name__)

SKILLS_ROOT = "skills/"
DRAFTS_ROOT = "skill_drafts/"
SKILL_MD = "SKILL.md"

# `^---\n(.*?)\n---\n(.*)$` —— 必须 DOTALL，body 是多行的
_FRONTMATTER = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.DOTALL)


def new_draft_id() -> str:
    """草稿 ID。Java 用 `UUID.randomUUID().toString().replace("-", "")`。"""
    return uuid.uuid4().hex


def _not_found(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


def _bad(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


# ==================== 路径校验 ====================


def validate_file_path(relative_path: str | None) -> str:
    """校验相对路径安全性（对齐 Java `validateFilePath`）。

    挡两件事：`..` 路径穿越（能读到别的 Skill 甚至整个 bucket），
    以及绝对路径 / 以 `skills/` 开头（那已经不是"相对"路径了）。
    """
    if not relative_path or not relative_path.strip():
        raise _bad("文件路径不能为空")
    normalized = relative_path.replace("\\", "/")
    if ".." in normalized:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="路径不允许包含 .."
        )
    if normalized.startswith("/") or normalized.startswith("skills/"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="路径必须为相对路径"
        )
    return normalized


# ==================== SKILL.md 读写 ====================


def _skill_md_path(slug: str) -> str:
    return f"{SKILLS_ROOT}{slug}/{SKILL_MD}"


async def get_skill_markdown(slug: str) -> str:
    path = _skill_md_path(slug)
    if not await minio_client.exists(path):
        raise _not_found("SKILL.md 不存在")
    return (await minio_client.download_bytes(path)).decode("utf-8", errors="replace")


async def skill_markdown_exists(slug: str) -> bool:
    return await minio_client.exists(_skill_md_path(slug))


async def write_skill_markdown(slug: str, content: str) -> None:
    await minio_client.upload_string(content, _skill_md_path(slug), "text/markdown")


async def delete_skill_directory(slug: str) -> None:
    for obj in await minio_client.list_objects(f"{SKILLS_ROOT}{slug}/"):
        await minio_client.delete(obj)


async def list_skill_files(slug: str) -> list[str]:
    """返回**相对路径**列表（去掉 `skills/{slug}/` 前缀）。"""
    prefix = f"{SKILLS_ROOT}{slug}/"
    return [obj[len(prefix):] for obj in await minio_client.list_objects(prefix)]


async def read_file(slug: str, relative_path: str) -> bytes:
    full = f"{SKILLS_ROOT}{slug}/{relative_path}"
    if not await minio_client.exists(full):
        raise _not_found("文件不存在")
    return await minio_client.download_bytes(full)


async def write_file(slug: str, relative_path: str, content: str) -> None:
    await minio_client.upload_string(
        content, f"{SKILLS_ROOT}{slug}/{relative_path}", guess_content_type(relative_path)
    )


async def delete_file(slug: str, relative_path: str) -> None:
    """删文件或整个目录。

    目录的判定：以 `/` 结尾就当目录前缀批量删。Java 原样照做。
    """
    prefix = f"{SKILLS_ROOT}{slug}/{relative_path}"
    if relative_path.endswith("/"):
        for obj in await minio_client.list_objects(prefix):
            await minio_client.delete(obj)
        return
    if not await minio_client.exists(prefix):
        raise _not_found("文件不存在")
    await minio_client.delete(prefix)


def guess_content_type(path: str) -> str:
    """按扩展名猜 MIME（对齐 Java `guessContentType`）。

    不猜就用 `application/octet-stream`，浏览器会**强制下载**而不是内联预览。
    """
    lower = path.lower()
    table = {
        ".md": "text/markdown",
        ".txt": "text/plain",
        ".json": "application/json",
        ".yaml": "text/yaml",
        ".yml": "text/yaml",
        ".xml": "application/xml",
        ".html": "text/html",
        ".htm": "text/html",
        ".css": "text/css",
        ".js": "application/javascript",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".svg": "image/svg+xml",
        ".gif": "image/gif",
        ".pdf": "application/pdf",
    }
    for ext, mime in table.items():
        if lower.endswith(ext):
            return mime
    return "application/octet-stream"


# ==================== 文件树 ====================


def build_tree_from_paths(paths: list[str]) -> list[dict[str, Any]]:
    """扁平路径列表 → 树（对齐 Java `buildTreeFromPaths`）。

    MinIO 的 list 只给扁平 key，树要在内存里拼。中间目录的 `path` 带尾斜杠
    （前端靠 `isDir` + `path` 递归渲染，`path` 也就是删除目录时的前缀）。
    """
    roots: dict[str, dict[str, Any]] = {}

    for path in paths:
        parts = path.split("/")
        level = roots
        parent_children: list[dict[str, Any]] | None = None

        for i, part in enumerate(parts):
            is_leaf = i == len(parts) - 1
            node = level.get(part)
            if node is None:
                node = {"name": part, "path": "", "isDir": False, "children": None}
                if is_leaf:
                    node["path"] = path
                    node["isDir"] = False
                else:
                    node["path"] = "/".join(parts[: i + 1]) + "/"
                    node["isDir"] = True
                    node["children"] = []
                level[part] = node
                # 新节点必须同时挂进父节点的 children，否则下次用 children
                # 重建索引时会把它丢掉（Java 注释里专门写了这行）
                if parent_children is not None:
                    parent_children.append(node)

            if not is_leaf:
                if node["children"] is None:
                    node["children"] = []
                parent_children = node["children"]
                level = {c["name"]: c for c in parent_children}

    return list(roots.values())


async def build_file_tree(slug: str) -> list[dict[str, Any]]:
    return build_tree_from_paths(await list_skill_files(slug))


# ==================== Frontmatter ====================


def parse_skill_markdown(content: str | None) -> dict[str, Any]:
    """SKILL.md → 元数据字典。

    **解析失败一律降级，不报错**：没有 frontmatter 就把整篇当 promptTemplate。
    因为 Skill 文件来源很杂（手写、第三方 ZIP、ModelScope），
    一个格式不规范的第三方包不该让整个导入 500。
    """
    if not content or not content.strip():
        return {"prompt_template": ""}

    match = _FRONTMATTER.match(content)
    if not match:
        return {"prompt_template": content.strip()}

    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        logger.warning("[SkillStorage] YAML 解析失败，按无 frontmatter 处理: %s", exc)
        return {"prompt_template": content.strip()}
    if not isinstance(data, dict):
        return {"prompt_template": content.strip()}

    def s(key: str) -> str | None:
        val = data.get(key)
        return None if val is None else str(val)

    def lst(key: str) -> list[str]:
        val = data.get(key)
        if isinstance(val, (list, tuple)):
            return [str(v) for v in val if v is not None]
        return []

    return {
        "slug": s("slug"),
        "name": s("name"),
        "description": s("description"),
        "version": s("version"),
        "tool_dependencies": lst("tool_dependencies"),
        "mcp_dependencies": lst("mcp_dependencies"),
        "skill_dependencies": lst("skill_dependencies"),
        "prompt_template": match.group(2).strip(),
    }


def build_skill_markdown(meta: dict[str, Any]) -> str:
    """元数据 → SKILL.md。

    标量字段必须用 `default_flow_style=False`（plain `key: value`）：
    flow style（默认 True）会输出 `{key: value}` —— 每行是一个 flow 映射节点，
    连起来根本不是合法 YAML 文档流，本模块的 `parse_skill_markdown`
    （safe_load）直接解析失败降级。第一版就栽在这：写出去的自己读不回。
    """
    lines = ["---"]
    for key in ("name", "slug", "description", "version"):
        val = meta.get(key)
        if val:
            dumped = yaml.safe_dump(
                {key: str(val)},
                allow_unicode=True,
                default_flow_style=False,
                sort_keys=False,
            ).strip()
            lines.append(dumped)
    for key in ("tool_dependencies", "mcp_dependencies", "skill_dependencies"):
        values = meta.get(key) or []
        if values:
            lines.append(f"{key}:")
            lines += [f"  - {v}" for v in values]
        else:
            lines.append(f"{key}: []")
    lines.append("---")
    lines.append("")
    lines.append(meta.get("prompt_template") or "")
    return "\n".join(lines)


# ==================== ZIP 导入（两阶段） ====================


def _strip_common_root(names: list[str]) -> list[str]:
    """所有条目都在同一个顶层目录下时剥掉它（见模块 docstring）。

    `a/SKILL.md` + `a/scripts/x.py` → `SKILL.md` + `scripts/x.py`
    `SKILL.md` + `scripts/x.py`（本来就没顶层目录）→ 原样
    """
    if not names:
        return names
    tops = {n.split("/")[0] for n in names if "/" in n}
    if len(tops) != 1:
        return names
    top = tops.pop()
    # 还有"裸文件"在根目录时不能剥（剥了它就没了）
    if any("/" not in n for n in names):
        return names
    return [n[len(top) + 1:] for n in names if n.startswith(top + "/")]


def _read_zip(data: bytes) -> dict[str, bytes]:
    """ZIP 字节 → {相对路径: 内容}。目录条目跳过。"""
    files: dict[str, bytes] = {}
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = [n for n in zf.namelist() if not n.endswith("/")]
        mapping = {n: m for n, m in zip(names, _strip_common_root(names))}
        for name, mapped in mapping.items():
            if not mapped:
                continue
            files[mapped] = zf.read(name)
    return files


async def stage_draft(draft_id: str, zip_bytes: bytes) -> dict[str, Any]:
    """阶段一：解压 ZIP 到草稿目录，解析 SKILL.md 返回预览。"""
    try:
        files = _read_zip(zip_bytes)
    except zipfile.BadZipFile as exc:
        raise _bad(f"ZIP 文件无效: {exc}") from exc

    if SKILL_MD not in files:
        await cleanup_draft(draft_id)
        raise _bad("ZIP 中缺少 SKILL.md")

    prefix = f"{DRAFTS_ROOT}{draft_id}/"
    for rel, content in files.items():
        await minio_client.upload_bytes(content, prefix + rel)

    meta = parse_skill_markdown(files[SKILL_MD].decode("utf-8", errors="replace"))
    return {
        "draft_id": draft_id,
        "slug": meta.get("slug"),
        "name": meta.get("name"),
        "description": meta.get("description"),
        "version": meta.get("version"),
        "tool_dependencies": meta.get("tool_dependencies", []),
        "skill_dependencies": meta.get("skill_dependencies", []),
        "file_names": list(files.keys()),
    }


async def commit_draft(draft_id: str, target_slug: str | None) -> str:
    """阶段二：草稿 → 正式目录，返回最终 slug。"""
    draft_prefix = f"{DRAFTS_ROOT}{draft_id}/"
    objects = await minio_client.list_objects(draft_prefix)
    if not objects:
        raise _bad("草稿目录为空")

    final_slug = (target_slug or "").strip()
    if not final_slug:
        md_path = draft_prefix + SKILL_MD
        if await minio_client.exists(md_path):
            text = (await minio_client.download_bytes(md_path)).decode(
                "utf-8", errors="replace"
            )
            final_slug = parse_skill_markdown(text).get("slug") or ""
        if not final_slug:
            final_slug = draft_id

    target_prefix = f"{SKILLS_ROOT}{final_slug}/"
    for src in objects:
        await minio_client.copy_object(src, target_prefix + src[len(draft_prefix):])

    await cleanup_draft(draft_id)
    return final_slug


async def upload_draft_file(dest_path: str, content: bytes) -> None:
    """写文件到草稿目录（远程安装用：`skill_drafts/{draftId}/{slug}/...`）。"""
    await minio_client.upload_bytes(content, dest_path)


async def commit_draft_for_slug(draft_id: str, slug: str) -> str:
    """远程安装专用：只提交草稿里指定 slug 的子目录。"""
    src_prefix = f"{DRAFTS_ROOT}{draft_id}/{slug}/"
    objects = await minio_client.list_objects(src_prefix)
    if not objects:
        raise _bad(f"草稿中未找到 Skill: {slug}")

    target_prefix = f"{SKILLS_ROOT}{slug}/"
    for src in objects:
        await minio_client.copy_object(src, target_prefix + src[len(src_prefix):])
    for src in objects:
        await minio_client.delete(src)
    return slug


async def cleanup_draft(draft_id: str) -> None:
    """清草稿。**失败只记日志** —— 清理不干净不影响主流程。"""
    try:
        for obj in await minio_client.list_objects(f"{DRAFTS_ROOT}{draft_id}/"):
            await minio_client.delete(obj)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[SkillStorage] 清理草稿失败: draftId=%s err=%s", draft_id, exc)


# ==================== ZIP 导出 ====================


async def export_skill_zip(slug: str) -> bytes:
    prefix = f"{SKILLS_ROOT}{slug}/"
    objects = await minio_client.list_objects(prefix)
    if not objects:
        raise _not_found("Skill 目录下没有文件")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for obj in objects:
            zf.writestr(f"{slug}/{obj[len(prefix):]}", await minio_client.download_bytes(obj))
    return buf.getvalue()
