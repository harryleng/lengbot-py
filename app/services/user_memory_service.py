"""用户长期记忆管理（对照 Java `UserMemoryServiceImpl` 的 CRUD 面）。

三个照抄 Java 的"小心思"：

1. **fromValue 三口径**：`preference` / `PREFERENCE` / `用户偏好` 都认，
   未命中回落 preference —— 前端下拉存 code、LLM 抽取存 name、
   手工输入可能存中文，三种历史写法都在库里。
2. **空 keywords 自动提取**：标点清洗 → 按空格拆 → 2~20 字的 token → 去重取 12。
3. **超过 15 条裁剪**：新增前把最没用的挤掉 —— 停用/归档的先删（statusWeight），
   再按 confidence 升序、lastUsedAt/updateTime 升序。

`embedding_vector` 不走 ORM：实体不映射该列，向量化成功后用原生 SQL
`UPDATE ... SET embedding_vector=:v::vector` 单独写。失败只 log 不抛 ——
文本记忆本身已落库，向量丢了只是"语义检索不到"，不该让用户看到 500。
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from decimal import Decimal

from sqlalchemy import delete as sa_delete
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.snowflake import new_id
from app.models.entities import UserMemory

logger = logging.getLogger(__name__)

DEFAULT_CONFIDENCE = 1.0
MAX_USER_MEMORY_COUNT = 15
MAX_CONTENT_LEN = 1000

MEMORY_TYPES = {
    "preference": "用户偏好",
    "profile": "用户画像",
    "project_fact": "项目事实",
    "instruction": "长期指令",
    "lesson": "踩坑经验",
    "case": "成功案例",
}
MEMORY_STATUSES = {
    "active": "启用",
    "disabled": "停用",
    "archived": "归档",
}

_SPLIT_RE = re.compile(r"[，。！？、；：,.!?;:()（）\[\]{}\"'`~@#$%^&*_+=|\\//<>\s]+")


def _from_value(value: str | None, table: dict[str, str], default: str) -> str:
    """code / 枚举名大写 / 中文 desc 三口径，未命中回落默认。"""
    if not value or not value.strip():
        return default
    v = value.strip().lower()
    for code, desc in table.items():
        if v == code or v == code.upper() or v == desc.lower():
            return code
    return default


def extract_keywords(content: str) -> list[str]:
    if not content or not content.strip():
        return []
    cleaned = _SPLIT_RE.sub(" ", content)
    seen: list[str] = []
    for token in cleaned.split(" "):
        token = token.strip()
        if 2 <= len(token) <= 20 and token not in seen:
            seen.append(token)
            if len(seen) >= 12:
                break
    return seen


def normalize_keywords(keywords: list[str] | None, content: str) -> list[str]:
    values = keywords if keywords else extract_keywords(content)
    out: list[str] = []
    for v in values:
        if v and str(v).strip() and str(v).strip() not in out:
            out.append(str(v).strip())
        if len(out) >= 12:
            break
    return out


def normalize_content(content: str | None) -> str:
    normalized = (content or "").strip()
    if not normalized:
        raise ValueError("记忆内容不能为空")
    return normalized[:MAX_CONTENT_LEN]


def to_out(row: UserMemory) -> dict:
    return {
        "id": str(row.id),
        "agentId": str(row.agent_id) if row.agent_id else None,
        "sessionId": str(row.session_id) if row.session_id else None,
        "memoryType": row.memory_type,
        "content": row.content,
        "keywords": list(row.keywords or []),
        "confidence": float(row.confidence) if row.confidence is not None else None,
        "status": row.status,
        "lastUsedAt": row.last_used_at,
        "createTime": row.create_time,
        "updateTime": row.update_time,
    }


class UserMemoryService:
    def __init__(self, db: AsyncSession, user_id: int) -> None:
        self.db = db
        self.user_id = user_id

    async def list_memories(
        self, keyword: str | None, status: str | None
    ) -> list[dict]:
        stmt = (
            select(UserMemory)
            .where(UserMemory.user_id == self.user_id)
            .where(UserMemory.deleted == 0)
        )
        if status and status.strip():
            stmt = stmt.where(
                UserMemory.status == _from_value(status, MEMORY_STATUSES, "active")
            )
        if keyword and keyword.strip():
            stmt = stmt.where(UserMemory.content.like(f"%{keyword.strip()}%"))
        stmt = stmt.order_by(UserMemory.update_time.desc())
        rows = (await self.db.execute(stmt)).scalars().all()
        return [to_out(r) for r in rows]

    async def create(
        self,
        content: str,
        memory_type: str | None,
        keywords: list[str] | None,
        confidence: float | None,
        agent_id: int | None,
    ) -> dict:
        normalized = normalize_content(content)
        await self._prune_for_new()
        now = datetime.now()
        row = UserMemory(
            id=new_id(),
            user_id=self.user_id,
            agent_id=agent_id,
            memory_type=_from_value(memory_type, MEMORY_TYPES, "preference"),
            content=normalized,
            keywords=normalize_keywords(keywords, normalized),
            confidence=(
                Decimal(str(confidence)) if confidence is not None else Decimal(str(DEFAULT_CONFIDENCE))
            ),
            status="active",
            create_time=now,
            update_time=now,
            deleted=0,
        )
        self.db.add(row)
        await self.db.commit()
        await self._refresh_embedding(row.id, normalized)
        return to_out(row)

    async def update(
        self,
        memory_id: int,
        content: str,
        memory_type: str | None,
        keywords: list[str] | None,
        confidence: float | None,
        agent_id: int | None,
    ) -> dict:
        row = await self._get_owned(memory_id)
        normalized = normalize_content(content)
        row.content = normalized
        row.memory_type = _from_value(memory_type, MEMORY_TYPES, "preference")
        row.keywords = normalize_keywords(keywords, normalized)
        row.confidence = (
            Decimal(str(confidence)) if confidence is not None else Decimal(str(DEFAULT_CONFIDENCE))
        )
        row.agent_id = agent_id
        row.update_time = datetime.now()
        await self.db.commit()
        await self._refresh_embedding(memory_id, normalized)
        return to_out(row)

    async def update_status(self, memory_id: int, status: str | None) -> dict:
        row = await self._get_owned(memory_id)
        row.status = _from_value(status, MEMORY_STATUSES, "active")
        row.update_time = datetime.now()
        await self.db.commit()
        return to_out(row)

    async def delete(self, memory_id: int) -> None:
        row = await self._get_owned(memory_id)
        # Java removeById → MP 逻辑删除（实体有 deleted 字段）
        row.deleted = 1
        row.update_time = datetime.now()
        await self.db.commit()

    async def _get_owned(self, memory_id: int) -> UserMemory:
        row = await self.db.get(UserMemory, memory_id)
        if row is None or row.deleted or row.user_id != self.user_id:
            raise LookupError("记忆不存在")
        return row

    async def _prune_for_new(self) -> None:
        """超 15 条挤掉最没用的：停用/归档优先，再按 confidence、lastUsedAt、updateTime。"""
        stmt = (
            select(UserMemory)
            .where(UserMemory.user_id == self.user_id)
            .where(UserMemory.deleted == 0)
        )
        rows = list((await self.db.execute(stmt)).scalars().all())
        overflow = len(rows) - MAX_USER_MEMORY_COUNT + 1
        if overflow <= 0:
            return

        def status_weight(s: str | None) -> int:
            # Java statusWeight：archived=0（最先删）> disabled=1 > active=2 ——
            # 活着的记忆比停用/归档的更值得留
            return {"archived": 0, "disabled": 1, "active": 2}.get(s or "active", 2)

        def ts(v: datetime | None) -> datetime:
            return v or datetime.min

        rows.sort(
            key=lambda r: (
                status_weight(r.status),
                float(r.confidence or 0),
                ts(r.last_used_at),
                ts(r.update_time),
            )
        )
        victims = [r.id for r in rows[:overflow]]
        await self.db.execute(
            sa_delete(UserMemory).where(UserMemory.id.in_(victims))
        )
        await self.db.commit()
        logger.info("[UserMemory] 裁剪 %d 条旧记忆 userId=%s", len(victims), self.user_id)

    async def _refresh_embedding(self, memory_id: int, content: str) -> None:
        """向量化写 embedding_vector。失败只 log —— 文本已落库，语义检索不到不算错误。"""
        try:
            from app.services.embedding_client import embed_query, to_pgvector

            vec = await embed_query(content)
            await self.db.execute(
                text(
                    "UPDATE user_memory SET embedding_vector = CAST(:v AS vector) "
                    "WHERE id = :id"
                ),
                {"v": to_pgvector(vec), "id": memory_id},
            )
            await self.db.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[UserMemory] 记忆向量生成失败 memoryId=%s: %s", memory_id, exc)
            # rollback 防止事务abort影响后续请求（连接复用）
            try:
                await self.db.rollback()
            except Exception:  # noqa: BLE001
                pass