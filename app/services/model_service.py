"""模型管理 4 接口（对照 Java `ModelController` / `ModelServiceImpl`）。

`model` 表是「模型管理」页手工维护的（或 fetch-models 拉取后勾选），
`type` 是人工确认的，比靠名字猜的准 —— 检索选 embedding 模型就看这列。

Java 的 create/delete 后会增量同步模型缓存（Redis）；Python 侧模型配置
由 `_resolve_provider` 每次直查（进程内缓存按 providerId），模型行变更后
**缓存不会自动失效** —— 这在"删了模型还能用"的场景下是可接受延迟，
真要做失效得连 workflow.nodes._provider_cache 一起清。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.snowflake import new_id
from app.schemas.model import ModelCreateIn  # noqa: F401  # 复用入口
from app.models.entities import ModelRecord
def model_out(row: ModelRecord) -> dict:
    return {
        "id": str(row.id),
        "providerId": str(row.provider_id) if row.provider_id else None,
        "modelId": row.model_id,
        "name": row.name,
        "type": row.type,
        "dimension": row.dimension,
        "status": row.status,
        "createTime": row.create_time,
        "updateTime": row.update_time,
    }


class ModelService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def list_by_provider(self, provider_id: int) -> list[dict]:
        stmt = (
            select(ModelRecord)
            .where(ModelRecord.provider_id == provider_id)
            .where(ModelRecord.deleted == 0)
            .order_by(ModelRecord.type, ModelRecord.model_id)
        )
        rows = (await self.db.execute(stmt)).scalars().all()
        return [model_out(r) for r in rows]

    async def list_by_type(self, type_name: str) -> list[dict]:
        stmt = (
            select(ModelRecord)
            .where(ModelRecord.type == type_name)
            .where(ModelRecord.status == "active")
            .where(ModelRecord.deleted == 0)
            .order_by(ModelRecord.provider_id, ModelRecord.model_id)
        )
        rows = (await self.db.execute(stmt)).scalars().all()
        return [model_out(r) for r in rows]

    async def create(
        self, provider_id: int, model_id: str, name: str | None,
        type_name: str | None, dimension: int | None,
    ) -> dict:
        # 同一 provider 下 modelId 唯一（Java MODEL_ALREADY_EXISTS）
        stmt = (
            select(func.count())
            .select_from(ModelRecord)
            .where(ModelRecord.provider_id == provider_id)
            .where(ModelRecord.model_id == model_id)
            .where(ModelRecord.deleted == 0)
        )
        if int((await self.db.execute(stmt)).scalar() or 0) > 0:
            raise ValueError("该模型标识已存在于当前供应商下")

        now = datetime.now()
        row = ModelRecord(
            id=new_id(),
            provider_id=provider_id,
            model_id=model_id,
            name=name,
            type=type_name,
            dimension=dimension,
            status="active",
            create_time=now,
            update_time=now,
            deleted=0,
        )
        self.db.add(row)
        await self.db.commit()
        await self.db.refresh(row)
        return model_out(row)

    async def delete(self, model_row_id: int) -> None:
        row = await self.db.get(ModelRecord, model_row_id)
        if row is None or row.deleted:
            raise LookupError("模型不存在")
        row.deleted = 1
        row.update_time = datetime.now()
        await self.db.commit()
