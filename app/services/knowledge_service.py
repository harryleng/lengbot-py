"""知识库核心服务：CRUD + 权限 + 成员 + 配置 + 示例问题 + Advisor。

对照 Java：
- `KnowledgeServiceImpl`（1057 行）
- `KnowledgeMemberServiceImpl`
- `KnowledgeAdvisorServiceImpl`

## 三个必须照抄的业务规则

### 1. 权限是四级阶梯，不是布尔
`creator > manager > developer > viewer`（`KnowledgeRole.isAtLeast` 用 ordinal 比较）。
- 建库人自动成为 `creator`
- 删除库 = 仅 `creator`
- 改配置 / 拉人踢人 = `manager` 及以上
- 上传文档 / 增删改文档 / 问答对 / 图谱抽取 = `developer` 及以上
- 看详情 / 看成员 / 检索 / 提问 = 任意成员

**越权一律返回 404 而不是 403** —— 这是本项目既定口径（见阶段 13 的
`_ensure_owned`）。原因：403 会泄露"这个 ID 存在但不属于你"，
攻击者能靠状态码差异枚举出有效知识库 ID。

### 2. 名称全局唯一
Java 是 `count(name = ?) > 0` 就抛 —— **没有带 `deleted = 0` 条件**。
MyBatis-Plus 的 `@TableLogic` 会自动拼 `deleted = 0`，所以实际上只是"未删除的不重名"。
Python 侧显式写 `deleted == 0`，语义对齐。

### 3. 删除是级联 + 软失败
知识库删除会级联清掉 document / chunk / embedding / qa_pair / 图谱。
Java 的做法：
- 文档链**强耦合**，失败直接抛（否则主表已删、子表残留 → 数据不一致）
- qa_pair / 图谱是**可选子系统**，`safeRemove` 软失败不阻塞

照抄这个分层：文档链失败要能看到，可选子系统失败只打日志。

## 一个 Java 侧的真实 bug，Python 版修掉

`KnowledgeAdvisorMapper` 的休眠分块 SQL 写的是 `c.status = 'completed'`，
但 `chunk.status` 的枚举只有 `chunked / vectorizing / vectorized / failed`，
**没有 `completed`**。结果是休眠分块列表永远返回空 —— 而且不报错。
库里实测 3320 条 chunk 全是 `vectorized`，一条都匹配不上。

Python 版按真实枚举值查（`vectorized`），并在 docstring 里写清原因，
避免以后有人"对齐 Java"又改回去。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.snowflake import new_id
from app.models.entities import (
    Chunk,
    Document,
    Knowledge,
    KnowledgeMember,
    QaPair,
)
from app.schemas.common import PageResult
from app.schemas.knowledge import (
    ROLE_RANK,
    AdvisorSummaryOut,
    IngestConfigOut,
    KnowledgeIn,
    KnowledgeMemberOut,
    KnowledgeOut,
    LowRatedChunkOut,
    SleepingChunkOut,
    parse_json_value,
    to_json_text,
)

logger = logging.getLogger(__name__)

# 默认向量化模型。Java 侧 `knowledge.embedding_model` 有 DB 默认值
# 'text-embedding-3-small'，但实测库里那条链路用的是 bge-large-zh-v1.5
# （见 app/services/embedding_client.py 的模块 docstring）。
# 这里给 bge 作为默认，**不是**抄 DB 默认值 —— 用 DB 默认值会指向一个
# 从没被真正用过的模型名，用户建库后入库必然失败。
DEFAULT_EMBEDDING_MODEL = "bge-large-zh-v1.5"

# 默认分块参数（Java `ChunkParams` 的默认值）
DEFAULT_CHUNK_STRATEGY = "general"
DEFAULT_CHUNK_SIZE = 512
DEFAULT_CHUNK_OVERLAP = 10

# Advisor 内容预览长度上限（Java `PREVIEW_MAX_LENGTH`）
PREVIEW_MAX_LENGTH = 200

# 示例问题上限（Java `MAX_EXAMPLE_QUESTIONS`）
MAX_EXAMPLE_QUESTIONS = 10


def _now() -> datetime:
    return datetime.now()


def _not_found(msg: str = "知识库不存在") -> HTTPException:
    # 404 而不是 403：越权与不存在要给同一个响应，防 ID 枚举
    return HTTPException(status_code=404, detail=msg)


class KnowledgeService:
    def __init__(self, db: AsyncSession, user_id: int) -> None:
        self.db = db
        self.user_id = user_id

    # ------------------------------------------------------------------
    # 权限
    # ------------------------------------------------------------------

    async def _role_of(self, knowledge_id: int, user_id: int | None = None) -> str | None:
        uid = user_id if user_id is not None else self.user_id
        return (
            await self.db.execute(
                select(KnowledgeMember.role).where(
                    KnowledgeMember.knowledge_id == knowledge_id,
                    KnowledgeMember.user_id == uid,
                )
            )
        ).scalar_one_or_none()

    async def ensure_member(self, knowledge_id: int) -> str:
        """任意成员即可。返回角色。"""
        role = await self._role_of(knowledge_id)
        if role is None:
            raise _not_found()
        return role

    async def ensure_role(self, knowledge_id: int, required: str) -> str:
        """要求 `required` 及以上权限。返回实际角色。

        注意 `ROLE_RANK` 是"越小权限越大"，所以比较用 `<=`：
        实际角色 rank 3（viewer）要 `required` rank 2（developer）时，
        `3 <= 2` 为假 → 拒绝。写成 `>=` 就完全反了。
        """
        role = await self._role_of(knowledge_id)
        if role is None:
            raise _not_found()
        if ROLE_RANK.get(role, 99) > ROLE_RANK[required]:
            raise HTTPException(
                status_code=404,
                detail=f"需要{_role_desc(required)}及以上权限",
            )
        return role

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    async def _get_entity(self, knowledge_id: int) -> Knowledge:
        entity = (
            await self.db.execute(
                select(Knowledge).where(Knowledge.id == knowledge_id, Knowledge.deleted == 0)
            )
        ).scalar_one_or_none()
        if entity is None:
            raise _not_found()
        return entity

    async def _to_out(self, entity: Knowledge, *, role: str | None = None) -> KnowledgeOut:
        """实体 → 出参。jsonb 字段转 JSON 字符串，ID 转字符串。"""
        config = dict(entity.config or {})
        # Dify 的 token 密文绝不能出接口 —— Java `sanitizeForResponse` 同款处理
        if entity.type == "dify":
            config.pop("tokenCiphertext", None)
            config["tokenConfigured"] = True

        return KnowledgeOut(
            id=str(entity.id),
            user_id=str(entity.user_id),
            name=entity.name,
            description=entity.description,
            embedding_model=entity.embedding_model,
            type=entity.type,
            config=to_json_text(config),
            query_params=to_json_text(entity.query_params),
            mindmap_data=(
                to_json_text(entity.mindmap_data, "null") if entity.mindmap_data is not None else None
            ),
            example_questions=to_json_text(entity.example_questions, "[]"),
            document_count=entity.document_count or 0,
            chunk_count=entity.chunk_count or 0,
            total_tokens=entity.total_tokens or 0,
            status=entity.status,
            graph_enabled=bool(entity.graph_enabled),
            node_count=entity.node_count or 0,
            edge_count=entity.edge_count or 0,
            create_time=entity.create_time,
            update_time=entity.update_time,
            my_role=role,
        )

    async def page(
        self, page_num: int, page_size: int, name: str | None = None
    ) -> PageResult[KnowledgeOut]:
        """分页查询「当前用户有权限的」知识库。

        关键：**不是**查自己创建的，是查 `knowledge_member` 里所有加入的库。
        只按 `user_id` 过滤会漏掉别人拉你进去的库 —— 这是 Java 侧
        `listMyKnowledge` 的语义，必须对齐。
        """
        member_ids = select(KnowledgeMember.knowledge_id).where(
            KnowledgeMember.user_id == self.user_id
        )
        cond = [
            Knowledge.deleted == 0,
            Knowledge.status == "active",
            Knowledge.id.in_(member_ids),
        ]
        if name:
            cond.append(Knowledge.name.ilike(f"%{name}%"))

        total = (
            await self.db.execute(select(func.count()).select_from(Knowledge).where(*cond))
        ).scalar_one()

        rows = (
            (
                await self.db.execute(
                    select(Knowledge)
                    .where(*cond)
                    .order_by(Knowledge.create_time.desc())
                    .offset((page_num - 1) * page_size)
                    .limit(page_size)
                )
            )
            .scalars()
            .all()
        )

        # 一次查出当前用户在所有命中的库里的角色，避免 N+1
        ids = [r.id for r in rows]
        role_map: dict[int, str] = {}
        if ids:
            role_rows = (
                await self.db.execute(
                    select(KnowledgeMember.knowledge_id, KnowledgeMember.role).where(
                        KnowledgeMember.user_id == self.user_id,
                        KnowledgeMember.knowledge_id.in_(ids),
                    )
                )
            ).all()
            role_map = {int(k): v for k, v in role_rows}

        records = [await self._to_out(r, role=role_map.get(r.id)) for r in rows]
        return PageResult.of(records, total, page_num, page_size)

    async def get(self, knowledge_id: int) -> KnowledgeOut:
        role = await self.ensure_member(knowledge_id)
        return await self._to_out(await self._get_entity(knowledge_id), role=role)

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    async def _check_name_unique(self, name: str, exclude_id: int | None = None) -> None:
        cond = [Knowledge.name == name, Knowledge.deleted == 0]
        if exclude_id is not None:
            cond.append(Knowledge.id != exclude_id)
        count = (await self.db.execute(select(func.count()).select_from(Knowledge).where(*cond))).scalar_one()
        if count > 0:
            raise HTTPException(status_code=400, detail="知识库名称已存在")

    async def create(self, dto: KnowledgeIn) -> KnowledgeOut:
        await self._check_name_unique(dto.name)

        now = _now()
        config = parse_json_value(dto.config, {})
        if dto.type == "dify":
            config = self._build_dify_config(dto.dify_config, config, token_required=True)

        kid = new_id()
        entity = Knowledge(
            id=kid,
            user_id=self.user_id,
            name=dto.name,
            description=dto.description,
            embedding_model=dto.embedding_model or DEFAULT_EMBEDDING_MODEL,
            type=dto.type,
            config=config,
            query_params=parse_json_value(dto.query_params, {}),
            document_count=0,
            chunk_count=0,
            total_tokens=0,
            status="active",
            example_questions=[],
            # Dify 是外部只读库，没有本地图谱可抽取
            graph_enabled=False if dto.type == "dify" else bool(dto.graph_enabled),
            node_count=0,
            edge_count=0,
            create_time=now,
            update_time=now,
            deleted=0,
        )
        self.db.add(entity)

        # 创建者自动成为 CREATOR —— 少了这一步，建完库立刻访问详情就 404
        self.db.add(
            KnowledgeMember(
                id=new_id(),
                knowledge_id=kid,
                user_id=self.user_id,
                role="creator",
                create_time=now,
                update_time=now,
            )
        )
        await self.db.commit()
        await self.db.refresh(entity)
        return await self._to_out(entity, role="creator")

    def _build_dify_config(
        self, dify_config: dict | None, current: dict, *, token_required: bool
    ) -> dict:
        """构造 Dify 连接配置。

        Java 侧会 `DifySecretCipher.encrypt(token)` 落密文；Python 版
        **不做加密**，与 2026-09-14 撤销 `SecretCipher` 的决策一致
        （见项目 MEMORY：`LENGBOT_CIPHER_KEY` 缺失会导致 fail-closed 报错）。
        因此直接落明文 `token`，出参时由 `_to_out` 摘掉。
        """
        cfg = dify_config or {}
        api_url = str(cfg.get("apiUrl") or cfg.get("api_url") or "").strip().rstrip("/")
        dataset_id = str(cfg.get("datasetId") or cfg.get("dataset_id") or "").strip()
        token = str(cfg.get("token") or "").strip()

        if not api_url or not dataset_id:
            raise HTTPException(status_code=400, detail="Dify 配置不完整：apiUrl / datasetId 必填")
        if token_required and not token and not current.get("token") and not current.get("tokenCiphertext"):
            raise HTTPException(status_code=400, detail="Dify 配置不完整：token 必填")

        return {
            "connector": "dify",
            "apiUrl": api_url,
            "datasetId": dataset_id,
            "token": token or current.get("token") or current.get("tokenCiphertext") or "",
            "tokenConfigured": True,
            "readOnly": True,
        }

    async def update(self, dto: KnowledgeIn) -> KnowledgeOut:
        if not dto.id:
            raise HTTPException(status_code=400, detail="缺少 id")
        kid = int(dto.id)
        await self.ensure_role(kid, "manager")
        entity = await self._get_entity(kid)

        if entity.name != dto.name:
            await self._check_name_unique(dto.name, exclude_id=kid)

        entity.name = dto.name
        entity.description = dto.description
        if entity.type == "dify":
            # Dify 库不允许改 embeddingModel / graphEnabled（它不本地向量化）
            entity.embedding_model = None  # type: ignore[assignment]
            entity.graph_enabled = False
            entity.config = self._build_dify_config(
                dto.dify_config, entity.config or {}, token_required=False
            )
        else:
            if dto.embedding_model:
                entity.embedding_model = dto.embedding_model
            if dto.graph_enabled is not None:
                entity.graph_enabled = dto.graph_enabled
            if dto.config is not None:
                entity.config = parse_json_value(dto.config, {})
        entity.update_time = _now()
        await self.db.commit()
        await self.db.refresh(entity)
        return await self._to_out(entity)

    async def delete(self, knowledge_id: int) -> None:
        """级联删除。仅 creator 可操作。

        分层策略照抄 Java：
        - 文档链（chunk / embedding / document）失败要抛出
        - qa_pair 软失败
        """
        await self.ensure_role(knowledge_id, "creator")
        entity = await self._get_entity(knowledge_id)
        now = _now()

        # 1) 文档链：先删向量，再删 chunk，最后软删文档
        doc_ids = (
            (
                await self.db.execute(
                    select(Document.id).where(
                        Document.knowledge_id == knowledge_id, Document.deleted == 0
                    )
                )
            )
            .scalars()
            .all()
        )
        if doc_ids:
            await self.db.execute(
                text(
                    "DELETE FROM embedding WHERE chunk_id IN "
                    "(SELECT id FROM chunk WHERE document_id = ANY(:ids))"
                ),
                {"ids": list(doc_ids)},
            )
            await self.db.execute(delete(Chunk).where(Chunk.document_id.in_(doc_ids)))
            await self.db.execute(
                update(Document)
                .where(Document.id.in_(doc_ids))
                .values(deleted=1, update_time=now)
            )

        # 2) 问答对：可选子系统，软失败
        try:
            await self.db.execute(
                text(
                    "DELETE FROM embedding WHERE qa_pair_id IN "
                    "(SELECT id FROM qa_pair WHERE knowledge_id = :kid)"
                ),
                {"kid": knowledge_id},
            )
            await self.db.execute(
                update(QaPair)
                .where(QaPair.knowledge_id == knowledge_id, QaPair.deleted == 0)
                .values(deleted=1, update_time=now)
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[知识库删除] qa_pair 清理失败，跳过: kid=%s err=%s", knowledge_id, exc)

        # 3) 图谱（Neo4j 侧的点/边不在 Python 管辖范围，只清 PG 台账）
        try:
            await self.db.execute(
                text("UPDATE knowledge_graph SET deleted = 1, update_time = :now WHERE knowledge_id = :kid"),
                {"kid": knowledge_id, "now": now},
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[知识库删除] 图谱台账清理失败，跳过: kid=%s err=%s", knowledge_id, exc)

        # 4) 软删主表 + 清成员
        entity.deleted = 1
        entity.update_time = now
        await self.db.execute(
            delete(KnowledgeMember).where(KnowledgeMember.knowledge_id == knowledge_id)
        )
        await self.db.commit()

    async def refresh_stats(self, knowledge_id: int) -> None:
        """全量重算 document_count / chunk_count / total_tokens。

        为什么需要这个接口：入库是异步的，中途失败会让计数器漂移
        （`updateStats` 已经加过 1，但文档最终 failed）。前端有个
        「修复统计」按钮就是调它。
        """
        await self.ensure_member(knowledge_id)
        entity = await self._get_entity(knowledge_id)

        row = (
            await self.db.execute(
                select(
                    func.count(Document.id),
                    func.coalesce(func.sum(Document.token_count), 0),
                ).where(Document.knowledge_id == knowledge_id, Document.deleted == 0)
            )
        ).one()
        doc_count = int(row[0] or 0)
        token_count = int(row[1] or 0)

        chunk_count = (
            await self.db.execute(
                select(func.count(Chunk.id)).where(Chunk.knowledge_id == knowledge_id)
            )
        ).scalar_one()

        entity.document_count = doc_count
        entity.chunk_count = int(chunk_count or 0)
        entity.total_tokens = token_count
        entity.update_time = _now()
        await self.db.commit()
        logger.info(
            "[知识库统计] 重算完成 kid=%s docs=%s chunks=%s tokens=%s",
            knowledge_id,
            doc_count,
            chunk_count,
            token_count,
        )

    # ------------------------------------------------------------------
    # 成员
    # ------------------------------------------------------------------

    async def list_members(self, knowledge_id: int) -> list[KnowledgeMemberOut]:
        await self.ensure_member(knowledge_id)
        rows = (
            await self.db.execute(
                text(
                    """
                    SELECT m.id, m.knowledge_id, m.user_id, m.role, m.create_time,
                           u.username, u.nickname, u.avatar
                    FROM knowledge_member m
                    LEFT JOIN users u ON u.id = m.user_id
                    WHERE m.knowledge_id = :kid
                    ORDER BY CASE m.role
                                 WHEN 'creator' THEN 0 WHEN 'manager' THEN 1
                                 WHEN 'developer' THEN 2 ELSE 3 END,
                             m.create_time
                    """
                ),
                {"kid": knowledge_id},
            )
        ).mappings().all()
        return [
            KnowledgeMemberOut(
                id=str(r["id"]),
                knowledge_id=str(r["knowledge_id"]),
                user_id=str(r["user_id"]),
                role=r["role"],
                username=r["username"],
                nickname=r["nickname"],
                avatar=r["avatar"],
                create_time=r["create_time"],
            )
            for r in rows
        ]

    async def add_member(self, knowledge_id: int, user_id: int, role: str) -> None:
        await self.ensure_role(knowledge_id, "manager")
        if role not in ROLE_RANK:
            raise HTTPException(status_code=400, detail=f"未知角色: {role}")
        if role == "creator":
            # 不允许通过"加成员"造出第二个 creator —— 删库权限只能有一个入口
            raise HTTPException(status_code=400, detail="不能直接添加创建者角色")

        exists = (
            await self.db.execute(
                select(KnowledgeMember.id).where(
                    KnowledgeMember.knowledge_id == knowledge_id,
                    KnowledgeMember.user_id == user_id,
                )
            )
        ).scalar_one_or_none()
        if exists is not None:
            raise HTTPException(status_code=400, detail="该用户已是知识库成员")

        now = _now()
        self.db.add(
            KnowledgeMember(
                id=new_id(),
                knowledge_id=knowledge_id,
                user_id=user_id,
                role=role,
                create_time=now,
                update_time=now,
            )
        )
        await self.db.commit()

    async def update_member_role(self, knowledge_id: int, user_id: int, role: str) -> None:
        await self.ensure_role(knowledge_id, "manager")
        if role not in ROLE_RANK:
            raise HTTPException(status_code=400, detail=f"未知角色: {role}")

        member = (
            await self.db.execute(
                select(KnowledgeMember).where(
                    KnowledgeMember.knowledge_id == knowledge_id,
                    KnowledgeMember.user_id == user_id,
                )
            )
        ).scalar_one_or_none()
        if member is None:
            raise _not_found("成员不存在")
        if member.role == "creator":
            # 创建者不可被降级 —— 否则库会变成"谁也删不掉"的孤儿
            raise HTTPException(status_code=400, detail="不能修改创建者的角色")

        member.role = role
        member.update_time = _now()
        await self.db.commit()

    async def remove_member(self, knowledge_id: int, user_id: int) -> None:
        await self.ensure_role(knowledge_id, "manager")
        member = (
            await self.db.execute(
                select(KnowledgeMember).where(
                    KnowledgeMember.knowledge_id == knowledge_id,
                    KnowledgeMember.user_id == user_id,
                )
            )
        ).scalar_one_or_none()
        if member is None:
            raise _not_found("成员不存在")
        if member.role == "creator":
            raise HTTPException(status_code=400, detail="不能移除创建者")
        await self.db.execute(
            delete(KnowledgeMember).where(KnowledgeMember.id == member.id)
        )
        await self.db.commit()

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------

    async def get_ingest_config(self, knowledge_id: int) -> IngestConfigOut:
        entity = await self._get_entity(knowledge_id)
        cfg = entity.config or {}
        return IngestConfigOut(
            chunk_strategy=str(cfg.get("defaultChunkStrategy") or DEFAULT_CHUNK_STRATEGY),
            chunk_size=int(cfg.get("defaultChunkSize") or DEFAULT_CHUNK_SIZE),
            chunk_overlap=int(cfg.get("defaultChunkOverlap") or DEFAULT_CHUNK_OVERLAP),
            chunk_delimiter=str(cfg.get("defaultChunkDelimiter") or ""),
            collapse_whitespace=bool(cfg.get("defaultCollapseWhitespace") or False),
        )

    async def get_query_params(self, knowledge_id: int) -> dict[str, Any]:
        """读检索配置。空则回退到 config 里的老字段（Java 兼容逻辑）。"""
        await self.ensure_member(knowledge_id)
        entity = await self._get_entity(knowledge_id)

        params = dict(entity.query_params or {})
        if params:
            return params

        # 兼容旧数据：早期只有 config.ragTopK / ragThreshold
        cfg = entity.config or {}
        fallback: dict[str, Any] = {}
        if "ragTopK" in cfg:
            fallback["final_top_k"] = cfg["ragTopK"]
        if "ragThreshold" in cfg:
            fallback["similarity_threshold"] = cfg["ragThreshold"]
        return fallback

    async def update_query_params(self, knowledge_id: int, params: dict[str, Any]) -> dict[str, Any]:
        """保存检索配置，保存前按库类型归一化。

        **归一化不是"清洗脏数据"，是核心业务逻辑**：PG 和 Milvus 的参数集合
        不同（Milvus 有 bm25_* / graph_*，PG 有 keyword_weight），
        不按类型过滤的话，用户在 Milvus 库填的 bm25_weight 会被带进 PG 库，
        检索链路读到一个不认识的 key 就直接忽略 —— 配了等于没配，
        而且 UI 上还显示"已保存"。
        """
        await self.ensure_role(knowledge_id, "manager")
        entity = await self._get_entity(knowledge_id)

        normalized = normalize_query_params(entity.type, params or {})
        entity.query_params = normalized
        entity.update_time = _now()
        await self.db.commit()
        return normalized

    # ------------------------------------------------------------------
    # 示例问题
    # ------------------------------------------------------------------

    async def get_example_questions(self, knowledge_id: int) -> list[str]:
        await self.ensure_member(knowledge_id)
        entity = await self._get_entity(knowledge_id)
        raw = entity.example_questions
        if isinstance(raw, list):
            return [str(x) for x in raw]
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
                return [str(x) for x in parsed] if isinstance(parsed, list) else []
            except json.JSONDecodeError:
                return []
        return []

    async def update_example_questions(self, knowledge_id: int, questions: list[str]) -> None:
        """全量替换。空串去掉，**超 10 条时删最早的（保留新的）**。

        Java 是 `while (size > 10) remove(0)` —— 保留末尾。
        写成 `[:10]` 就反了：用户新加的会被截掉，老的反而留下。
        """
        await self.ensure_role(knowledge_id, "developer")
        entity = await self._get_entity(knowledge_id)
        cleaned = [q.strip() for q in questions if q and q.strip()]
        while len(cleaned) > MAX_EXAMPLE_QUESTIONS:
            cleaned.pop(0)
        entity.example_questions = cleaned
        entity.update_time = _now()
        await self.db.commit()

    async def append_example_question(self, knowledge_id: int, question: str) -> bool:
        """追加一条（去重 + 限 10）。被 `example_question_service` 复用。"""
        q = (question or "").strip()
        if not q:
            return False
        entity = await self._get_entity(knowledge_id)
        current = entity.example_questions
        questions = [str(x) for x in current] if isinstance(current, list) else []
        if q in questions:
            return False
        questions.append(q)
        while len(questions) > MAX_EXAMPLE_QUESTIONS:
            questions.pop(0)
        entity.example_questions = questions
        entity.update_time = _now()
        await self.db.commit()
        return True

    # ------------------------------------------------------------------
    # 思维导图
    # ------------------------------------------------------------------

    async def get_mindmap(self, knowledge_id: int) -> Any:
        await self.ensure_member(knowledge_id)
        entity = await self._get_entity(knowledge_id)
        return entity.mindmap_data

    # ------------------------------------------------------------------
    # Advisor（反馈聚合）
    # ------------------------------------------------------------------

    async def advisor_summary(self, knowledge_id: int, window_days: int = 14) -> AdvisorSummaryOut:
        """反馈概览。数据源是 `message.metadata.ragReferences` ⨝ `message_feedback`。

        `ragReferences` 里的 `knowledgeId` / `chunkId` 都是**字符串**
        （前端 ToStringSerializer 序列化的结果），所以 SQL 里要 `->>` 取文本比较，
        不能 `->` 取 jsonb 再和数字比 —— 类型对不上，静默返回 0 行。
        """
        await self.ensure_member(knowledge_id)
        days = window_days if window_days > 0 else 14
        kid = str(knowledge_id)

        row = (
            await self.db.execute(
                text(
                    """
                    SELECT
                        COUNT(DISTINCT m.id)                                             AS total_references,
                        COUNT(DISTINCT mf.id) FILTER (WHERE mf.rating = 'like')          AS total_likes,
                        COUNT(DISTINCT mf.id) FILTER (WHERE mf.id IS NOT NULL)           AS total_feedback,
                        COUNT(DISTINCT ref->>'chunkId')  AS referenced_chunk_count
                    FROM message m
                    CROSS JOIN LATERAL jsonb_array_elements(
                        CASE WHEN jsonb_typeof(m.metadata->'ragReferences') = 'array'
                             THEN m.metadata->'ragReferences' ELSE '[]'::jsonb END
                    ) AS ref
                    LEFT JOIN message_feedback mf ON mf.message_id = m.id
                    WHERE m.metadata->'ragReferences' @> jsonb_build_array(
                              jsonb_build_object('knowledgeId', CAST(:kid AS text)))
                      AND ref->>'knowledgeId' = CAST(:kid AS text)
                    """
                ),
                {"kid": kid},
            )
        ).mappings().one()

        total_references = int(row["total_references"] or 0)
        total_likes = int(row["total_likes"] or 0)
        total_feedback = int(row["total_feedback"] or 0)
        referenced_chunks = int(row["referenced_chunk_count"] or 0)
        # Java 用 totalFeedback - totalLikes 推 dislike（含 reason 但不含 rating 的会算进来，
        # 实际数据里只有 like/dislike 两种 rating，等价）
        total_dislikes = max(total_feedback - total_likes, 0)
        like_rate = (total_likes / total_feedback) if total_feedback > 0 else 0.0

        sleeping = await self._count_sleeping_chunks(knowledge_id, days)
        return AdvisorSummaryOut(
            knowledge_id=str(knowledge_id),
            total_references=total_references,
            total_likes=total_likes,
            total_dislikes=total_dislikes,
            like_rate=round(like_rate, 4),
            referenced_chunk_count=referenced_chunks,
            sleeping_chunk_count=sleeping,
            window_days=days,
        )

    async def _ref_stats_cte(self) -> str:
        """引用统计 CTE。抽出来避免三段 SQL 各写一遍写歪。"""
        return """
            SELECT (ref->>'chunkId')::bigint AS chunk_id,
                   MAX(m.create_time)        AS last_referenced_at,
                   COUNT(DISTINCT m.id)      AS reference_count
            FROM message m
            CROSS JOIN LATERAL jsonb_array_elements(
                CASE WHEN jsonb_typeof(m.metadata->'ragReferences') = 'array'
                     THEN m.metadata->'ragReferences' ELSE '[]'::jsonb END
            ) AS ref
            WHERE ref->>'knowledgeId' = CAST(:kid AS text)
              AND ref->>'chunkId' ~ '^[0-9]+$'
            GROUP BY ref->>'chunkId'
        """

    async def _count_sleeping_chunks(self, knowledge_id: int, days: int) -> int:
        # `CAST(:days AS integer)` 不能省：`CURRENT_DATE - (:days * INTERVAL '1 day')`
        # 里 asyncpg 推断不出 :days 的类型，报 AmbiguousParameterError → 500。
        # 这类错误只在"多参数混用"时出现，单跑 SQL 客户端看不出来。
        sql = f"""
            WITH ref_stats AS ({await self._ref_stats_cte()})
            SELECT COUNT(*)
            FROM chunk c
            LEFT JOIN ref_stats rs ON rs.chunk_id = c.id
            WHERE c.knowledge_id = :kid_int
              AND c.status = 'vectorized'
              AND (rs.last_referenced_at IS NULL
                   OR rs.last_referenced_at < CURRENT_DATE - (CAST(:days AS integer) * INTERVAL '1 day'))
        """
        return int(
            (
                await self.db.execute(
                    text(sql), {"kid": str(knowledge_id), "kid_int": knowledge_id, "days": days}
                )
            ).scalar_one()
            or 0
        )

    async def advisor_low_rated(
        self, knowledge_id: int, limit: int = 10
    ) -> list[LowRatedChunkOut]:
        await self.ensure_member(knowledge_id)
        cap = min(limit, 50) if limit > 0 else 10

        rows = (
            await self.db.execute(
                text(
                    """
                    SELECT (ref->>'chunkId')::bigint                              AS chunk_id,
                           COUNT(DISTINCT m.id)                                   AS reference_count,
                           COUNT(DISTINCT mf.id) FILTER (WHERE mf.rating = 'like')    AS like_count,
                           COUNT(DISTINCT mf.id) FILTER (WHERE mf.rating = 'dislike') AS dislike_count,
                           MAX(m.create_time)                                     AS last_referenced_at
                    FROM message m
                    CROSS JOIN LATERAL jsonb_array_elements(
                        CASE WHEN jsonb_typeof(m.metadata->'ragReferences') = 'array'
                             THEN m.metadata->'ragReferences' ELSE '[]'::jsonb END
                    ) AS ref
                    LEFT JOIN message_feedback mf ON mf.message_id = m.id
                    WHERE ref->>'knowledgeId' = CAST(:kid AS text)
                      AND ref->>'chunkId' ~ '^[0-9]+$'
                    GROUP BY ref->>'chunkId'
                    HAVING COUNT(DISTINCT mf.id) FILTER (WHERE mf.rating = 'dislike') > 0
                    ORDER BY dislike_count DESC, like_count ASC, reference_count DESC
                    LIMIT :lim
                    """
                ),
                {"kid": str(knowledge_id), "lim": cap},
            )
        ).mappings().all()
        if not rows:
            return []

        chunk_ids = [int(r["chunk_id"]) for r in rows]
        info = await self._chunk_preview_map(chunk_ids)

        out: list[LowRatedChunkOut] = []
        for r in rows:
            cid = int(r["chunk_id"])
            meta = info.get(cid, {})
            likes = int(r["like_count"] or 0)
            dislikes = int(r["dislike_count"] or 0)
            total = likes + dislikes
            out.append(
                LowRatedChunkOut(
                    chunk_id=str(cid),
                    document_id=str(meta["document_id"]) if meta.get("document_id") else None,
                    document_name=meta.get("document_name"),
                    content_preview=meta.get("preview"),
                    like_count=likes,
                    dislike_count=dislikes,
                    dislike_rate=round(dislikes / total, 4) if total else 0.0,
                    reference_count=int(r["reference_count"] or 0),
                    last_referenced_at=r["last_referenced_at"],
                )
            )
        return out

    async def advisor_sleeping(
        self, knowledge_id: int, days: int = 14, limit: int = 10
    ) -> list[SleepingChunkOut]:
        """休眠分块：最近 `days` 天内没被引用（含从未引用）。

        **这里修掉了 Java 的 `status='completed'` bug** —— 见模块 docstring。
        """
        await self.ensure_member(knowledge_id)
        cap = min(limit, 50) if limit > 0 else 10
        d = days if days > 0 else 14

        sql = f"""
            WITH ref_stats AS ({await self._ref_stats_cte()})
            SELECT c.id            AS chunk_id,
                   c.document_id   AS document_id,
                   c.content       AS content,
                   c.create_time   AS chunk_create_time,
                   rs.last_referenced_at,
                   COALESCE(rs.reference_count, 0) AS reference_count
            FROM chunk c
            LEFT JOIN ref_stats rs ON rs.chunk_id = c.id
            WHERE c.knowledge_id = :kid_int
              AND c.status = 'vectorized'
              AND (rs.last_referenced_at IS NULL
                   OR rs.last_referenced_at < CURRENT_DATE - (CAST(:days AS integer) * INTERVAL '1 day'))
            ORDER BY rs.last_referenced_at NULLS FIRST, c.create_time ASC
            LIMIT :lim
        """
        rows = (
            await self.db.execute(
                text(sql),
                {"kid": str(knowledge_id), "kid_int": knowledge_id, "days": d, "lim": cap},
            )
        ).mappings().all()
        if not rows:
            return []

        doc_ids = {int(r["document_id"]) for r in rows if r["document_id"]}
        name_map = await self._doc_name_map(doc_ids)

        now = _now()
        out: list[SleepingChunkOut] = []
        for r in rows:
            last = r["last_referenced_at"]
            created = r["chunk_create_time"]
            baseline = last or created
            sleeping_days = (now - baseline).days if baseline else 0
            content = str(r["content"] or "")
            out.append(
                SleepingChunkOut(
                    chunk_id=str(r["chunk_id"]),
                    document_id=str(r["document_id"]) if r["document_id"] else None,
                    document_name=name_map.get(int(r["document_id"])) if r["document_id"] else None,
                    content_preview=(
                        content[:PREVIEW_MAX_LENGTH] + "…" if len(content) > PREVIEW_MAX_LENGTH else content
                    ),
                    chunk_create_time=created,
                    last_referenced_at=last,
                    sleeping_days=max(sleeping_days, 0),
                    reference_count=int(r["reference_count"] or 0),
                )
            )
        return out

    async def _chunk_preview_map(self, chunk_ids: list[int]) -> dict[int, dict[str, Any]]:
        if not chunk_ids:
            return {}
        rows = (
            await self.db.execute(
                text(
                    """
                    SELECT c.id, c.document_id, c.content, d.name AS document_name
                    FROM chunk c
                    LEFT JOIN document d ON d.id = c.document_id
                    WHERE c.id = ANY(:ids)
                    """
                ),
                {"ids": chunk_ids},
            )
        ).mappings().all()
        result: dict[int, dict[str, Any]] = {}
        for r in rows:
            content = str(r["content"] or "")
            result[int(r["id"])] = {
                "document_id": r["document_id"],
                "document_name": r["document_name"],
                "preview": (
                    content[:PREVIEW_MAX_LENGTH] + "…"
                    if len(content) > PREVIEW_MAX_LENGTH
                    else content
                ),
            }
        return result

    async def _doc_name_map(self, doc_ids: set[int]) -> dict[int, str]:
        if not doc_ids:
            return {}
        rows = (
            await self.db.execute(
                select(Document.id, Document.name).where(Document.id.in_(list(doc_ids)))
            )
        ).all()
        return {int(i): n for i, n in rows}


def _role_desc(role: str) -> str:
    return {"creator": "创建者", "manager": "管理者", "developer": "开发者", "viewer": "查看者"}.get(
        role, role
    )


# ----------------------------------------------------------------------
# 检索参数归一化
# ----------------------------------------------------------------------

# PG / Milvus / Dify 三套字段集合，刻意分开写死。
# 用"按类型过滤"而不是"统一保留全部"，是因为多出来的 key 会让检索链路
# 读到不认识的参数后静默走默认值 —— 用户以为自己配了，其实没生效。
_PG_FIELDS = (
    "search_mode",
    "final_top_k",
    "similarity_threshold",
    "query_rewrite",
    "vector_weight",
    "keyword_weight",
    "use_reranker",
    "reranker_model",
    "recall_top_k",
    "qa_enabled",
    "qa_top_k",
    "qa_threshold",
    "qa_priority",
    "use_es_bm25",
    "es_weight",
)

_MILVUS_EXTRA = (
    "bm25_weight",
    "bm25_top_k",
    "bm25_drop_ratio_search",
    "use_graph_retrieval",
    "graph_entity_top_k",
    "graph_triple_top_k",
    "graph_max_nodes",
    "graph_top_k",
    "graph_weight",
    "ppr_damping",
    "ppr_iterations",
    "rrf_k",
    "use_community_retrieval",
    "graph_community_top_k",
    "community_weight",
)


def _clamp_int(src: dict[str, Any], key: str, default: int, lo: int, hi: int) -> int:
    val = src.get(key, default)
    try:
        num = int(val)
    except (TypeError, ValueError):
        num = default
    return max(lo, min(hi, num))


def _clamp_float(src: dict[str, Any], key: str, default: float, lo: float, hi: float) -> float:
    val = src.get(key, default)
    try:
        num = float(val)
    except (TypeError, ValueError):
        num = default
    return max(lo, min(hi, num))


def _as_bool(src: dict[str, Any], key: str, default: bool) -> bool:
    val = src.get(key, default)
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().lower() in {"true", "1", "yes", "on"}
    if isinstance(val, (int, float)):
        return bool(val)
    return default


def _as_str(src: dict[str, Any], key: str, default: str = "") -> str:
    val = src.get(key, default)
    return val.strip() if isinstance(val, str) else default


def normalize_query_params(kb_type: str, src: dict[str, Any]) -> dict[str, Any]:
    """按库类型归一化检索配置（对齐 Java `normalizeQueryParams`）。"""
    mode = _as_str(src, "search_mode", "hybrid").lower()
    if mode not in {"vector", "keyword", "hybrid"}:
        mode = "hybrid"

    if kb_type == "dify":
        # Dify 是外部托管检索，只暴露它真正认的四个参数
        return {
            "search_mode": mode,
            "final_top_k": _clamp_int(src, "final_top_k", 5, 1, 20),
            "score_threshold_enabled": _as_bool(src, "score_threshold_enabled", True),
            "similarity_threshold": _clamp_float(src, "similarity_threshold", 0.0, 0.0, 1.0),
        }

    is_milvus = kb_type == "milvus"
    out: dict[str, Any] = {
        "search_mode": mode,
        "final_top_k": _clamp_int(src, "final_top_k", 10 if is_milvus else 5, 1, 100),
        "similarity_threshold": _clamp_float(
            src, "similarity_threshold", 0.0 if is_milvus else 0.5, 0.0, 1.0
        ),
        "query_rewrite": _as_bool(src, "query_rewrite", False),
        "vector_weight": _clamp_float(src, "vector_weight", 0.7, 0.0, 1.0),
    }

    if is_milvus:
        out["bm25_weight"] = _clamp_float(src, "bm25_weight", 0.3, 0.0, 1.0)
        out["bm25_top_k"] = _clamp_int(src, "bm25_top_k", 30, 1, 200)
        out["bm25_drop_ratio_search"] = _clamp_float(src, "bm25_drop_ratio_search", 0.0, 0.0, 1.0)
    else:
        out["keyword_weight"] = _clamp_float(src, "keyword_weight", 0.3, 0.0, 1.0)

    out["use_reranker"] = _as_bool(src, "use_reranker", False)
    out["reranker_model"] = _as_str(src, "reranker_model", "")
    out["recall_top_k"] = _clamp_int(src, "recall_top_k", 50, 1, 200)
    out["qa_enabled"] = _as_bool(src, "qa_enabled", True)
    out["qa_top_k"] = _clamp_int(src, "qa_top_k", 3, 1, 20)
    out["qa_threshold"] = _clamp_float(src, "qa_threshold", 0.85, 0.0, 1.0)
    out["qa_priority"] = _as_bool(src, "qa_priority", True)
    out["use_es_bm25"] = _as_bool(src, "use_es_bm25", False)
    out["es_weight"] = _clamp_float(src, "es_weight", 0.3, 0.0, 1.0)

    if is_milvus:
        graph_on = _as_bool(src, "use_graph_retrieval", False)
        out["use_graph_retrieval"] = graph_on
        out["graph_entity_top_k"] = _clamp_int(src, "graph_entity_top_k", 10, 1, 100)
        out["graph_triple_top_k"] = _clamp_int(src, "graph_triple_top_k", 10, 1, 100)
        out["graph_max_nodes"] = _clamp_int(src, "graph_max_nodes", 100, 10, 500)
        out["graph_top_k"] = _clamp_int(src, "graph_top_k", 5, 1, 50)
        out["graph_weight"] = _clamp_float(src, "graph_weight", 0.3, 0.0, 1.0)
        out["ppr_damping"] = _clamp_float(src, "ppr_damping", 0.85, 0.0, 1.0)
        out["ppr_iterations"] = _clamp_int(src, "ppr_iterations", 15, 1, 100)
        out["rrf_k"] = _clamp_int(src, "rrf_k", 60, 1, 200)
        # 社区摘要默认跟随图检索开关，用户可单独关
        out["use_community_retrieval"] = _as_bool(src, "use_community_retrieval", graph_on)
        out["graph_community_top_k"] = _clamp_int(src, "graph_community_top_k", 3, 1, 50)
        out["community_weight"] = _clamp_float(src, "community_weight", 0.3, 0.0, 1.0)

    return out
