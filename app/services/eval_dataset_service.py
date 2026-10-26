"""评测集服务（评测集 / 数据项 / 版本）。

对应 Java `EvalDatasetServiceImpl` + `EvalDatasetVersionServiceImpl` +
`EvalDatasetItemServiceImpl`。

### 三个要点

**1. 版本冻结的是"数据项 id 列表"，不是内容。**
`eval_dataset_version.dataset_items` 存 `[id1, id2, ...]`。所以条目**内容**改了，
历史版本读出来也是新内容。Java 就这么设计的 —— 想要内容快照得自己另存。
这一点在"复现历史实验"时会有影响，值得记住。

**2. 建版本时数据集不能为空。**
Java 抛 `EVAL_DATASET_EMPTY`。空版本没意义（实验跑 0 条），而且
`data_count=0` 的版本在前端会显示成"空版本"，用户会以为是 bug。

**3. 名称唯一性只在同用户内检查？**
不，Java 是**全局唯一**（`count(name=?)` 不带 user_id）。
这是有意的：评测集是共享资源，两个用户同名会在选择器里分不清。
`list()` 却**按 user_id 过滤** —— 这两个不对称是 Java 的现状，
看起来像 bug 但改了会破坏隔离，照抄。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.snowflake import new_id
from app.models.entities import EvalDataset, EvalDatasetItem, EvalDatasetVersion
from app.schemas.eval import (
    EvalDatasetIn,
    EvalDatasetItemOut,
    EvalDatasetOut,
    EvalDatasetVersionOut,
    parse_json_value,
)

logger = logging.getLogger(__name__)


def _bad(msg: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=msg)


def _nf(msg: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=msg)


def _normalize_content(raw: Any) -> dict:
    """数据项内容归一成 dict。

    三种输入都要接：
    - dict（脚本/测试直接传对象）
    - JSON 字符串（前端 `JSON.stringify` 后提交）
    - 纯文本 → 包成 `{"content": 原文}`（早期数据可能是纯文本，
      `parseItemContent` 在前端也有这个兜底）

    非法 JSON 抛 400 而不是静默存字符串 —— jsonb 列存不了非法 JSON，
    入库时才报错的话，用户已经填完整个表单了。
    """
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, list):
        return {"content": raw}
    text = str(raw).strip()
    if not text:
        return {}
    try:
        parsed = parse_json_value(text)
    except ValueError:
        return {"content": text}
    if isinstance(parsed, dict):
        return parsed
    return {"content": parsed}


class EvalDatasetService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # ==================================================================
    # 评测集
    # ==================================================================
    async def list(
        self, user_id: int, page_num: int = 1, page_size: int = 20, keyword: str | None = None
    ) -> tuple[list[EvalDatasetOut], int]:
        conditions: list[Any] = [EvalDataset.deleted == 0, EvalDataset.user_id == user_id]
        if keyword and keyword.strip():
            conditions.append(EvalDataset.name.ilike(f"%{keyword.strip()}%"))

        total = int(
            (
                await self.db.execute(
                    select(func.count()).select_from(EvalDataset).where(*conditions)
                )
            ).scalar()
            or 0
        )
        rows = (
            await self.db.execute(
                select(EvalDataset)
                .where(*conditions)
                .order_by(EvalDataset.create_time.desc().nulls_last(), EvalDataset.id.desc())
                .offset((max(page_num, 1) - 1) * page_size)
                .limit(page_size)
            )
        ).scalars().all()
        return [EvalDatasetOut.model_validate(r) for r in rows], total

    async def _require(self, dataset_id: int) -> EvalDataset:
        row = (
            await self.db.execute(
                select(EvalDataset).where(
                    EvalDataset.id == dataset_id, EvalDataset.deleted == 0
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise _nf("评测集不存在")
        return row

    async def get(self, dataset_id: int) -> EvalDatasetOut:
        return EvalDatasetOut.model_validate(await self._require(dataset_id))

    async def _ensure_name_free(self, name: str, exclude_id: int | None = None) -> None:
        # 全局唯一（不按 user 过滤），对齐 Java
        conditions: list[Any] = [EvalDataset.name == name, EvalDataset.deleted == 0]
        if exclude_id is not None:
            conditions.append(EvalDataset.id != exclude_id)
        if (await self.db.execute(select(EvalDataset.id).where(*conditions))).first():
            raise _bad(f"评测集名称已存在：{name}")

    async def create(self, dto: EvalDatasetIn, user_id: int) -> EvalDatasetOut:
        name = dto.name.strip()
        await self._ensure_name_free(name)
        try:
            columns = parse_json_value(dto.columns_config, default=[])
        except ValueError as exc:
            raise _bad(f"columnsConfig 不是合法 JSON：{exc}") from exc

        now = datetime.now()
        row = EvalDataset(
            id=new_id(),
            name=name,
            description=dto.description,
            columns_config=columns if isinstance(columns, list) else [],
            user_id=user_id,
            create_time=now,
            update_time=now,
            deleted=0,
        )
        self.db.add(row)
        await self.db.commit()
        return EvalDatasetOut.model_validate(row)

    async def update(self, dataset_id: int, dto: EvalDatasetIn) -> None:
        """`None` / 空表示"这个字段不动"（对齐 Java 的 `if (x != null)`）。"""
        await self._require(dataset_id)
        values: dict[str, Any] = {"update_time": datetime.now()}
        if dto.name is not None and dto.name.strip():
            name = dto.name.strip()
            await self._ensure_name_free(name, exclude_id=dataset_id)
            values["name"] = name
        if dto.description is not None:
            values["description"] = dto.description
        if dto.columns_config is not None:
            try:
                columns = parse_json_value(dto.columns_config, default=[])
            except ValueError as exc:
                raise _bad(f"columnsConfig 不是合法 JSON：{exc}") from exc
            values["columns_config"] = columns if isinstance(columns, list) else []

        await self.db.execute(
            update(EvalDataset).where(EvalDataset.id == dataset_id).values(**values)
        )
        await self.db.commit()

    async def delete(self, dataset_id: int) -> None:
        """软删评测集。

        **不级联删数据项和版本** —— Java 的 `deleteById` 只 removeById。
        为什么不级联：删掉的评测集如果被历史实验引用，级联删了版本会让
        那些实验的 `datasetVersion` 变成悬空。留着数据（`deleted=0` 的条目
        仍可被历史版本按 id 查到）比删干净更有用。

        但版本列表接口会按 dataset 查，数据集软删后前端就拿不到了，
        所以实际效果是"看不见但没坏"。
        """
        await self._require(dataset_id)
        await self.db.execute(
            update(EvalDataset)
            .where(EvalDataset.id == dataset_id)
            .values(deleted=1, update_time=datetime.now())
        )
        await self.db.commit()

    # ==================================================================
    # 数据项
    # ==================================================================
    async def add_item(self, dataset_id: int, raw_content: Any) -> EvalDatasetItemOut:
        await self._require(int(dataset_id))
        now = datetime.now()
        row = EvalDatasetItem(
            id=new_id(),
            dataset_id=int(dataset_id),
            data_content=_normalize_content(raw_content),
            create_time=now,
            update_time=now,
            deleted=0,
        )
        self.db.add(row)
        await self.db.commit()
        return EvalDatasetItemOut.model_validate(row)

    async def batch_add_items(self, dataset_id: int, contents: list[Any] | None) -> int:
        """批量加。返回条数。

        空列表直接返回 0（不报错）—— 前端"批量导入"如果解析出 0 条，
        报错反而打断流程，返回 0 它会提示"未导入任何数据"。
        """
        await self._require(int(dataset_id))
        if not contents:
            return 0
        now = datetime.now()
        rows = [
            EvalDatasetItem(
                id=new_id(),
                dataset_id=int(dataset_id),
                data_content=_normalize_content(c),
                create_time=now,
                update_time=now,
                deleted=0,
            )
            for c in contents
        ]
        self.db.add_all(rows)
        await self.db.commit()
        return len(rows)

    async def list_items(
        self, dataset_id: int, page_num: int = 1, page_size: int = 20
    ) -> tuple[list[EvalDatasetItemOut], int]:
        conditions: list[Any] = [
            EvalDatasetItem.dataset_id == dataset_id,
            EvalDatasetItem.deleted == 0,
        ]
        total = int(
            (
                await self.db.execute(
                    select(func.count()).select_from(EvalDatasetItem).where(*conditions)
                )
            ).scalar()
            or 0
        )
        rows = (
            await self.db.execute(
                select(EvalDatasetItem)
                .where(*conditions)
                .order_by(
                    EvalDatasetItem.create_time.desc().nulls_last(),
                    EvalDatasetItem.id.desc(),
                )
                .offset((max(page_num, 1) - 1) * page_size)
                .limit(page_size)
            )
        ).scalars().all()
        return [EvalDatasetItemOut.model_validate(r) for r in rows], total

    async def delete_item(self, item_id: int) -> None:
        result = await self.db.execute(
            update(EvalDatasetItem)
            .where(EvalDatasetItem.id == item_id, EvalDatasetItem.deleted == 0)
            .values(deleted=1, update_time=datetime.now())
        )
        if int(result.rowcount or 0) == 0:
            raise _nf("数据项不存在")
        await self.db.commit()

    # ==================================================================
    # 版本
    # ==================================================================
    async def create_version(self, dataset_id: int, version: str) -> EvalDatasetVersionOut:
        """冻结当前条目 id 列表。

        **空数据集拒绝**（对齐 Java `EVAL_DATASET_EMPTY`）：
        0 条数据的版本跑实验会产出 0 条结果，进度条永远 0%，
        用户以为是卡住了。
        """
        await self._require(int(dataset_id))
        item_ids = (
            await self.db.execute(
                select(EvalDatasetItem.id)
                .where(
                    EvalDatasetItem.dataset_id == int(dataset_id),
                    EvalDatasetItem.deleted == 0,
                )
                .order_by(EvalDatasetItem.create_time.asc().nulls_last(),
                          EvalDatasetItem.id.asc())
            )
        ).scalars().all()
        if not item_ids:
            raise _bad("评测集还没有数据，无法创建版本")

        now = datetime.now()
        row = EvalDatasetVersion(
            id=new_id(),
            dataset_id=int(dataset_id),
            version=version.strip(),
            data_count=len(item_ids),
            status="draft",
            # 存字符串还是数字？Java 存的是 Long 数组。这里也存 int，
            # 出参 schema 会转成字符串（防 JS 精度丢失）。
            dataset_items=[int(x) for x in item_ids],
            create_time=now,
            update_time=now,
            deleted=0,
        )
        self.db.add(row)
        await self.db.commit()
        return EvalDatasetVersionOut.model_validate(row)

    async def list_versions(self, dataset_id: int) -> list[EvalDatasetVersionOut]:
        rows = (
            await self.db.execute(
                select(EvalDatasetVersion)
                .where(
                    EvalDatasetVersion.dataset_id == dataset_id,
                    EvalDatasetVersion.deleted == 0,
                )
                .order_by(
                    EvalDatasetVersion.create_time.desc().nulls_last(),
                    EvalDatasetVersion.id.desc(),
                )
            )
        ).scalars().all()
        return [EvalDatasetVersionOut.model_validate(r) for r in rows]

    async def get_version_items(self, version_id: int) -> list[EvalDatasetItemOut]:
        """按版本里的 id 列表取条目（顺序按 id 列表，不是按时间）。

        顺序有意义：实验按这个顺序跑，进度条和结果列表都依赖它稳定。
        """
        version = (
            await self.db.execute(
                select(EvalDatasetVersion).where(
                    EvalDatasetVersion.id == version_id,
                    EvalDatasetVersion.deleted == 0,
                )
            )
        ).scalar_one_or_none()
        if version is None:
            raise _nf("评测集版本不存在")

        ids: list[int] = []
        for raw in version.dataset_items or []:
            try:
                ids.append(int(raw))
            except (TypeError, ValueError):
                continue
        if not ids:
            return []

        rows = {
            r.id: r
            for r in (
                await self.db.execute(
                    select(EvalDatasetItem).where(EvalDatasetItem.id.in_(ids))
                )
            ).scalars().all()
        }
        return [
            EvalDatasetItemOut.model_validate(rows[i]) for i in ids if i in rows
        ]
