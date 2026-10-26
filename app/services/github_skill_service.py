"""GitHub / ModelScope 远程 Skill 浏览与安装准备。

对照 Java：`GitHubSkillService`（643 行）。四条链路：

| 接口 | 走哪条路 |
|---|---|
| `/remote/list` | GitHub Contents API 递归找 SKILL.md，或 ModelScope API |
| `/remote/search` | 本地 `npx skills find <keyword>`（**不是** GitHub API） |
| `/remote/prepare` | 下载仓库 ZIP（或 git clone）→ 暂存草稿 → 返回预览 |
| `/remote/commit` | 草稿 → 正式目录 + 落库（在 `skill_service`） |

## Python 版的两个刻意差异

1. **网络调用用 httpx.AsyncClient，不用阻塞式 RestTemplate**。
   Java 的请求落在 Tomcat 工作线程上，慢了只占一个线程；Python 只有一个
   事件循环，同步 HTTP 会把**所有**请求卡住。
2. **`owner` / `repo` 进 URL 前先校验格式**。Java 直接拼字符串，
   `owner` 传 `../` 或带 `?` 会拼出别的路径。这里用 `^[A-Za-z0-9._-]+$` 挡住。

## 关于 `/remote/search`

它**依赖宿主机装了 Node.js**（`npx -y skills find`）。没装时 Java 抛
`INTERNAL_ERROR`（500）；这里同样抛 500 并给出明确提示，
而不是静默返回空列表 —— 空列表会让用户以为"没有匹配的 Skill"。
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from typing import Any

import httpx
from fastapi import HTTPException, status

from app.core.config import settings
from app.services import skill_storage

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
MODELSCOPE_PREFIX = "https://modelscope.cn/skills/"
# npx skills find 的输出行：`owner/repo@skill-name  339.3K installs`
SEARCH_LINE = re.compile(r"^([a-zA-Z0-9_.\-]+/[a-zA-Z0-9_.\-]+)@([a-zA-Z0-9_.\-]+)(?:\s+(.*))?$")
# owner / repo 进 URL 前的白名单校验（见模块 docstring）
SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")
ANSI = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")

TIMEOUT = httpx.Timeout(30.0, connect=10.0)


def _bad(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


def _is_windows() -> bool:
    return os.name == "nt"


def _headers() -> dict[str, str]:
    headers = {"Accept": "application/vnd.github.v3+json"}
    if settings.github_token:
        headers["Authorization"] = f"Bearer {settings.github_token}"
    return headers


# ==================== source 解析 ====================


def is_model_scope_url(source: str) -> bool:
    return source.strip().startswith(MODELSCOPE_PREFIX)


def parse_source(source: str) -> tuple[str, str, str | None]:
    """`owner/repo` / GitHub URL / `owner/repo@branch` → (owner, repo, branch)。"""
    if not source or not source.strip():
        raise _bad("source 不能为空")
    s = source.strip()
    for prefix in ("https://github.com/", "http://github.com/", "github.com/"):
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    s = s.rstrip("/")
    if s.endswith(".git"):
        s = s[: -len(".git")]

    branch: str | None = None
    at = s.find("@")
    if at > 0:
        branch = s[at + 1:]
        s = s[:at]

    parts = s.split("/")
    if len(parts) < 2 or not parts[0].strip() or not parts[1].strip():
        raise _bad("无法解析仓库地址，支持格式：owner/repo 或 GitHub URL")

    owner, repo = parts[0].strip(), parts[1].strip()
    if not SAFE_SEGMENT.match(owner) or not SAFE_SEGMENT.match(repo):
        raise _bad("仓库地址包含非法字符")
    if branch is not None and not SAFE_SEGMENT.match(branch):
        raise _bad("分支名包含非法字符")
    return owner, repo, branch


# ==================== 列表 ====================


async def _github_get(url: str) -> object:
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.get(url, headers=_headers())
    if resp.status_code >= 400:
        logger.warning("[GitHubSkill] API %s → %s", url, resp.status_code)
        return None
    return resp.json()


async def _fetch_contents_recursive(
    owner: str, repo: str, path: str, branch: str | None, out: list[tuple[str, str]]
) -> None:
    url = f"{GITHUB_API}/repos/{owner}/{repo}/contents/{path}"
    if branch:
        url += f"?ref={branch}"
    data = await _github_get(url)
    if not isinstance(data, list):
        return
    for item in data:
        item_type = item.get("type", "")
        item_path = item.get("path", "")
        if item_type == "file":
            out.append((item_path, item.get("download_url", "")))
        elif item_type == "dir":
            await _fetch_contents_recursive(owner, repo, item_path, branch, out)


def _extract_slug_from_path(path: str) -> str:
    """`skills/foo/SKILL.md` → `foo`；根目录下返回空串（调用方用 repo 兜底）。"""
    normalized = path.replace("\\", "/")
    last = normalized.rfind("/")
    if last > 0:
        prev = normalized.rfind("/", 0, last)
        return normalized[prev + 1:last]
    return ""


async def list_remote_skills(
    owner: str, repo: str, branch: str | None
) -> list[dict[str, str]]:
    all_files: list[tuple[str, str]] = []
    await _fetch_contents_recursive(owner, repo, "", branch, all_files)

    skill_mds = [p for p, _ in all_files if p == "SKILL.md" or p.endswith("/SKILL.md")]
    result: list[dict[str, str]] = []
    for path in skill_mds:
        slug = _extract_slug_from_path(path) or repo
        content = await _get_file_content(owner, repo, path, branch)
        if content is None:
            continue
        meta = skill_storage.parse_skill_markdown(content)
        result.append({"name": slug, "description": meta.get("description") or ""})
    return result


async def _get_file_content(
    owner: str, repo: str, path: str, branch: str | None
) -> str | None:
    url = f"{GITHUB_API}/repos/{owner}/{repo}/contents/{path}"
    if branch:
        url += f"?ref={branch}"
    data = await _github_get(url)
    if not isinstance(data, dict) or "content" not in data:
        return None
    try:
        raw = re.sub(r"\s+", "", data["content"])
        return base64.b64decode(raw).decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        logger.warning("[GitHubSkill] base64 解码失败: path=%s err=%s", path, exc)
        return None


# ==================== 全局搜索（npx） ====================


def _run_npx_skills_find(keyword: str) -> str:
    """同步跑 `npx -y skills find`，返回 stdout。"""
    cmd = "npx.cmd" if _is_windows() else "npx"
    temp_home = tempfile.mkdtemp(prefix=".skills-find-")
    try:
        env = dict(os.environ, HOME=temp_home)
        proc = subprocess.run(  # noqa: S603
            [cmd, "-y", "skills", "find", keyword],
            cwd=temp_home,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
        if proc.returncode != 0:
            logger.warning(
                "[GitHubSkill] npx skills find 失败: exit=%s stderr=%s",
                proc.returncode,
                (proc.stderr or "")[:500],
            )
            return ""
        return proc.stdout or ""
    finally:
        shutil.rmtree(temp_home, ignore_errors=True)


def parse_search_output(output: str) -> list[dict[str, str]]:
    """解析 `npx skills find` 输出（去 ANSI、去装饰字符、按行匹配）。"""
    cleaned = ANSI.sub("", output).replace("\r", "\n")
    results: list[dict[str, str]] = []
    seen: set[str] = set()
    for line in cleaned.split("\n"):
        line = re.sub(r"^[│┌└◇◒◐◓◑■●]+\s*", "", line.strip()).strip()
        if not line:
            continue
        match = SEARCH_LINE.match(line)
        if not match:
            continue
        source, name, installs = match.group(1), match.group(2), match.group(3)
        key = f"{source}@{name}"
        if key in seen:
            continue
        seen.add(key)
        results.append(
            {"name": name, "source": source, "installs": (installs or "").strip()}
        )
    return results


async def search_remote_skills(keyword: str | None) -> list[dict[str, str]]:
    if not keyword or not keyword.strip():
        return []
    # 子进程是 args 列表（不经过 shell），这层校验是防日志/输出被注入换行
    if any(ch in keyword for ch in ("\n", "\r", "\0")):
        raise _bad("搜索关键字包含非法字符")

    try:
        stdout = await asyncio.to_thread(_run_npx_skills_find, keyword.strip())
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="服务端未安装 npx/Node.js，无法使用全局搜索功能",
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail="全局搜索超时"
        ) from exc
    return parse_search_output(stdout)


# ==================== 安装准备 ====================


async def prepare_remote_install(
    owner: str, repo: str, branch: str | None, skill_slugs: list[str]
) -> list[dict[str, Any]]:
    draft_id = skill_storage.new_draft_id()
    effective_branch = branch or "main"
    targets = set(skill_slugs)
    temp_dir = tempfile.mkdtemp(prefix="github-skill-")

    try:
        zip_url = (
            f"https://github.com/{owner}/{repo}/archive/refs/heads/{effective_branch}.zip"
        )
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
                resp = await client.get(zip_url, headers=_headers())
            if resp.status_code >= 400 or not resp.content:
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=f"下载仓库 ZIP 失败: HTTP {resp.status_code}",
                )
            _extract_zip(resp.content, temp_dir)
        except zipfile.BadZipFile as exc:
            raise _bad(f"仓库 ZIP 无效: {exc}") from exc

        root = _find_extracted_root(temp_dir)
        previews: list[dict[str, Any]] = []
        found: list[str] = []

        for skill_md_path in _walk_skill_mds(root):
            skill_dir = os.path.dirname(skill_md_path)
            relative = os.path.relpath(skill_md_path, root).replace("\\", "/")
            slug = _extract_slug_from_path(relative) or repo
            found.append(slug)
            if slug not in targets:
                continue

            with open(skill_md_path, "rb") as fh:
                md_bytes = fh.read()
            meta = skill_storage.parse_skill_markdown(
                md_bytes.decode("utf-8", errors="replace")
            )

            file_names: list[str] = []
            for dirpath, _, filenames in os.walk(skill_dir):
                for filename in filenames:
                    abs_path = os.path.join(dirpath, filename)
                    rel = os.path.relpath(abs_path, skill_dir).replace("\\", "/")
                    file_names.append(rel)
                    with open(abs_path, "rb") as fh:
                        await skill_storage.upload_draft_file(
                            f"{skill_storage.DRAFTS_ROOT}{draft_id}/{slug}/{rel}", fh.read()
                        )

            previews.append(
                {
                    "draft_id": draft_id,
                    "slug": slug,
                    "name": meta.get("name"),
                    "description": meta.get("description"),
                    "version": meta.get("version"),
                    "tool_dependencies": meta.get("tool_dependencies", []),
                    "skill_dependencies": meta.get("skill_dependencies", []),
                    "file_names": file_names,
                }
            )

        if not previews:
            await skill_storage.cleanup_draft(draft_id)
            raise _bad(f"未找到指定的 Skill: {skill_slugs}（ZIP 中找到: {found}）")
        return previews
    except HTTPException:
        await skill_storage.cleanup_draft(draft_id)
        raise
    except Exception as exc:  # noqa: BLE001
        await skill_storage.cleanup_draft(draft_id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"远程仓库下载失败: {exc}"
        ) from exc
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def _extract_zip(data: bytes, dest_dir: str) -> None:
    """解压并防 Zip Slip（条目路径逃出 dest_dir 就报错）。"""
    dest = os.path.realpath(dest_dir)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for entry in zf.namelist():
            target = os.path.realpath(os.path.join(dest, entry))
            if not target.startswith(dest + os.sep) and target != dest:
                raise zipfile.BadZipFile(f"ZIP 条目路径不安全: {entry}")
        zf.extractall(dest)  # noqa: S202


def _find_extracted_root(temp_dir: str) -> str:
    entries = os.listdir(temp_dir)
    if len(entries) == 1 and os.path.isdir(os.path.join(temp_dir, entries[0])):
        return os.path.join(temp_dir, entries[0])
    return temp_dir


def _walk_skill_mds(root: str) -> list[str]:
    found: list[str] = []
    for dirpath, _, filenames in os.walk(root):
        for filename in filenames:
            if filename == "SKILL.md":
                found.append(os.path.join(dirpath, filename))
    return found


# ==================== ModelScope ====================


def parse_model_scope_skill_id(source: str) -> str:
    s = source.strip()
    if s.startswith(MODELSCOPE_PREFIX):
        s = s[len(MODELSCOPE_PREFIX):]
    s = s.rstrip("/")
    if not s:
        raise _bad("ModelScope URL 中未找到 Skill ID")
    return s


async def list_model_scope_skills(source: str) -> list[dict[str, str]]:
    skill_id = parse_model_scope_skill_id(source)
    fallback = {"name": skill_id, "description": "ModelScope Skill", "source": "modelscope"}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.get(f"https://modelscope.cn/api/v1/skills/{skill_id}")
        if resp.status_code >= 400:
            return [fallback]
        node = resp.json()
        data = node.get("data") if isinstance(node, dict) and "data" in node else node
        if not isinstance(data, dict):
            return [fallback]
        return [
            {
                "name": str(data.get("name") or skill_id),
                "description": str(data.get("description") or ""),
                "source": "modelscope",
            }
        ]
    except Exception as exc:  # noqa: BLE001
        logger.warning("[GitHubSkill] ModelScope API 失败: %s err=%s", skill_id, exc)
        return [fallback]


async def prepare_model_scope_install(
    source: str, skill_slugs: list[str]
) -> list[dict[str, Any]]:
    skill_id = parse_model_scope_skill_id(source)
    draft_id = skill_storage.new_draft_id()
    temp_dir = tempfile.mkdtemp(prefix="modelscope-skill-")

    try:
        clone_dir = os.path.join(temp_dir, "skill")
        try:
            proc = await asyncio.to_thread(
                subprocess.run,  # noqa: S603
                ["git", "clone", "--depth=1", f"https://modelscope.cn/{skill_id}.git", clone_dir],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=180,
            )
        except FileNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="服务端未安装 git，无法安装 ModelScope Skill",
            ) from exc
        if proc.returncode != 0:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"ModelScope Skill 下载失败: {skill_id}",
            )

        found = _walk_skill_mds(clone_dir)
        if not found:
            raise _bad(f"ModelScope Skill 中未找到 SKILL.md: {skill_id}")

        skill_root = os.path.dirname(found[0])
        slug = os.path.basename(skill_root)
        with open(found[0], "rb") as fh:
            md_bytes = fh.read()
        meta = skill_storage.parse_skill_markdown(
            md_bytes.decode("utf-8", errors="replace")
        )

        file_names: list[str] = []
        for dirpath, _, filenames in os.walk(skill_root):
            for filename in filenames:
                abs_path = os.path.join(dirpath, filename)
                rel = os.path.relpath(abs_path, skill_root).replace("\\", "/")
                file_names.append(rel)
                with open(abs_path, "rb") as fh:
                    await skill_storage.upload_draft_file(
                        f"{skill_storage.DRAFTS_ROOT}{draft_id}/{slug}/{rel}", fh.read()
                    )

        return [
            {
                "draft_id": draft_id,
                "slug": slug,
                "name": meta.get("name"),
                "description": meta.get("description"),
                "version": meta.get("version"),
                "tool_dependencies": meta.get("tool_dependencies", []),
                "skill_dependencies": meta.get("skill_dependencies", []),
                "file_names": file_names,
            }
        ]
    except HTTPException:
        await skill_storage.cleanup_draft(draft_id)
        raise
    except Exception as exc:  # noqa: BLE001
        await skill_storage.cleanup_draft(draft_id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"ModelScope Skill 下载失败: {exc}",
        ) from exc
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
