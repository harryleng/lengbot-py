from pydantic import field_validator

from app.schemas.common import CamelModel


class UserOut(CamelModel):
    """当前登录用户。

    前端 `stores/user.js` 只关心 role（是否 admin）和 firstLogin，
    其余是头像/昵称展示用。
    """

    id: str
    username: str | None = None
    nickname: str | None = None
    email: str | None = None
    avatar: str | None = None
    phone: str | None = None
    role: str | None = None
    status: str | None = None
    # 前端用它决定是否弹「首次登录请改密码」
    first_login: bool = False

    @field_validator("id", mode="before")
    @classmethod
    def _bigint_to_str(cls, v: int | str | None) -> str | None:
        return None if v is None else str(v)


class LoginIn(CamelModel):
    username: str
    password: str


class LoginOut(CamelModel):
    token: str
    user: UserOut
