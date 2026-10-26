"""枚举字典接口（对照 Java `EnumController`）。

出参固定 `{value, label}`：`value` 是落库的 code，`label` 是中文 desc。
前端下拉框直接拿这两个字段渲染，不要合并成别的形状。

⚠️ 取值必须和 Java 枚举**逐字一致**，尤其 `ModelProviderType` 的 code 是**大写**
（OPENAI / DASHSCOPE …），而 `ToolType` / `ModelType` 是小写。
这不是风格不统一，是历史包袱：前者对应老配置的枚举名，改了会读不到已有配置。
"""

from __future__ import annotations

from fastapi import APIRouter

from app.schemas.common import Result
from app.schemas.misc import EnumVO

router = APIRouter(prefix="/api/enums", tags=["enums"])

TOOL_TYPES = [
    ("builtin", "内置"),
    ("api", "API调用"),
    ("knowledge", "知识库"),
]

MODEL_PROVIDER_TYPES = [
    ("OPENAI", "OpenAI"),
    ("DASHSCOPE", "通义千问"),
    ("DEEPSEEK", "DeepSeek"),
    ("OLLAMA", "Ollama"),
    ("MIMO", "小米MiMo"),
]

AGENT_STATUSES = [
    ("draft", "草稿"),
    ("published", "已发布"),
    ("published_editing", "编辑中"),
    ("archived", "已归档"),
]

MODEL_TYPES = [
    ("llm", "对话模型"),
    ("embedding", "嵌入模型"),
    ("rerank", "重排模型"),
    ("tts", "语音合成"),
    ("stt", "语音识别"),
]


def _vo(pairs: list[tuple[str, str]]) -> list[EnumVO]:
    return [EnumVO(value=code, label=desc) for code, desc in pairs]


@router.get("/tool-types", response_model=Result[list[EnumVO]], summary="工具类型枚举")
async def tool_types() -> Result[list[EnumVO]]:
    return Result.ok(_vo(TOOL_TYPES))


@router.get(
    "/model-provider-types",
    response_model=Result[list[EnumVO]],
    summary="模型提供商类型枚举",
)
async def model_provider_types() -> Result[list[EnumVO]]:
    return Result.ok(_vo(MODEL_PROVIDER_TYPES))


@router.get("/agent-statuses", response_model=Result[list[EnumVO]], summary="Agent 状态枚举")
async def agent_statuses() -> Result[list[EnumVO]]:
    return Result.ok(_vo(AGENT_STATUSES))


@router.get("/model-types", response_model=Result[list[EnumVO]], summary="模型类型枚举")
async def model_types() -> Result[list[EnumVO]]:
    return Result.ok(_vo(MODEL_TYPES))
