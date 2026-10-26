from typing import Annotated

from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import auth_gate
from app.core.db import get_db
from app.repositories.chat_session_repo import ChatSessionRepository
from app.services.chat_session_service import ChatSessionService

DbSession = Annotated[AsyncSession, Depends(get_db)]


async def get_current_user_id(
    request: Request, db: Annotated[AsyncSession, Depends(get_db)]
) -> int:
    """认证收口后的统一身份解析（见 `core/auth_gate.py`）。

    - `auth_enabled=False`（开发态默认）：恒返回 dev_user_id，行为与收口前一致，
      33 项回归脚本无需带 token。
    - `auth_enabled=True`（上线态）：解析 Authorization 头（token / API Key），
      无效一律 401。**这就是当初留的"阶段 1 只改这一个函数"的位置**，
      调用方一行没改。

    为什么签名里要 db：API Key 路径要查 `api_key` 表，
    复用请求级 session，不为认证单开连接。
    """
    try:
        return await auth_gate.resolve_user_id(request, db)
    except auth_gate.Unauthorized as exc:
        # 前端 axios 拦截器按 body.code 判断，HTTP 恒 200 与全站一致
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except auth_gate.RateLimited as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except auth_gate.AgentForbidden as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


CurrentUserId = Annotated[int, Depends(get_current_user_id)]


def get_chat_session_service(db: DbSession) -> ChatSessionService:
    """手工组装依赖。

    对照 Java 的 @Autowired：Python 通常不用 DI 容器，
    FastAPI 的 Depends 就是最轻的那一层。服务依赖 repo，repo 依赖 session，
    一条线写清楚，比满屏注解更好读。
    """
    return ChatSessionService(ChatSessionRepository(db))


ChatSessionServiceDep = Annotated[ChatSessionService, Depends(get_chat_session_service)]
