"""文档管理 schema（对齐前端 `knowledge.js` 的文档相关接口）。

## `status` 是裸字符串不是对象

Java 的 `DocumentStatus` 枚举带 `@JsonValue getCode()`，序列化出来是
`"completed"` 这样的**裸字符串**。但 Java 的 `ChunkStatus` 是
`@EnumValue @JsonValue private final String code` —— 也是字符串。

前端却写了双分支兜底：

    const s = d.status?.code || d.status

说明历史上有过 `{code, desc}` 的形态。这里统一输出裸字符串（对齐当前 Java），
前端两个分支都能吃。

## `embeddingJson` / `metadata` 是 jsonb

`Document` 实体在 Java 里用 `JsonbTypeHandler` 映射成 String 字段，
所以出参是**字符串**。但前端 `KnowledgeDetail.vue` 里：

    <div v-if="currentDoc?.embeddingJson" class="ingest-config-display">
      <span>{{ parseIngestConfig(currentDoc.embeddingJson).chunkStrategy }}</span>

`parseIngestConfig` 内部就是 `typeof json === 'string' ? JSON.parse(json) : json`。
两种都能吃，但为了和 Java 100% 一致，这里输出字符串。

**入参是对象**：`ingestDocument(docId, data)` 里 data 是
`{chunkStrategy, chunkSize, chunkOverlap, chunkDelimiter}` 对象，不是字符串。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from pydantic import Field, field_validator

from app.schemas.common import CamelModel


class DocumentOut(CamelModel):
    id: str
    knowledge_id: str
    user_id: str
    name: str
    file_type: str | None = None
    file_size: int | None = None
    file_path: str | None = None
    markdown_path: str | None = None
    chunk_count: int = 0
    token_count: int = 0
    status: str = "uploaded"
    error_message: str | None = None
    duplicate_rate: float | None = None
    # jsonb → JSON 字符串（见模块 docstring）
    embedding_json: Any = None
    metadata: Any = None
    version: int = 1
    last_edit_time: datetime | None = None
    create_time: datetime | None = None
    update_time: datetime | None = None

    @field_validator("embedding_json", "metadata", mode="before")
    @classmethod
    def _jsonb_to_text(cls, v: Any) -> Any:
        if v is None or isinstance(v, str):
            return v
        try:
            return json.dumps(v, ensure_ascii=False)
        except (TypeError, ValueError):
            return None


class ChunkOut(CamelModel):
    id: str
    document_id: str
    content: str
    chunk_index: int
    token_count: int = 0
    status: str = "chunked"
    metadata: Any = None
    create_time: datetime | None = None

    @field_validator("metadata", mode="before")
    @classmethod
    def _meta_to_text(cls, v: Any) -> Any:
        if v is None or isinstance(v, str):
            return v
        try:
            return json.dumps(v, ensure_ascii=False)
        except (TypeError, ValueError):
            return None


class IngestIn(CamelModel):
    """入库 / 预览分块参数（对齐 Java `IngestDTO` 的校验规则）。

    校验边界必须和 Java 一致，否则前端表单能提交但后端 400：
    - `chunkSize` 100~2000
    - `chunkOverlap` 0~99
    """

    chunk_strategy: str = "general"
    chunk_size: int = Field(512, ge=100, le=2000)
    chunk_overlap: int = Field(10, ge=0, le=99)
    chunk_delimiter: str | None = None
    collapse_whitespace: bool | None = None
    text_clean_rules: list[dict] | None = None
    ocr_enabled: bool | None = None

    @field_validator("chunk_strategy")
    @classmethod
    def _check_strategy(cls, v: str) -> str:
        allowed = {"general", "separator", "parent-child", "qa", "book", "laws"}
        if v not in allowed:
            raise ValueError(f"未知分块策略 {v}，可选：{' / '.join(sorted(allowed))}")
        return v


class DownloadInfoOut(CamelModel):
    """对齐 Java `DocumentDownloadVO`。"""

    url: str
    file_type: str
    file_name: str
    content_type: str


class UrlPreviewOut(CamelModel):
    """对齐 Java `UrlFetchPreviewVO`。"""

    url: str
    title: str | None = None
    content: str
    preview_html: str | None = None
    suggested_file_name: str | None = None
    content_length: int = 0
    description: str | None = None


class UrlSaveIn(CamelModel):
    """保存已预览的网页（对齐 Java `UrlSaveDTO`）。

    `syncConfig` 是对象（含 `headers` / `syncInterval`），
    落到 `document.metadata` 里供后续 `sync-url` 用。
    """

    url: str
    title: str | None = None
    content: str
    sync_config: dict | None = None
