"""Skill 管理接口（20 个，对齐前端 `src/api/skill.js`）。

## 路由顺序是硬约束

`/skills/{id}/...` 里 `{id}` 会被转成 int。静态前缀（`enabled` /
`import/*` / `remote/*` / `by-agent/*`）**必须**先注册。
`by-agent` 和 `import` 是两段路径，虽然段数上不会和 `/{id}/enabled` 撞，
但以后有人加 `/skills/import` 这种一段路径就会踩 —— 一次性排好最省心。

本项目在 agents / tools / model-providers / knowledge 上踩过 7 次同类坑，
注释都留在各模块头部，别再踩第 8 次。

## 二进制出参的口径

`GET /{id}/export` 和 `GET /{id}/file` 返回 **base64 字符串**（不是裸字节流）。
原因是 Java 侧 `Result<byte[]>` 被 Jackson 序列化成 base64，前端 `SkillDetail.vue`
直接 `atob(res.data)` 解码。改成返回裸 ZIP 会让前端 `res.data` 变成 Blob，
`atob` 直接抛异常。
"""

from __future__ import annotations

import base64
from typing import Annotated

from fastapi import APIRouter, File, Query, UploadFile

from app.api.deps import CurrentUserId, DbSession
from app.schemas.common import PageResult, Result
from app.schemas.skill import (
    RemoteListIn,
    RemotePrepareIn,
    SkillFileNode,
    SkillFileWriteIn,
    SkillImportPreview,
    SkillIn,
    SkillOut,
)
from app.services.skill_service import SkillService

router = APIRouter(prefix="/api/skills", tags=["skills"])


def _svc(db: DbSession) -> SkillService:
    return SkillService(db)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


# ==================== 静态前缀（必须先注册） ====================


@router.get(
    "/enabled",
    response_model=Result[list[SkillOut]],
    summary="所有启用中的全局 Skill（供 Agent 绑定下拉）",
)
async def list_enabled(db: DbSession, user_id: CurrentUserId) -> Result[list[SkillOut]]:
    return Result.ok(await _svc(db).list_enabled())


@router.get(
    "/by-agent/{agent_id}",
    response_model=Result[list[SkillOut]],
    summary="按 Agent 查询 Skill（兼容旧接口）",
)
async def list_by_agent(
    agent_id: int, db: DbSession, user_id: CurrentUserId, name: str | None = Query(None)
) -> Result[list[SkillOut]]:
    """现在新建的 Skill 都是 `scope=global`，这条通常返回空。

    前端 Agent 编辑页还在调，删掉会 404，所以保留。
    """
    return Result.ok(await _svc(db).list_by_agent(agent_id, name))


@router.post("/import/preview", response_model=Result[SkillImportPreview], summary="ZIP 导入预览（阶段一）")
async def import_preview(
    db: DbSession,
    user_id: CurrentUserId,
    file: Annotated[UploadFile, File(...)],
) -> Result[SkillImportPreview]:
    """上传 ZIP → 解压到草稿目录 → 解析 SKILL.md 返回预览。

    两阶段设计：用户先看到"这个包里有什么、slug 叫什么"，再决定要不要落库。
    不做这一步的话，一个打错名字的 ZIP 就直接污染了 Skill 库。
    """
    return Result.ok(await _svc(db).import_preview(await file.read()))


@router.post("/import/commit", response_model=Result[SkillOut], summary="ZIP 导入确认（阶段二）")
async def import_commit(
    db: DbSession,
    user_id: CurrentUserId,
    draft_id: str = Query(..., alias="draftId"),
    target_slug: str | None = Query(None, alias="targetSlug"),
) -> Result[SkillOut]:
    return Result.ok(await _svc(db).import_commit(draft_id, target_slug))


@router.post("/remote/list", response_model=Result[list[dict[str, str]]], summary="列出远程仓库中的 Skill")
async def remote_list(
    dto: RemoteListIn, db: DbSession, user_id: CurrentUserId
) -> Result[list[dict[str, str]]]:
    return Result.ok(await _svc(db).list_remote(dto.source))


@router.post(
    "/remote/search",
    response_model=Result[list[dict[str, str]]],
    summary="全局搜索远程 Skill",
)
async def remote_search(
    dto: RemoteListIn, db: DbSession, user_id: CurrentUserId
) -> Result[list[dict[str, str]]]:
    """走本地 `npx skills find`，**不是** GitHub API。

    没装 Node.js 时返回 500 而不是空列表 —— 空列表会让用户以为"没搜到"。
    """
    return Result.ok(await _svc(db).search_remote(dto.source))


