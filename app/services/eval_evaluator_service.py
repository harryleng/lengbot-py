"""评估器服务（评估器 / 版本 / 模板 / 单次测试）。

对应 Java `EvalEvaluatorServiceImpl` + `EvalEvaluatorVersionServiceImpl`。

### 与评测集不同的两处

**1. `evaluator` 和 `evaluator_version` 是两级，但版本没有"发布"概念。**
`status` 永远是 `draft`（Java 用 `EvalVersionStatus.DRAFT` 初始化），
`eval_experiment_result.evaluator_version_id` 直接引用具体版本 id。
所以**改提示词 = 新建一个版本**，老实验仍指向老版本 —— 这是评测可复现的基础。

**2. 版本创建不校验变量是否被 prompt 用到。**
Java 也不校验。用户填了 `variables=["input","actual_output"]`，
但 prompt 里只写了 `{{input}}`，跑起来 `actual_output` 就是空值。
这个校验放服务端做会挡住"分批补全"的编辑流程，交给前端提示更合适。

### tags 是逗号分隔字符串，不是数组

`eval_evaluator.tags` 是 varchar（`"准确性,完整性"`）。
前端 `tags.split(',')` 自己拆。给数组会变成 `"[object Object]"` 之类的怪东西。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.snowflake import new_id
from app.models.entities import (
    EvalEvaluator,
    EvalEvaluatorTemplate,
    EvalEvaluatorVersion,
)
from app.schemas.eval import (
    EvalEvaluatorIn,
    EvalEvaluatorOut,
    EvalEvaluatorTemplateOut,
    EvalEvaluatorTestIn,
    EvalEvaluatorVersionIn,
    EvalEvaluatorVersionOut,
    EvalScoreOut,
    parse_json_value,
)
from app.services.eval_judge_service import EvalJudgeService

logger = logging.getLogger(__name__)


def _bad(msg: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=msg)


def _nf(msg: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=msg)


class EvalEvaluatorService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # ==================================================================
    # 评估器
    # ==================================================================
    async def list(
        self, user_id: int, page_num: int = 1, page_size: int = 20, keyword: str | None = None
    ) -> tuple[list[EvalEvaluatorOut], int]:
        conditions: list[Any] = [EvalEvaluator.deleted == 0, EvalEvaluator.user_id == user_id]
        if keyword and keyword.strip():
            conditions.append(EvalEvaluator.name.ilike(f"%{keyword.strip()}%"))

        total = int(
            (
                await self.db.execute(
                    select(func.count()).select_from(EvalEvaluator).where(*conditions)
                )
            ).scalar()
            or 0
        )
        rows = (
            await self.db.execute(
                select(EvalEvaluator)
                .where(*conditions)
                .order_by(
                    EvalEvaluator.create_time.desc().nulls_last(), EvalEvaluator.id.desc()
                )
                .offset((max(page_num, 1) - 1) * page_size)
                .limit(page_size)
            )
        ).scalars().all()
        return [EvalEvaluatorOut.model_validate(r) for r in rows], total

    async def _require(self, evaluator_id: int) -> EvalEvaluator:
        row = (
            await self.db.execute(
                select(EvalEvaluator).where(
                    EvalEvaluator.id == evaluator_id, EvalEvaluator.deleted == 0
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise _nf("评估器不存在")
        return row

    async def get(self, evaluator_id: int) -> EvalEvaluatorOut:
        return EvalEvaluatorOut.model_validate(await self._require(evaluator_id))

    async def _ensure_name_free(self, name: str, exclude_id: int | None = None) -> None:
        conditions: list[Any] = [EvalEvaluator.name == name, EvalEvaluator.deleted == 0]
        if exclude_id is not None:
            conditions.append(EvalEvaluator.id != exclude_id)
        if (await self.db.execute(select(EvalEvaluator.id).where(*conditions))).first():
            raise _bad(f"评估器名称已存在：{name}")

    async def create(self, dto: EvalEvaluatorIn, user_id: int) -> EvalEvaluatorOut:
        name = dto.name.strip()
        await self._ensure_name_free(name)
        now = datetime.now()
        row = EvalEvaluator(
            id=new_id(),
            name=name,
            description=dto.description,
            tags=dto.tags,
            user_id=user_id,
            create_time=now,
            update_time=now,
            deleted=0,
        )
        self.db.add(row)
        await self.db.commit()
        return EvalEvaluatorOut.model_validate(row)

    async def update(self, evaluator_id: int, dto: EvalEvaluatorIn) -> None:
        await self._require(evaluator_id)
        values: dict[str, Any] = {"update_time": datetime.now()}
        if dto.name is not None and dto.name.strip():
            name = dto.name.strip()
            await self._ensure_name_free(name, exclude_id=evaluator_id)
            values["name"] = name
        if dto.description is not None:
            values["description"] = dto.description
        if dto.tags is not None:
            values["tags"] = dto.tags
        await self.db.execute(
            update(EvalEvaluator).where(EvalEvaluator.id == evaluator_id).values(**values)
        )
        await self.db.commit()

    async def delete(self, evaluator_id: int) -> None:
        """软删评估器。

        **不级联删版本** —— 历史实验结果的 `evaluatorVersionId` 还要能查到
        "当时用的是哪个版本的提示词"。级联了就成了悬空引用。
        """
        await self._require(evaluator_id)
        await self.db.execute(
            update(EvalEvaluator)
            .where(EvalEvaluator.id == evaluator_id)
            .values(deleted=1, update_time=datetime.now())
        )
        await self.db.commit()

    # ==================================================================
    # 版本
    # ==================================================================
    async def create_version(
        self, dto: EvalEvaluatorVersionIn
    ) -> EvalEvaluatorVersionOut:
        evaluator_id = int(dto.evaluator_id)
        await self._require(evaluator_id)

        try:
            variables = parse_json_value(dto.variables, default=[])
            model_config = parse_json_value(dto.model_config_json, default={})
        except ValueError as exc:
            raise _bad(f"JSON 字段格式错误：{exc}") from exc

        now = datetime.now()
        row = EvalEvaluatorVersion(
            id=new_id(),
            evaluator_id=evaluator_id,
            version=dto.version.strip(),
            # 列名就叫 model_config（Pydantic 那边用 alias 绕开了）
            model_config=model_config if isinstance(model_config, dict) else {},
            prompt=dto.prompt,
            variables=variables if isinstance(variables, list) else [],
            status="draft",
            create_time=now,
            update_time=now,
            deleted=0,
        )
        self.db.add(row)
        await self.db.commit()
        return EvalEvaluatorVersionOut.model_validate(row)

    async def list_versions(self, evaluator_id: int) -> list[EvalEvaluatorVersionOut]:
        rows = (
            await self.db.execute(
                select(EvalEvaluatorVersion)
                .where(
                    EvalEvaluatorVersion.evaluator_id == evaluator_id,
                    EvalEvaluatorVersion.deleted == 0,
                )
                .order_by(
                    EvalEvaluatorVersion.create_time.desc().nulls_last(),
                    EvalEvaluatorVersion.id.desc(),
                )
            )
        ).scalars().all()
        return [EvalEvaluatorVersionOut.model_validate(r) for r in rows]

    async def _require_version(self, version_id: int) -> EvalEvaluatorVersion:
        row = (
            await self.db.execute(
                select(EvalEvaluatorVersion).where(
                    EvalEvaluatorVersion.id == version_id,
                    EvalEvaluatorVersion.deleted == 0,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise _nf("评估器版本不存在")
        return row

    # ==================================================================
    # 模板
    # ==================================================================
    async def list_templates(self) -> list[EvalEvaluatorTemplateOut]:
        rows = (
            await self.db.execute(
                select(EvalEvaluatorTemplate)
                .where(EvalEvaluatorTemplate.deleted == 0)
                .order_by(
                    EvalEvaluatorTemplate.create_time.desc().nulls_last(),
                    EvalEvaluatorTemplate.id.desc(),
                )
            )
        ).scalars().all()
        return [EvalEvaluatorTemplateOut.model_validate(r) for r in rows]

    async def get_template(self, key: str) -> EvalEvaluatorTemplateOut:
        row = (
            await self.db.execute(
                select(EvalEvaluatorTemplate).where(
                    EvalEvaluatorTemplate.evaluator_template_key == key,
                    EvalEvaluatorTemplate.deleted == 0,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise _nf(f"模板不存在：{key}")
        return EvalEvaluatorTemplateOut.model_validate(row)

    # ==================================================================
    # 单次测试（真调模型）
    # ==================================================================
    async def test(self, dto: EvalEvaluatorTestIn) -> EvalScoreOut | None:
        """跑一次评估器看打分。**版本不存在返回 null**（对齐 Java，不是 404）。

        为什么不是 404：前端"测试"按钮在版本下拉还没选好时也会触发，
        404 会弹一个红框；返回 `data: null` 前端只是不显示结果。
        """
        version = (
            await self.db.execute(
                select(EvalEvaluatorVersion).where(
                    EvalEvaluatorVersion.id == int(dto.evaluator_version_id),
                    EvalEvaluatorVersion.deleted == 0,
                )
            )
        ).scalar_one_or_none()
        if version is None:
            logger.warning("测试评估器：版本 %s 不存在", dto.evaluator_version_id)
            return None

        return await EvalJudgeService(self.db).judge(
            version.model_config, version.prompt, dto.variables
        )
