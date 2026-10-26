"""用户认证业务（register / password / profile / avatar / init-admin / 查询）。

对照 Java `UserServiceImpl`。密码用 **bcrypt**（与 Java Sa-Token 的 BCrypt 哈希兼容：
`$2a$10$...` 前缀 Java 写入的存量哈希，Python `bcrypt.checkpw` 可直接校验，
迁移期两侧混跑不会互相踢下线）。
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

import bcrypt
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.snowflake import new_id
from app.models.entities import User
from app.services import minio_client

logger = logging.getLogger(__name__)

# 与 Java VALID_FRAMES / ALLOWED_AVATAR_EXTS 逐字一致
VALID_FRAMES = {"lightning", "flame", "stars"}
ALLOWED_AVATAR_EXTS = {"jpg", "jpeg", "png", "gif", "webp", "bmp"}

K_AVATAR_FRAME = "avatarFrame"
K_LEVEL = "level"


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def check_password(plain: str, hashed: str | None) -> bool:
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(plain.encode(), hashed.encode())
    except ValueError:
        return False


def to_dto(user: User) -> dict:
    """Java `UserDTO.from(user)` 的字段集。"""
    config = user.config if isinstance(user.config, dict) else {}
    return {
        "id": str(user.id),
        "username": user.username,
        "nickname": user.nickname,
        "email": user.email,
        "avatar": user.avatar,
        "phone": user.phone,
        "role": user.role,
        "status": user.status,
        "createTime": user.create_time,
        "lastLoginAt": user.last_login_at,
        "firstLogin": False,
        "avatarFrame": config.get(K_AVATAR_FRAME),
        "level": config.get(K_LEVEL),
    }


class UserService:
    def __init__(self, db: AsyncSession, user_id: int) -> None:
        self.db = db
        self.user_id = user_id

    async def _current_user(self) -> User:
        row = await self.db.get(User, self.user_id)
        if row is None:
            raise LookupError("用户不存在")
        return row

    @staticmethod
    async def username_taken(db: AsyncSession, username: str) -> bool:
        stmt = (
            select(func.count())
            .select_from(User)
            .where(User.username == username)
            .where(User.deleted == 0)
        )
        return int((await db.execute(stmt)).scalar() or 0) > 0

    # ------------------------------------------------------------------
    async def has_any_user(self) -> bool:
        stmt = select(func.count()).select_from(User).where(User.deleted == 0)
        return int((await self.db.execute(stmt)).scalar() or 0) > 0

    async def register(self, username: str, password: str, nickname: str | None, email: str | None) -> dict:
        if await self.username_taken(self.db, username):
            raise ValueError("用户名已存在")
        now = datetime.now()
        row = User(
            id=new_id(),
            username=username,
            password=hash_password(password),
            nickname=nickname or username,
            email=email or "",
            role="user",
            status="active",
            create_time=now,
            update_time=now,
            deleted=0,
        )
        self.db.add(row)
        await self.db.commit()
        await self.db.refresh(row)
        return to_dto(row)

    async def init_admin(self, username: str, password: str, nickname: str | None) -> dict:
        if await self.has_any_user():
            raise ValueError("系统已初始化，不能重复创建管理员")
        if await self.username_taken(self.db, username):
            raise ValueError("用户名已存在")
        now = datetime.now()
        row = User(
            id=new_id(),
            username=username,
            password=hash_password(password),
            nickname=nickname or username,
            email="",
            role="admin",
            status="active",
            create_time=now,
            update_time=now,
            deleted=0,
        )
        self.db.add(row)
        await self.db.commit()
        await self.db.refresh(row)
        return to_dto(row)

    async def change_password(self, old_password: str, new_password: str) -> None:
        row = await self._current_user()
        if not check_password(old_password, row.password):
            raise PermissionError("用户名或密码错误")
        row.password = hash_password(new_password)
        row.update_time = datetime.now()
        await self.db.commit()

    async def update_profile(
        self,
        nickname: str | None,
        email: str | None,
        phone: str | None,
        avatar_frame: str | None,
        level: int | None,
    ) -> dict:
        """⚠️ `users.config` 与头像框/等级/长期记忆偏好混存一列 ——
        必须读整列改目标 key 再写回（同 user_preference_service 的约束）。"""
        row = await self._current_user()
        if nickname is not None:
            row.nickname = nickname
        if email is not None:
            row.email = email
        if phone is not None:
            row.phone = phone

        config = dict(row.config or {})
        if avatar_frame is not None:
            if avatar_frame == "" or avatar_frame == "none":
                config.pop(K_AVATAR_FRAME, None)
            else:
                if avatar_frame not in VALID_FRAMES:
                    raise ValueError(f"无效的头像框: {avatar_frame}")
                config[K_AVATAR_FRAME] = avatar_frame
        if level is not None:
            config[K_LEVEL] = level
        # JSONB 原地修改不触发脏检查，必须整体重新赋值
        row.config = config
        row.update_time = datetime.now()
        await self.db.commit()
        await self.db.refresh(row)
        return to_dto(row)

    async def upload_avatar(self, filename: str, data: bytes) -> str:
        row = await self._current_user()
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if ext not in ALLOWED_AVATAR_EXTS:
            raise ValueError("支持格式: jpg/jpeg/png/gif/webp/bmp")

        path = f"user/{self.user_id}/avatar/{uuid.uuid4().hex}.{ext}"
        # 先删旧头像再传新（Java 同款顺序）；旧头像不存在时 delete 静默跳过
        await minio_client.delete(row.avatar) if row.avatar and "/avatar/" in row.avatar else None
        await minio_client.upload_bytes(data, path, f"image/{'jpeg' if ext == 'jpg' else ext}")
        url = minio_client.public_url(path)
        row.avatar = url
        row.update_time = datetime.now()
        await self.db.commit()
        return url

    async def get_by_ids(self, ids: list[int]) -> list[dict]:
        if not ids:
            return []
        stmt = select(User).where(User.id.in_(ids)).where(User.deleted == 0)
        rows = (await self.db.execute(stmt)).scalars().all()
        return [to_dto(r) for r in rows]

    async def search(self, keyword: str) -> list[dict]:
        kw = (keyword or "").strip()
        if not kw:
            return []
        like = f"%{kw}%"
        stmt = (
            select(User)
            .where(User.deleted == 0)
            .where((User.username.like(like)) | (User.nickname.like(like)))
            .limit(20)
        )
        rows = (await self.db.execute(stmt)).scalars().all()
        return [to_dto(r) for r in rows]