@router.post(
    "/remote/prepare",
    response_model=Result[list[SkillImportPreview]],
    summary="远程安装准备（下载并暂存草稿）",
)
async def remote_prepare(
    dto: RemotePrepareIn, db: DbSession, user_id: CurrentUserId
) -> Result[list[SkillImportPreview]]:
    return Result.ok(await _svc(db).prepare_remote(dto.source, dto.skills))


@router.post("/remote/commit", response_model=Result[SkillOut], summary="远程安装确认（提交草稿）")
async def remote_commit(
    db: DbSession,
    user_id: CurrentUserId,
    draft_id: str = Query(..., alias="draftId"),
    slug: str = Query(...),
) -> Result[SkillOut]:
    return Result.ok(await _svc(db).commit_remote(draft_id, slug))


@router.post("/remote/cleanup", response_model=Result[None], summary="清理远程安装草稿")
async def remote_cleanup(
    db: DbSession,
    user_id: CurrentUserId,
    draft_id: str = Query(..., alias="draftId"),
) -> Result[None]:
    await _svc(db).cleanup_draft(draft_id)
    return Result.ok(None)


# ==================== 集合路由 ====================


@router.get("", response_model=Result[PageResult[SkillOut]], summary="分页查询全局 Skill")
async def page(
    db: DbSession,
    user_id: CurrentUserId,
    page_num: int = Query(1, alias="pageNum"),
    page_size: int = Query(20, alias="pageSize"),
    keyword: str | None = Query(None),
) -> Result[PageResult[SkillOut]]:
    return Result.ok(await _svc(db).page(page_num, page_size, keyword))


@router.post("", response_model=Result[SkillOut], summary="新增 Skill")
async def create(dto: SkillIn, db: DbSession, user_id: CurrentUserId) -> Result[SkillOut]:
    return Result.ok(await _svc(db).create(dto))


@router.put("", response_model=Result[SkillOut], summary="更新 Skill（内置不可编辑）")
async def update(dto: SkillIn, db: DbSession, user_id: CurrentUserId) -> Result[SkillOut]:
    return Result.ok(await _svc(db).update(dto))


# ==================== 单资源路由 ====================


@router.delete("/{skill_id}", response_model=Result[None], summary="删除 Skill（内置不可删除）")
async def delete(skill_id: int, db: DbSession, user_id: CurrentUserId) -> Result[None]:
    await _svc(db).delete(skill_id)
    return Result.ok(None)


@router.put("/{skill_id}/enabled", response_model=Result[None], summary="启用 / 禁用 Skill")
async def set_enabled(
    skill_id: int,
    db: DbSession,
    user_id: CurrentUserId,
    enabled: bool = Query(..., description="true 启用 / false 禁用"),
) -> Result[None]:
    await _svc(db).set_enabled(skill_id, enabled)
    return Result.ok(None)


@router.get("/{skill_id}/export", response_model=Result[str], summary="导出 Skill 为 ZIP（base64）")
async def export_zip(skill_id: int, db: DbSession, user_id: CurrentUserId) -> Result[str]:
    return Result.ok(_b64(await _svc(db).export_zip(skill_id)))


@router.get("/{skill_id}/files", response_model=Result[list[SkillFileNode]], summary="Skill 文件树")
async def list_files(skill_id: int, db: DbSession, user_id: CurrentUserId) -> Result[list[SkillFileNode]]:
    return Result.ok(await _svc(db).list_files(skill_id))


@router.get("/{skill_id}/file", response_model=Result[str], summary="读取 Skill 文件内容（base64）")
async def read_file(
    skill_id: int,
    db: DbSession,
    user_id: CurrentUserId,
    path: str = Query(..., description="相对路径，如 SKILL.md"),
) -> Result[str]:
    return Result.ok(_b64(await _svc(db).read_file(skill_id, path)))


@router.post("/{skill_id}/file", response_model=Result[None], summary="创建 Skill 文件 / 目录")
async def create_file(
    skill_id: int, dto: SkillFileWriteIn, db: DbSession, user_id: CurrentUserId
) -> Result[None]:
    await _svc(db).create_file(skill_id, dto.path, dto.content, bool(dto.dir))
    return Result.ok(None)


@router.put("/{skill_id}/file", response_model=Result[None], summary="更新 Skill 文件内容")
async def update_file(
    skill_id: int, dto: SkillFileWriteIn, db: DbSession, user_id: CurrentUserId
) -> Result[None]:
    await _svc(db).update_file(skill_id, dto.path, dto.content)
    return Result.ok(None)


@router.delete("/{skill_id}/file", response_model=Result[None], summary="删除 Skill 文件 / 目录")
async def delete_file(
    skill_id: int,
    db: DbSession,
    user_id: CurrentUserId,
    path: str = Query(...),
) -> Result[None]:
    await _svc(db).delete_file(skill_id, path)
    return Result.ok(None)
