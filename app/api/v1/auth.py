"""认证接口 —— **开发态替身**，不是真认证。

对照 Java：`AuthController` + Sa-Token（`StpUtil.login(id)` 发 token，
之后的请求由拦截器解析 `Authorization` 头还原 loginId）。

Python 版现状：
  - 密码**不做校验**（`users.password` 是 BCrypt 哈希，这里没装 bcrypt 依赖）。
    只要用户名存在且 active，就发 token。
  - token 只是个占位串 `dev-<userId>`，后端**根本不解析它** ——
    `get_current_user_id()` 固定返回 `settings.dev_user_id`。
  - `get_current_user_id` 是唯一需要改的地方（见 `api/deps.py`），
    真认证接上来时，登录/接口调用方一行都不用动。

为什么明知不校验还写这个接口：前端 `stores/user.js` 的登录流程读
`res.data.token` 再塞 localStorage，`router/index.js` 的守卫靠有没有 token
决定放不放行。没有这个接口，前端连聊天页都进不去。
"""

import logging

from datetime import datetime

from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile
from sqlalchemy import select

from app.api.deps import CurrentUserId, DbSession
from app.models.entities import User
from app.schemas.common import Result
from app.schemas.user import LoginIn, LoginOut, UserOut

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _to_out(user: User) -> UserOut:
    return UserOut(
        id=str(user.id),
        username=user.username,
        nickname=user.nickname,
        email=user.email,
        avatar=user.avatar,
        phone=user.phone,
        role=user.role,
        status=user.status,
        first_login=False,
    )


@router.post("/login", response_model=Result[LoginOut], summary="登录")
async def login(body: LoginIn, db: DbSession) -> Result[LoginOut]:
    user = (
        await db.execute(
            select(User).where(User.username == body.username, User.status == "active")
        )
    ).scalars().first()

    if user is None:
        # 前端拦截器读 body.code，用 401 让它走到「登录失败」分支
        return Result.fail("用户名或密码错误", code=401)

    if settings.auth_enabled:
        # 上线态：真校验 BCrypt（Java 存量的 $2a$ 哈希 bcrypt 可直接验）
        if not check_password(body.password, user.password):
            return Result.fail("用户名或密码错误", code=401)
    else:
        logger.warning(
            "开发态登录：用户 %s 未校验密码（AUTH_ENABLED=false，users.password 未参与比对）",
            user.username,
        )

    token = token_store.new_token()
    await token_store.save(token, user.id)
    user.last_login_at = datetime.now()
    await db.commit()
    return Result.ok(LoginOut(token=token, user=_to_out(user)))


@router.get("/me", response_model=Result[UserOut], summary="当前用户")
async def me(user_id: CurrentUserId, db: DbSession) -> Result[UserOut]:
    user = await db.get(User, user_id)
    if user is None:
        return Result.fail("用户不存在", code=401)
    return Result.ok(_to_out(user))


@router.post("/logout", response_model=Result[None], summary="登出")
async def logout(request: Request) -> Result[None]:
    """吊销服务端会话（Redis 删 key）。开发态 token 无会话，删也无害。"""
    raw = (request.headers.get("Authorization") or "").strip()
    if raw.lower().startswith("bearer "):
        raw = raw[7:].strip()
    if raw:
        await token_store.revoke(raw)
    return Result.ok(None)


# ======================================================================
# 以下为阶段 16+ 补齐的 8 个接口（register/profile/avatar/password/init…）
# 密码校验是真做的：bcrypt 与 Java Sa-Token 的 BCrypt 哈希互通。
# ======================================================================
from app.core import token_store  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.schemas.common import CamelModel  # noqa: E402
from app.services.user_admin_service import (  # noqa: E402
    UserService,
    check_password,
)


class RegisterIn(CamelModel):
    username: str
    password: str
    nickname: str | None = None
    email: str | None = None


class ProfileIn(CamelModel):
    nickname: str | None = None
    email: str | None = None
    phone: str | None = None
    avatar_frame: str | None = None
    level: int | None = None


class ChangePasswordIn(CamelModel):
    old_password: str
    new_password: str


def _svc(db: DbSession, user_id: CurrentUserId) -> UserService:
    return UserService(db, user_id)


@router.post("/register", response_model=Result[dict], summary="注册新用户")
async def register(body: RegisterIn, db: DbSession) -> Result[dict]:
    if not (3 <= len(body.username) <= 32):
        raise HTTPException(status_code=400, detail="用户名长度3-32")
    if not (6 <= len(body.password) <= 64):
        raise HTTPException(status_code=400, detail="密码长度6-64")
    try:
        user = await UserService(db, 0).register(
            body.username, body.password, body.nickname, body.email
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # 注册即自动登录：发真 token（上线态无需再手动 login）
    token = token_store.new_token()
    await token_store.save(token, int(user["id"]))
    return Result.ok({"token": token, "user": user})


@router.get("/users/batch", response_model=Result[list[dict]], summary="批量获取用户信息")
async def users_batch(
    db: DbSession,
    ids: list[int] = Query(default=[]),
) -> Result[list[dict]]:
    return Result.ok(await UserService(db, 0).get_by_ids(ids))


@router.get("/users/search", response_model=Result[list[dict]], summary="搜索用户（用户名/昵称）")
async def users_search(db: DbSession, keyword: str = Query(default="")) -> Result[list[dict]]:
    return Result.ok(await UserService(db, 0).search(keyword))


@router.put("/profile", response_model=Result[dict], summary="更新个人信息")
async def update_profile(body: ProfileIn, db: DbSession, user_id: CurrentUserId) -> Result[dict]:
    try:
        user = await _svc(db, user_id).update_profile(
            body.nickname, body.email, body.phone, body.avatar_frame, body.level
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return Result.ok(user)


@router.put("/password", response_model=Result[None], summary="修改密码")
async def change_password(
    body: ChangePasswordIn, db: DbSession, user_id: CurrentUserId
) -> Result[None]:
    try:
        await _svc(db, user_id).change_password(body.old_password, body.new_password)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        # Java：原密码错误 → USERNAME_OR_PASSWORD_ERROR；HTTP 恒 200，body.code 区分
        return Result.fail(str(exc), code=401)
    return Result.ok(None)


@router.post("/avatar", response_model=Result[str], summary="上传用户头像")
async def upload_avatar(
    db: DbSession,
    user_id: CurrentUserId,
    file: UploadFile = File(...),
) -> Result[str]:
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="上传文件不能为空")
    try:
        url = await _svc(db, user_id).upload_avatar(file.filename or "", data)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return Result.ok(url)


@router.get("/init-status", response_model=Result[dict], summary="检查系统初始化状态")
async def init_status(db: DbSession) -> Result[dict]:
    initialized = await UserService(db, 0).has_any_user()
    return Result.ok({"initialized": initialized})


@router.post("/init-admin", response_model=Result[dict], summary="初始化管理员（仅无用户时）")
async def init_admin(body: RegisterIn, db: DbSession) -> Result[dict]:
    try:
        user = await UserService(db, 0).init_admin(
            body.username, body.password, body.nickname
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # 返回结构和 login 一致：token + user
    token = token_store.new_token()
    await token_store.save(token, int(user["id"]))
    return Result.ok({"token": token, "user": user})
