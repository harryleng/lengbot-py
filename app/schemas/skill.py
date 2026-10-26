"""Skill 管理的 schema。

对照 Java：`SkillRequestDTO` / `Skill` / `SkillFileTreeNodeDTO` /
`SkillFileWriteDTO` / `SkillImportPreviewDTO` / `RemoteListDTO` / `RemotePrepareDTO`。

## 出参口径（照抄 Java 的"不一致"，因为前端是按这个写的）

- **id / agentId / modelId 出字符串**。19 位雪花 ID 用 JSON number 传，
  JS 的 `Number` 只有 53 位精度，最后 3 位会变成 0 —— 前端再拿这个 id 去删/改
  就打到不存在的记录上。前端 `request.js` 里那段 `convertLongToString` 是
  兜底，但**后端不该依赖兜底**。
- **`isDir` 不是 `dir`**。Java Lombok `private boolean isDir` → Jackson 出 `isDir`；
  前端 `node.isDir` 直接读。写成 `dir` 文件树会全变成叶子节点，点不开目录。
- **`toolIds` / `mcpServerIds` / `skillDependencies` 出数组**。前端 `parseIdArray`
  数组和 JSON 字符串都能吃，给数组更省事。
- **`config` 出 JSON 文本字符串**。前端 `row.config || '{}'` 然后原样回传，
  给它对象会被当字符串拼进 JSON body。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from pydantic import Field, field_validator

from app.schemas.common import CamelModel


def _to_json_text(value: Any, default: str = "{}") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return default


def _to_id_list(value: Any) -> list[str]:
    """JSONB 数组 / JSON 字符串 / None 一律归一成字符串数组。

    DB 里是 JSONB，但历史数据可能是 `"[]"` 这种字符串，
    前端 `(form.toolIds || []).filter(...)` 遇到非数组直接崩。
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if v is not None]
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return [text]
        return _to_id_list(parsed)
    return [str(value)]


class SkillIn(CamelModel):
    """创建 / 更新请求体（对齐 Java `SkillRequestDTO`）。

    Java 侧 `@NotBlank name` + 一堆 `@Size`。Python 侧只在服务层校验
    **会真正咬人的**（name 空、slug 格式），长度限制不重复实现 ——
    DB 列本身有长度上限，超了会报数据库错误，够了。
    """

    id: str | None = None
    slug: str | None = None
    agent_id: str | None = None
    tool_ids: list[str] | None = None
    mcp_server_ids: list[str] | None = None
    model_id: str | None = None
    name: str | None = None
    display_name: str | None = None
    icon: str | None = None
    description: str | None = None
    prompt_template: str | None = None
    config: str | None = None
    sort_order: int | None = None
    scope: str | None = None
    version: str | None = None
    skill_dependencies: list[str] | None = None
    source_type: str | None = None

    @field_validator("id", "agent_id", "model_id", mode="before")
    @classmethod
    def _id_to_str(cls, v: Any) -> str | None:
        return None if v is None else str(v)

    @field_validator("tool_ids", "mcp_server_ids", "skill_dependencies", mode="before")
    @classmethod
    def _ids(cls, v: Any) -> list[str] | None:
        return None if v is None else _to_id_list(v)


class SkillOut(CamelModel):
    """Skill 响应体。字段全量对齐 Java 的 `Skill` 实体。"""

    id: str
    slug: str | None = None
    agent_id: str | None = None
    tool_ids: list[str] = Field(default_factory=list)
    mcp_server_ids: list[str] = Field(default_factory=list)
    model_id: str | None = None
    name: str | None = None
    display_name: str | None = None
    icon: str | None = None
    description: str | None = None
    prompt_template: str | None = None
    config: str = "{}"
    sort_order: int = 0
    status: str | None = None
    scope: str | None = None
    is_builtin: int = 0
    content_hash: str | None = None
    object_prefix: str | None = None
    version: str | None = None
    skill_dependencies: list[str] = Field(default_factory=list)
    source_type: str | None = None
    user_id: str | None = None
    create_time: datetime | None = None
    update_time: datetime | None = None

    @field_validator("id", "agent_id", "model_id", "user_id", mode="before")
    @classmethod
    def _id_to_str(cls, v: Any) -> str | None:
        return None if v is None else str(v)

    @field_validator("tool_ids", "mcp_server_ids", "skill_dependencies", mode="before")
    @classmethod
    def _ids(cls, v: Any) -> list[str]:
        return _to_id_list(v)

    @field_validator("config", mode="before")
    @classmethod
    def _config(cls, v: Any) -> str:
        return _to_json_text(v)


class SkillFileNode(CamelModel):
    """文件树节点（对齐 Java `SkillFileTreeNodeDTO`）。

    `is_dir` 会被 camel 化输出成 `isDir`，正好是前端要的 key。
    """

    name: str
    path: str
    is_dir: bool = False
    children: list["SkillFileNode"] | None = None


class SkillFileWriteIn(CamelModel):
    """写文件请求体（对齐 Java `SkillFileWriteDTO`）。"""

    path: str
    content: str | None = None
    dir: bool | None = None


class SkillImportPreview(CamelModel):
    """ZIP / 远程导入预览（对齐 Java `SkillImportPreviewDTO`）。"""

    draft_id: str | None = None
    slug: str | None = None
    name: str | None = None
    description: str | None = None
    version: str | None = None
    tool_dependencies: list[str] = Field(default_factory=list)
    skill_dependencies: list[str] = Field(default_factory=list)
    file_names: list[str] = Field(default_factory=list)

    @field_validator("tool_dependencies", "skill_dependencies", "file_names", mode="before")
    @classmethod
    def _ids(cls, v: Any) -> list[str]:
        return _to_id_list(v)


class RemoteListIn(CamelModel):
    """远程列表 / 搜索请求体（对齐 Java `RemoteListDTO`）。"""

    source: str


class RemotePrepareIn(RemoteListIn):
    """远程安装准备请求体（对齐 Java `RemotePrepareDTO`）。"""

    skills: list[str] = Field(default_factory=list)
