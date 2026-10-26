"""系统配置：默认模型配置（对话 / 向量 / TTS / 重排）。

对照 Java：`SystemConfigServiceImpl` 里 `getDefault*Config` / `updateDefault*Config`
这一组方法，存储是 `system_config` 表的 key-value。

**这里刻意偏离 Java 一处**：`providerId` 出参用**字符串**。
Java 的 `DefaultAiConfigDTO.providerId` 是 `Long`，Jackson 序列化成 JSON 数字，
而 providerId 是 19 位雪花 ID（约 2e18），**超过 JS 的 Number.MAX_SAFE_INTEGER
（9e15）**。前端 `JSON.parse` 之后末几位就被改写了，再拿去和
`with-models` 返回的 provider.id（字符串）比对时匹配不上 ——
表现是"设置页的模型下拉框明明是配置过的，打开却显示空"。

前端代码是 `String(cfg.providerId)`，所以给字符串完全兼容。
入参两种都接受（字符串/数字），因为前端提交时传的是 select 的 value（字符串）。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entities import SystemConfig

logger = logging.getLogger(__name__)

# 配置键。注意 `default_ai_provider` 是老键，`default_chat_model` 是新键，
# 读对话模型时新键优先、老键兜底（Java 同逻辑）。
KEY_AI_PROVIDER = "default_ai_provider"       # 旧：默认 AI 配置
KEY_CHAT_MODEL = "default_chat_model"
KEY_EMBEDDING_MODEL = "default_embedding_model"
KEY_TTS_MODEL = "default_tts_model"
KEY_RERANK_MODEL = "default_rerank_model"

# 每种模型配置的默认描述（键不存在时 upsert 用）
_DESCRIPTIONS = {
    KEY_CHAT_MODEL: "默认对话模型配置",
    KEY_EMBEDDING_MODEL: "默认向量模型配置（知识库文档嵌入等场景使用）",
    KEY_TTS_MODEL: "默认TTS模型配置（语音合成等场景使用）",
    KEY_RERANK_MODEL: "默认重排模型配置（知识库检索精排等场景使用）",
}


def _empty_config() -> dict[str, Any]:
    return {"providerId": None, "modelId": None}


def _parse_model_config(raw: str | None) -> dict[str, Any]:
    """JSON 文本 → 配置 dict。

    容错比 Java 更宽：Java 的 `parseModelConfig` 解析失败会返回空 DTO，
    这里同样。另外 `providerId` 可能是字符串也可能是数字，统一成**字符串**出去。
    """
    out = _empty_config()
    if not raw or not raw.strip():
        return out
    try:
        node = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("默认模型配置 JSON 解析失败: %s", raw[:120])
        return out
    if not isinstance(node, dict):
        return out

    pid = node.get("providerId")
    if pid is not None and str(pid).strip() != "":
        out["providerId"] = str(pid).strip()
    mid = node.get("modelId")
    if mid is not None and str(mid).strip() != "":
        out["modelId"] = str(mid).strip()
    return out


def _dump_model_config(payload: dict[str, Any]) -> str:
    """配置 dict → JSON 文本（落库用）。

    `providerId` 落库时存**字符串**：库里现在的数据是 `{"providerId": null}` 或
    数字，两种都能读；存字符串是为了和出参一致，避免"存进去是数字、读出来是字符串"。
    """
    pid = payload.get("providerId")
    mid = payload.get("modelId")
    data = {
        "providerId": (str(pid).strip() or None) if pid is not None else None,
        "modelId": (str(mid).strip() or None) if mid is not None else None,
    }
    return json.dumps(data, ensure_ascii=False)


class SystemConfigService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------

    async def get_raw(self, key: str) -> str | None:
        row = (
            await self.db.execute(
                select(SystemConfig).where(SystemConfig.config_key == key)
            )
        ).scalar_one_or_none()
        return row.config_value if row else None

    async def get_chat(self) -> dict[str, Any]:
        """默认对话模型：新键优先，老键兜底（Java 同）。

        为什么要兜底：这个配置改过名（`default_ai_provider` → `default_chat_model`），
        老库里只有老键。不回退的话老库升级后"默认对话模型"会变成空。
        """
        raw = await self.get_raw(KEY_CHAT_MODEL)
        if not raw or not raw.strip():
            raw = await self.get_raw(KEY_AI_PROVIDER)
        return _parse_model_config(raw)

    async def get_embedding(self) -> dict[str, Any]:
        return _parse_model_config(await self.get_raw(KEY_EMBEDDING_MODEL))

    async def get_tts(self) -> dict[str, Any]:
        return _parse_model_config(await self.get_raw(KEY_TTS_MODEL))

    async def get_rerank(self) -> dict[str, Any]:
        return _parse_model_config(await self.get_raw(KEY_RERANK_MODEL))

    async def set_raw(self, key: str, value: str, description: str | None = None) -> None:
        """通用写配置（Landing 等"整段 JSON 当字符串存"的场景用）。

        `_upsert` 是私有的，外部模块不该直接调私有方法；这里开一个语义清晰的口子。
        """
        await self._upsert(key, value, description)

    async def get_all(self) -> dict[str, Any]:
        """合并接口（`default-models`），设置页一次拿 4 个。"""
        return {
            "chat": await self.get_chat(),
            "embedding": await self.get_embedding(),
            "tts": await self.get_tts(),
            "rerank": await self.get_rerank(),
        }

    # ------------------------------------------------------------------
    # 写
    # ------------------------------------------------------------------

    async def _upsert(self, key: str, value: str, description: str | None = None) -> None:
        """写配置，不存在就建。

        **Java 那边配置项不存在时只 log warn 后返回，静默不保存** ——
        前端会看到"保存成功"但其实什么都没写。这里改成 upsert：
        用户点了保存就该保存，不该因为少一行初始化数据而无声失败。
        """
        row = (
            await self.db.execute(
                select(SystemConfig).where(SystemConfig.config_key == key)
            )
        ).scalar_one_or_none()

        now = datetime.now()
        if row is None:
            self.db.add(
                SystemConfig(
                    config_key=key,
                    config_value=value,
                    description=description or _DESCRIPTIONS.get(key),
                    create_time=now,
                    update_time=now,
                )
            )
            logger.info("新建系统配置 %s", key)
        else:
            row.config_value = value
            row.update_time = now
            logger.info("更新系统配置 %s", key)
        await self.db.commit()

    async def update_chat(self, payload: dict[str, Any]) -> None:
        await self._upsert(KEY_CHAT_MODEL, _dump_model_config(payload))

    async def update_ai(self, payload: dict[str, Any]) -> None:
        """兼容旧接口：等同于更新默认对话模型。"""
        await self.update_chat(payload)

    async def update_embedding(self, payload: dict[str, Any]) -> None:
        await self._upsert(KEY_EMBEDDING_MODEL, _dump_model_config(payload))

    async def update_tts(self, payload: dict[str, Any]) -> None:
        await self._upsert(KEY_TTS_MODEL, _dump_model_config(payload))

    async def update_rerank(self, payload: dict[str, Any]) -> None:
        await self._upsert(KEY_RERANK_MODEL, _dump_model_config(payload))
