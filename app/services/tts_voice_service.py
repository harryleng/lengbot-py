"""TTS 音色缓存管理（对照 Java `TtsVoiceManageService`）。

核心规则只有一条：**Provider 同步不能覆盖用户自定义字段**。
同步是 upsert，只刷新 friendly_name / locale / gender 三个 Provider 派生字段，
favorite / voice_group / remark 是用户自己点的收藏、自己填的备注，重同步不能清空。

Python 侧没有接真实的 edge-tts（Java 走 edge-tts 的 voices 列表接口），
这里用一份**内置的 edge-tts 常见音色清单**当 Provider 返回，
保证 `sync` 能真写入数据、音色下拉不是空的。
等真接入 edge-tts 时，只要把 `list_provider_voices` 换成真实调用即可，
上层 upsert 逻辑一行都不用改。
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.snowflake import new_id
from app.models.entities import TtsVoice
from app.schemas.tts_voice import TtsVoiceOut

logger = logging.getLogger(__name__)

DEFAULT_PROVIDER = "edge-tts"

# edge-tts 真实 ShortName 的子集（中文 + 英文常用），够下拉用
_BUILTIN_VOICES: list[dict[str, str]] = [
    {"name": "zh-CN-XiaoxiaoNeural", "friendlyName": "晓晓", "locale": "zh-CN", "gender": "Female"},
    {"name": "zh-CN-YunxiNeural", "friendlyName": "云希", "locale": "zh-CN", "gender": "Male"},
    {"name": "zh-CN-YunyeNeural", "friendlyName": "云野", "locale": "zh-CN", "gender": "Male"},
    {"name": "zh-CN-XiaoyiNeural", "friendlyName": "晓伊", "locale": "zh-CN", "gender": "Female"},
    {"name": "zh-CN-YunjianNeural", "friendlyName": "云健", "locale": "zh-CN", "gender": "Male"},
    {"name": "zh-CN-XiaoxuanNeural", "friendlyName": "晓萱", "locale": "zh-CN", "gender": "Female"},
    {
        "name": "zh-CN-liaoning-XiaobeiNeural",
        "friendlyName": "晓北(辽宁)",
        "locale": "zh-CN",
        "gender": "Female",
    },
    {"name": "zh-HK-HiuGaaiNeural", "friendlyName": "曉佳", "locale": "zh-HK", "gender": "Female"},
    {"name": "zh-TW-HsiaoChenNeural", "friendlyName": "曉臻", "locale": "zh-TW", "gender": "Female"},
    {"name": "en-US-AriaNeural", "friendlyName": "Aria", "locale": "en-US", "gender": "Female"},
    {"name": "en-US-GuyNeural", "friendlyName": "Guy", "locale": "en-US", "gender": "Male"},
    {"name": "en-GB-SoniaNeural", "friendlyName": "Sonia", "locale": "en-GB", "gender": "Female"},
    {"name": "ja-JP-NanamiNeural", "friendlyName": "七海", "locale": "ja-JP", "gender": "Female"},
]


def list_provider_voices(provider: str | None) -> list[dict[str, str]]:
    """从 Provider 拉音色清单。目前只有内置 edge-tts 清单。"""
    if (provider or DEFAULT_PROVIDER) != DEFAULT_PROVIDER:
        return []
    return [dict(v) for v in _BUILTIN_VOICES]


class TtsVoiceService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def list(
        self,
        provider: str | None,
        locale: str | None,
        gender: str | None,
        favorite: bool | None,
        group: str | None,
        keyword: str | None,
    ) -> list[TtsVoiceOut]:
        stmt = select(TtsVoice).where(or_(TtsVoice.deleted == 0, TtsVoice.deleted.is_(None)))
        if provider:
            stmt = stmt.where(TtsVoice.provider == provider)
        if locale:
            stmt = stmt.where(TtsVoice.locale == locale)
        if gender:
            stmt = stmt.where(TtsVoice.gender == gender)
        if favorite:
            # Java：favorite 只有 true 才生效，传 false 表示"不筛选"
            stmt = stmt.where(TtsVoice.favorite == 1)
        if group:
            stmt = stmt.where(TtsVoice.voice_group == group)
        if keyword and keyword.strip():
            kw = f"%{keyword.strip()}%"
            stmt = stmt.where(
                (TtsVoice.voice_name.ilike(kw)) | (TtsVoice.friendly_name.ilike(kw))
            )
        stmt = stmt.order_by(TtsVoice.favorite.desc(), TtsVoice.sort_order, TtsVoice.id)

        rows = (await self.db.execute(stmt)).scalars().all()
        if not rows:
            # 空库兜底：Java 会懒同步一次再查；同步不到就直接用 Provider 实时列表顶上
            synced = await self.sync(provider)
            if synced > 0:
                rows = (await self.db.execute(stmt)).scalars().all()
            else:
                return [
                    TtsVoiceOut(
                        voice_uri=v["name"],
                        name=v["name"],
                        friendly_name=v["friendlyName"],
                        locale=v["locale"],
                        gender=v["gender"],
                        provider=DEFAULT_PROVIDER,
                        favorite=False,
                        voice_group="",
                        remark="",
                        sort_order=0,
                    )
                    for v in list_provider_voices(provider)
                ]
        return [_to_out(r) for r in rows]

    async def sync(self, provider: str | None) -> int:
        live = list_provider_voices(provider)
        if not live:
            return 0
        prov = provider or DEFAULT_PROVIDER
        count = 0
        now = datetime.now()
        for v in live:
            stmt = (
                select(TtsVoice)
                .where(TtsVoice.provider == prov)
                .where(TtsVoice.voice_name == v["name"])
                .where(or_(TtsVoice.deleted == 0, TtsVoice.deleted.is_(None)))
            )
            exist = (await self.db.execute(stmt)).scalars().first()
            if exist is None:
                self.db.add(
                    TtsVoice(
                        id=new_id(),
                        voice_name=v["name"],
                        provider=prov,
                        friendly_name=v["friendlyName"],
                        locale=v["locale"],
                        gender=v["gender"],
                        favorite=0,
                        voice_group="",
                        remark="",
                        sort_order=0,
                        extra_json={},
                        create_time=now,
                        update_time=now,
                        deleted=0,
                    )
                )
                count += 1
                continue
            changed = False
            if exist.friendly_name != v["friendlyName"]:
                exist.friendly_name = v["friendlyName"]
                changed = True
            if exist.locale != v["locale"]:
                exist.locale = v["locale"]
                changed = True
            if exist.gender != v["gender"]:
                exist.gender = v["gender"]
                changed = True
            if changed:
                exist.update_time = now
                count += 1
        await self.db.commit()
        logger.info("[TtsVoice] 同步完成：provider=%s, 新增/变更=%d", prov, count)
        return count

    async def update_meta(
        self, voice_name: str, provider: str | None, body: dict | None
    ) -> None:
        stmt = select(TtsVoice).where(TtsVoice.voice_name == voice_name)
        if provider:
            stmt = stmt.where(TtsVoice.provider == provider)
        stmt = stmt.where(or_(TtsVoice.deleted == 0, TtsVoice.deleted.is_(None)))
        row = (await self.db.execute(stmt)).scalars().first()
        if row is None:
            raise LookupError(f"音色不存在: {voice_name}")
        if not body:
            return
        if "favorite" in body:
            row.favorite = 1 if _as_bool(body["favorite"]) else 0
        if "voiceGroup" in body:
            row.voice_group = (body["voiceGroup"] or "").strip() if body["voiceGroup"] else ""
        if "remark" in body:
            row.remark = str(body["remark"]) if body["remark"] is not None else ""
        row.update_time = datetime.now()
        await self.db.commit()

    async def list_groups(self) -> list[str]:
        stmt = (
            select(TtsVoice.voice_group)
            .where(or_(TtsVoice.deleted == 0, TtsVoice.deleted.is_(None)))
            .where(TtsVoice.voice_group.is_not(None))
            .where(TtsVoice.voice_group != "")
            .distinct()
        )
        rows = (await self.db.execute(stmt)).scalars().all()
        return sorted({g for g in rows if g})


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _to_out(row: TtsVoice) -> TtsVoiceOut:
    return TtsVoiceOut(
        voice_uri=row.voice_name,
        name=row.voice_name,
        friendly_name=row.friendly_name,
        locale=row.locale,
        gender=row.gender,
        provider=row.provider,
        favorite=row.favorite == 1,
        voice_group=row.voice_group or "",
        remark=row.remark or "",
        sort_order=row.sort_order,
    )
