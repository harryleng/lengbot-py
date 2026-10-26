"""管理员用户管理（对照 Java `AdminController` + `UserServiceImpl`）。

⚠️ 一个**有意不照抄** Java 的地方：Java 的 `listUsers` 出参是 `Page<User>`
实体直出，而 User 实体的 `password` 字段**没有 @JsonIgnore** ——
BCrypt 哈希会随用户列表一起发给前端。管理员界面没有任何地方消费这个字段，
这是纯泄露（哈希可被离线爆破）。Python 版的列表/详情出参一律**剔除 password**，
其余字段保持一致。等 Java 侧修的时候前端不用动。

两条自我保护规则（Java 同款）：

- 不允许修改**自己的角色**（防误降级把自己锁在管理员门外）
- 不允许删除**自己**
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entities import Agent, Knowledge, KnowledgeMember, User


def user_admin_out(row: User) -> dict:
    """管理员视角的用户条目。与 Java UserDTO 基本一致但**剔除 password**。"""
    config = row.config if isinstance(row.config, dict) else {}
    return {
        "id": str(row.id),
        "username": row.username,
        "nickname": row.nickname,
        "email": row.email,
        "avatar": row.avatar,
        "phone": row.phone,
        "role": row.role,
        "status": row.status,
        "lastLoginAt": row.last_login_at,
        "createTime": row.create_time,
        "updateTime": row.update_time,
        "avatarFrame": config.get("avatarFrame"),
        "level": config.get("level"),
    }


class AdminUserService:
    def __init__(self, db: AsyncSession, user_id: int) -> None:
        self.db = db
        self.user_id = user_id

    async def check_admin(self) -> None:
        row = await self.db.get(User, self.user_id)
        if row is None or row.role != "admin":
            raise PermissionError("需要管理员权限")

    async def list_users(
        self, page_num: int, page_size: int, keyword: str | None
    ) -> tuple[list[dict], int]:
        stmt = select(User).where(User.deleted == 0)
        if keyword and keyword.strip():
            like = f"%{keyword.strip()}%"
            stmt = stmt.where(or_(User.username.like(like), User.nickname.like(like)))
        # Java 是 orderByAsc(createTime)：管理列表老的在前，保持一致
        stmt = stmt.order_by(User.create_time)

        total = int(
            (await self.db.execute(select(func.count()).select_from(stmt.subquery()))).scalar()
            or 0
        )
        rows = (
            (await self.db.execute(stmt.offset((page_num - 1) * page_size).limit(page_size)))
            .scalars()
            .all()
        )
        return [user_admin_out(r) for r in rows], total

    async def get_user(self, user_id: int) -> dict | None:
        row = await self.db.get(User, user_id)
        if row is None or row.deleted:
            return None
        return user_admin_out(row)

    async def update_user(
        self,
        target_id: int,
        *,
        nickname: str | None,
        email: str | None,
        phone: str | None,
        role: str | None,
        status: str | None,
    ) -> None:
        row = await self.db.get(User, target_id)
        if row is None or row.deleted:
            raise LookupError("用户不存在")

        # 防误降级：改自己的角色直接拒（Java 同款）
        if target_id == self.user_id and role is not None and role != row.role:
            raise ValueError("不能修改自己的角色")

        if nickname is not None:
            row.nickname = nickname
        if email is not None:
            row.email = email
        if phone is not None:
            row.phone = phone
        if role is not None:
            row.role = role
        if status is not None:
            row.status = status
        row.update_time = datetime.now()
        await self.db.commit()

    async def delete_user(self, target_id: int) -> None:
        if target_id == self.user_id:
            raise ValueError("不能删除自己的账号")
        row = await self.db.get(User, target_id)
        if row is None or row.deleted:
            raise LookupError("用户不存在")
        # 逻辑删除（Java deleteById → MP 逻辑删 deleted=1）；
        # 用户名有"deleted=0 才唯一"的部分索引，软删后同名可重新注册
        row.deleted = 1
        row.status = "disabled"
        row.update_time = datetime.now()
        await self.db.commit()

    async def user_agents(self, target_id: int) -> list[dict]:
        stmt = (
            select(Agent)
            .where(Agent.user_id == target_id)
            .where(or_(Agent.deleted == 0, Agent.deleted.is_(None)))
            .order_by(Agent.create_time.desc())
        )
        rows = (await self.db.execute(stmt)).scalars().all()
        return [
            {
                "id": str(a.id),
                "name": a.name,
                "status": a.status,
                "createTime": a.create_time,
            }
            for a in rows
        ]

    async def user_knowledges(self, target_id: int) -> list[dict]:
        """通过 knowledge_member 反查该用户加入的库（Java 同款两跳）。"""
        member_ids = (
            await self.db.execute(
                select(KnowledgeMember.knowledge_id).where(
                    KnowledgeMember.user_id == target_id
                )
            )
        ).scalars().all()
        if not member_ids:
            return []
        stmt = (
            select(Knowledge)
            .where(Knowledge.id.in_(member_ids))
            .where(or_(Knowledge.deleted == 0, Knowledge.deleted.is_(None)))
        )
        rows = (await self.db.execute(stmt)).scalars().all()
        return [
            {
                "id": str(k.id),
                "name": k.name,
                "type": k.type,
                "createTime": k.create_time,
            }
            for k in rows
        ]
