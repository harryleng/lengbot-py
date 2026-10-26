"""管理员接口（对照 Java `AdminController`，6 个端点）。

所有端点都过 `check_admin`（dev 用户的 role=admin）；非管理员统一 403。
Java 的出参把 password 哈希也带出去了 —— 这里有意修掉，见 service 注释。

路由参数用 `{target_id}` 与依赖注入的 `user_id` 区分开，避免 FastAPI 把
"当前登录用户"和"目标用户"混成一个值（那会直接把归属校验变摆设）。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from app.api.deps import CurrentUserId, DbSession
from app.schemas.common import Result
from app.services.admin_user_service import AdminUserService

router = APIRouter(prefix="/api/admin", tags=["admin"])


class AdminUserUpdateIn(BaseModel):
    """Java `AdminUserUpdateDTO`：全字段可选，只更新传了的。"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    nickname: str | None = None
    email: str | None = None
    phone: str | None = None
    role: str | None = None
    status: str | None = None


async def _svc(db: DbSession, user_id: CurrentUserId) -> AdminUserService:
    svc = AdminUserService(db, user_id)
    try:
        await svc.check_admin()
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return svc


def _not_found(exc: LookupError) -> HTTPException:
    return HTTPException(status_code=404, detail=str(exc))


@router.get("/users", response_model=Result[dict], summary="分页查询所有用户")
async def list_users(
    db: DbSession,
    user_id: CurrentUserId,
    page_num: int = Query(default=1, alias="pageNum", ge=1),
    page_size: int = Query(default=20, alias="pageSize", ge=1, le=200),
    keyword: str | None = Query(default=None),
) -> Result[dict]:
    rows, total = await (await _svc(db, user_id)).list_users(page_num, page_size, keyword)
    return Result.ok(
        {"records": rows, "total": total, "pageNum": page_num, "pageSize": page_size}
    )


@router.get("/users/{target_id}", response_model=Result[dict], summary="获取用户详情")
async def get_user(
    db: DbSession, user_id: CurrentUserId, target_id: int
) -> Result[dict]:
    user = await (await _svc(db, user_id)).get_user(target_id)
    if user is None:
        # Java：Result.fail("用户不存在")
        return Result.fail("用户不存在")
    return Result.ok(user)


@router.put("/users/{target_id}", response_model=Result[None], summary="更新用户信息")
async def update_user(
    body: AdminUserUpdateIn, db: DbSession, user_id: CurrentUserId, target_id: int
) -> Result[None]:
    try:
        await (await _svc(db, user_id)).update_user(
            target_id,
            nickname=body.nickname,
            email=body.email,
            phone=body.phone,
            role=body.role,
            status=body.status,
        )
    except LookupError as exc:
        raise _not_found(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return Result.ok(None)


@router.delete("/users/{target_id}", response_model=Result[None], summary="删除用户")
async def delete_user(
    db: DbSession, user_id: CurrentUserId, target_id: int
) -> Result[None]:
    try:
        await (await _svc(db, user_id)).delete_user(target_id)
    except LookupError as exc:
        raise _not_found(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return Result.ok(None)


@router.get(
    "/users/{target_id}/agents",
    response_model=Result[list[dict]],
    summary="获取用户的 Agent 列表",
)
async def user_agents(
    db: DbSession, user_id: CurrentUserId, target_id: int
) -> Result[list[dict]]:
    return Result.ok(await (await _svc(db, user_id)).user_agents(target_id))


@router.get(
    "/users/{target_id}/knowledges",
    response_model=Result[list[dict]],
    summary="获取用户的知识库列表",
)
async def user_knowledges(
    db: DbSession, user_id: CurrentUserId, target_id: int
) -> Result[list[dict]]:
    return Result.ok(await (await _svc(db, user_id)).user_knowledges(target_id))
