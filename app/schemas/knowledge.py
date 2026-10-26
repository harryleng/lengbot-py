"""知识库相关的 schema。

## 最大的坑：jsonb 字段到底是对象还是字符串

库里 `knowledge.config` / `query_params` / `mindmap_data` / `example_questions`
都是 **jsonb**。Java 侧用 `JsonbTypeHandler` 把它们映射成 `String` 字段，
Jackson 序列化出来的就是**字符串**，例如：

    {"config": "{\"defaultChunkSize\":512}", "exampleQuestions": "[\"问题1\"]"}

前端 `KnowledgeDetail.vue` 就是按字符串消费的：

    const config = typeof k.config === 'string' ? JSON.parse(k.config) : k.config || {}
    const eq = res.data.exampleQuestions
    if (eq) { const parsed = typeof eq === 'string' ? JSON.parse(eq) : eq; ... }

看起来它做了双分支、给对象也能跑。**但 `exampleQuestions` 那一支有个隐藏差异**：
真给数组 `["a"]` 时 `typeof` 是 `object` 走 `else` 分支，`Array.isArray(parsed)` 成立，
勉强能用；而 `config` 给对象时 `|| {}` 也成立 —— 所以对象**不一定**白屏。

真正会坏的是另一批消费方：任何直接 `JSON.parse(knowledge.config)` 而不做
typeof 判断的代码（比如 `Knowledge.vue` 的卡片摘要、导出逻辑）。这种地方
拿到对象会直接抛 `Unexpected token o in JSON at position 1`。

因此这里统一 **jsonb → JSON 字符串**，对齐 Java 的对外契约。
这不是"随便选一个"，是照抄 Java 的出参形态；不一致就会在某个角落炸。

## 入参则相反

前端提交时 `createKnowledge(data)` 传的是**对象**（`config` 是对象、`queryParams` 是对象），
因为表单组件天然产出对象。所以入参用 `dict | str | None` 双兼容：
传对象就存对象，传字符串就先 parse。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from pydantic import Field, field_validator

from app.schemas.common import CamelModel

# 与 Java `KnowledgeServiceImpl.create` 的 `@Size(max = 50)` 对齐
KNOWLEDGE_NAME_MAX = 50
KNOWLEDGE_DESC_MAX = 50

VALID_TYPES = ("pg", "milvus", "dify")
VALID_ROLES = ("creator", "manager", "developer", "viewer")
# 权限等级：ordinal 越小权限越大（对齐 Java `KnowledgeRole.isAtLeast` 的 ordinal 语义）
ROLE_RANK = {"creator": 0, "manager": 1, "developer": 2, "viewer": 3}


def to_json_text(value: Any, default: str = "{}") -> str:
    """jsonb 值 → 给前端的 JSON 字符串。"""
    if value is None:
        return default
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return default


def parse_json_value(value: Any, default: Any = None) -> Any:
    """入参归一：对象原样返回，字符串尝试 parse，失败给默认值。

    注意 `default=None` 时返回 `{}`（不是 None）—— Java 的
    `JsonUtil.parseJsonToMap` 解析失败也返回空 Map，保持同构。
    """
    if value is None:
        return {} if default is None else default
    if isinstance(value, (dict, list)):
        return value
    text = str(value).strip()
    if not text:
        return {} if default is None else default
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {} if default is None else default


class KnowledgeIn(CamelModel):
    """创建 / 更新请求（对齐 Java `KnowledgeSaveDTO`）。

    只收用户可编辑字段 —— `user_id` / `document_count` 这些内部字段
    绝不能从请求体进来，否则前端传个 `documentCount: 999999` 就写进去了。
    """

    id: str | None = None
    name: str = Field(..., max_length=KNOWLEDGE_NAME_MAX)
    description: str | None = Field(None, max_length=KNOWLEDGE_DESC_MAX)
    embedding_model: str | None = None
    type: str = "pg"
    config: dict | str | None = None
    query_params: dict | str | None = None
    graph_enabled: bool | None = None
    # Dify 专用；type=dify 时必填 api_url / dataset_id / token
    dify_config: dict | None = None

    @field_validator("type")
    @classmethod
    def _check_type(cls, v: str) -> str:
        if v not in VALID_TYPES:
            raise ValueError(f"知识库类型只能是 {' / '.join(VALID_TYPES)}")
        return v


class KnowledgeOut(CamelModel):
    """知识库出参（对齐 Java `Knowledge` 实体的 JSON 形态）。"""

    id: str
    user_id: str
    name: str
    description: str | None = None
    embedding_model: str | None = None
    type: str
    # 这四个刻意是 str：见模块 docstring
    config: str
    query_params: str
    mindmap_data: str | None = None
    example_questions: str
    document_count: int = 0
    chunk_count: int = 0
    total_tokens: int = 0
    status: str = "active"
    graph_enabled: bool = False
    node_count: int = 0
    edge_count: int = 0
    create_time: datetime | None = None
    update_time: datetime | None = None
    # 当前用户在这个库里的角色。Java 没有这个字段（它靠单独的 /members 接口），
    # 但前端 KnowledgeDetail 需要判断"我能不能删"—— 加在这里省一次请求。
    # 刻意放最后且默认 None：Java 老前端不读它，不会受影响。
    my_role: str | None = None


class KnowledgeMemberOut(CamelModel):
    """成员列表项（对齐 Java `KnowledgeMemberVO`）。"""

    id: str
    knowledge_id: str
    user_id: str
    role: str
    username: str | None = None
    nickname: str | None = None
    avatar: str | None = None
    create_time: datetime | None = None


class IngestConfigOut(CamelModel):
    """默认入库配置（对齐 Java `IngestDTO`）。

    字段名与前端 `previewChunks` / `ingestDocument` 请求体一致：
    `chunkStrategy` / `chunkSize` / `chunkOverlap` / `chunkDelimiter`。
    """

    chunk_strategy: str = "general"
    chunk_size: int = 512
    chunk_overlap: int = 10
    chunk_delimiter: str = ""
    collapse_whitespace: bool = False
    text_clean_rules: list[dict] | None = None


class QueryParamsOut(CamelModel):
    """检索配置出参。

    **不能固定字段**：Java 返回的是 `Map<String, Object>`，PG / Milvus / Dify
    三种库的字段集合完全不同（Milvus 多 bm25_*、graph_*，Dify 少一半）。
    固定 schema 会把不认识字段静默丢掉，前端保存后再读就"配置丢了"。
    所以这里直接用 dict 透传，见 `knowledge_service.get_query_params`。
    """


class AdvisorSummaryOut(CamelModel):
    knowledge_id: str
    total_references: int = 0
    total_likes: int = 0
    total_dislikes: int = 0
    like_rate: float = 0.0
    referenced_chunk_count: int = 0
    sleeping_chunk_count: int = 0
    window_days: int = 14


class LowRatedChunkOut(CamelModel):
    """对齐 Java `LowRatedChunkVO`。"""

    chunk_id: str
    document_id: str | None = None
    document_name: str | None = None
    content_preview: str | None = None
    like_count: int = 0
    dislike_count: int = 0
    # dislikeCount / (likeCount + dislikeCount)，Java 侧在 service 里算好再塞 VO
    dislike_rate: float = 0.0
    reference_count: int = 0
    last_referenced_at: datetime | None = None


class SleepingChunkOut(CamelModel):
    """对齐 Java `SleepingChunkVO`。"""

    chunk_id: str
    document_id: str | None = None
    document_name: str | None = None
    content_preview: str | None = None
    chunk_create_time: datetime | None = None
    last_referenced_at: datetime | None = None
    # 从未被引用时 = 自创建以来的天数
    sleeping_days: int = 0
    reference_count: int = 0
