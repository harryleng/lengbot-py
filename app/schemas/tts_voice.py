"""TTS 音色管理的出参/入参。

对照 Java `TtsVoiceView`：最反直觉的一点是 **`voiceURI` 和 `name` 内容完全相同**
（都等于 voice_name），前者纯粹为兼容早期前端下拉而存在。
不要"顺手去重"只留一个，前端两处都在读。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from app.schemas.common import CamelModel


class TtsVoiceOut(CamelModel):
    # `to_camel("voice_uri")` 会得到 `voiceUri`，而 Java 的字段名是全大写的 `voiceURI`。
    # 前端下拉读的就是 `voiceURI`，这里必须显式指定别名，否则前端拿到 undefined。
    voice_uri: str | None = Field(
        default=None, alias="voiceURI", serialization_alias="voiceURI"
    )
    name: str | None = None
    friendly_name: str | None = None
    locale: str | None = None
    gender: str | None = None
    provider: str | None = None
    favorite: bool | None = None
    voice_group: str | None = None
    remark: str | None = None
    sort_order: int | None = None


class TtsVoiceMetaIn(BaseModel):
    """`PATCH /tts/voices/{name}`：只传要改的字段，不传的不动。

    Java 收的是裸 Map，键是 camelCase（`voiceGroup`）；
    这里两种写法都收，避免前端改一下命名就静默失效。
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    favorite: bool | None = None
    voice_group: str | None = None
    remark: str | None = None


class TtsSyncOut(CamelModel):
    synced: int = 0
