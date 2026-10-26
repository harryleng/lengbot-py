"""TTS 音色管理（对照 Java `TtsController` 的 `/voices*` 四个接口）。

路由顺序有讲究：`/groups` 和 `/sync` 必须**先于** `/{voice_name}` 注册，
否则 `GET /voices/groups` 会被当成 `voice_name="groups"` 命中 PATCH 之外的路径、
返回 405 而不是分组列表。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.api.deps import DbSession
from app.schemas.common import Result
from app.schemas.tts_voice import TtsSyncOut, TtsVoiceMetaIn, TtsVoiceOut
from app.services.tts_voice_service import TtsVoiceService

router = APIRouter(prefix="/api/tts/voices", tags=["tts"])


@router.get("/groups", response_model=Result[list[str]], summary="音色分组列表")
async def groups(db: DbSession) -> Result[list[str]]:
    return Result.ok(await TtsVoiceService(db).list_groups())


@router.post("/sync", response_model=Result[TtsSyncOut], summary="从 Provider 同步音色")
async def sync(
    db: DbSession, provider: str | None = Query(default=None)
) -> Result[TtsSyncOut]:
    synced = await TtsVoiceService(db).sync(provider)
    return Result.ok(TtsSyncOut(synced=synced))


@router.get("", response_model=Result[list[TtsVoiceOut]], summary="音色列表（带筛选）")
async def list_voices(
    db: DbSession,
    provider: str | None = Query(default=None),
    locale: str | None = Query(default=None),
    gender: str | None = Query(default=None),
    favorite: bool | None = Query(default=None),
    group: str | None = Query(default=None),
    keyword: str | None = Query(default=None),
) -> Result[list[TtsVoiceOut]]:
    return Result.ok(
        await TtsVoiceService(db).list(provider, locale, gender, favorite, group, keyword)
    )


@router.patch("/{voice_name}", response_model=Result[None], summary="更新音色元数据")
async def update_meta(
    db: DbSession,
    voice_name: str,
    body: TtsVoiceMetaIn | None = None,
    provider: str | None = Query(default=None),
) -> Result[None]:
    if not voice_name or not voice_name.strip():
        raise HTTPException(status_code=400, detail="音色名不能为空")
    # Java 的 body 是裸 Map，键是 camelCase 的 `voiceGroup`；
    # service 层照 Java 语义检查 camel 键，这里显式转换一次。
    payload: dict = {}
    if body is not None:
        if body.favorite is not None:
            payload["favorite"] = body.favorite
        if body.voice_group is not None:
            payload["voiceGroup"] = body.voice_group
        if body.remark is not None:
            payload["remark"] = body.remark
    try:
        await TtsVoiceService(db).update_meta(voice_name, provider, payload)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return Result.ok(None)
