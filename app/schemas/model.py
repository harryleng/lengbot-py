"""模型管理出入参（对照 Java `ModelRequestDTO`）。"""

from __future__ import annotations

from app.schemas.common import CamelModel


class ModelCreateIn(CamelModel):
    provider_id: str
    model_id: str
    name: str | None = None
    type: str | None = None
    dimension: int | None = None
