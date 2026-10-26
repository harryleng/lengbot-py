"""评测实验：CRUD + **真正的执行引擎**。

对应 Java `EvalExperimentServiceImpl` + `EvalExperimentResultServiceImpl`。

### 执行链路（一次实验）

```
数据集版本.dataset_items（id 列表）
  → 逐条取 item.data_content（如 {"input": "...", "reference_output": "..."}）
  → 按 experiment.evaluationObjectConfig.config.variableMap 映射成 prompt 变量
      {"input": "什么是 RAG"} → {"question": "什么是 RAG"}
  → 调用被测 Prompt（拿 datasetVersion 对应的 promptVersion 模板）
  → 得到 actual_output
  → 对每个 evaluatorConfig[i]：
      按它的 variableMap 组装评估器变量
        {"input": 来自数据集, "reference_output": 来自数据集, "actual_output": 刚生成的}
      → 调评估器 → score + reason
  → 落 eval_experiment_result（每个 样本×评估器 一行）
```

### 三处和 Java 的差异（都是刻意的）

**1. 不建 `task` 记录来排队，只建一条记录来存状态。**
Java 用 `TaskService.createTask` + 独立的 worker 扫表执行，支持重启恢复。
Python 版用 `asyncio.create_task` 在进程内跑 —— **重启即丢**。
所以这里仍然写 `task` 表（前端"停止"要写 `cancel_requested`、
失败要能查 `error`），但不做重试/恢复。`TaskRecord` 的 docstring 里写明了。

**2. 评测对象只支持 `type=prompt`。**
Java 还支持 `type=agent`（跑 Agent 链路）。Python 版要做就得复用
`ChatStreamService`，跨模块耦合大，且当前库里 2 个实验全是 prompt。
遇到其它 type 明确报 400，不静默跳过 —— 跳过会得到 0 条结果，
用户以为跑完了。

**3. 并发度低（3）。**
每个样本要 1 次生成 + N 次评估调 LLM。20 条 × 2 评估器 = 60 次调用，
全并发会撞供应商限流（之前踩过 429）。串行又太慢。
3 并发是折中，且每次调用之间不用 sleep —— 靠信号量压住峰值。
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import SessionLocal
from app.core.snowflake import new_id
from app.models.entities import (
    EvalDataset,
    EvalDatasetItem,
    EvalDatasetVersion,
    EvalEvaluator,
    EvalEvaluatorVersion,
    EvalExperiment,
    EvalExperimentResult,
    PromptVersion,
    TaskRecord,
)
from app.schemas.eval import (
    MAX_EVALUATORS,
    EvalExperimentIn,
    EvalExperimentOut,
    EvalExperimentOverviewOut,
    EvalExperimentResultOut,
    parse_json_value,
)

logger = logging.getLogger(__name__)

# 同时在跑的样本数（每个样本内部是串行调用）
SAMPLE_CONCURRENCY = 3
# 单次实验最多跑多少条样本（防止误配 1000 条把 key 烧光）
MAX_SAMPLES_PER_RUN = 200

STATUS_DRAFT = "draft"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_STOPPED = "stopped"


def _bad(msg: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=msg)


def _nf(msg: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=msg)


class EvalExperimentService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # ==================================================================
    # 查询
    # ==================================================================
    async def list(
        self,
        user_id: int,
        page_num: int = 1,
        page_size: int = 20,
        keyword: str | None = None,
        status_filter: str | None = None,
    ) -> tuple[list[EvalExperimentOut], int]:
        conditions: list[Any] = [
            EvalExperiment.deleted == 0,
            EvalExperiment.user_id == user_id,
        ]
        if keyword and keyword.strip():
            conditions.append(EvalExperiment.name.ilike(f"%{keyword.strip()}%"))
        if status_filter and status_filter.strip():
            conditions.append(EvalExperiment.status == status_filter.strip())

        total = int(
            (
                await self.db.execute(
                    select(func.count()).select_from(EvalExperiment).where(*conditions)
                )
            ).scalar()
            or 0
        )
        rows = (
            await self.db.execute(
                select(EvalExperiment)
                .where(*conditions)
                .order_by(
                    EvalExperiment.create_time.desc().nulls_last(), EvalExperiment.id.desc()
                )
                .offset((max(page_num, 1) - 1) * page_size)
                .limit(page_size)
            )
        ).scalars().all()

        # 注意：`_enrich_many` 返回的第二个值是**本页条数**，不是 total。
        # 分页信息必须用上面查出来的 total —— 用错的话前端永远只有一页
        # （total == pageSize），翻不到第二页。
        items, _ = await self._enrich_many(list(rows))
        return items, total

    async def _enrich_many(
        self, rows: list[EvalExperiment]
    ) -> tuple[list[EvalExperimentOut], int]:
        """批量补展示字段（datasetName / promptKey / evaluatorNameList）。

        为什么要批量：列表 20 条 × 每条的评估器逐个查，就是 20×N 次往返。
        Java 也专门做了 `enrichExperiment(exp, datasetMap)` 的预加载版本。
        """
        if not rows:
            return [], 0

        dataset_ids = {r.dataset_id for r in rows if r.dataset_id}
        datasets = {
            d.id: d.name
            for d in (
                await self.db.execute(
                    select(EvalDataset).where(EvalDataset.id.in_(dataset_ids))
                )
            ).scalars().all()
        } if dataset_ids else {}

        # 收集所有 evaluatorVersionId
        version_ids: set[int] = set()
        for r in rows:
            for cfg in (r.evaluator_config or []):
                if not isinstance(cfg, dict):
                    continue
                raw = cfg.get("evaluatorVersionId")
                try:
                    version_ids.add(int(raw))
                except (TypeError, ValueError):
                    continue

        versions = {
            v.id: v
            for v in (
                await self.db.execute(
                    select(EvalEvaluatorVersion).where(EvalEvaluatorVersion.id.in_(version_ids))
                )
            ).scalars().all()
        } if version_ids else {}

        evaluator_ids = {v.evaluator_id for v in versions.values() if v.evaluator_id}
        evaluators = {
            e.id: e.name
            for e in (
                await self.db.execute(
                    select(EvalEvaluator).where(EvalEvaluator.id.in_(evaluator_ids))
                )
            ).scalars().all()
        } if evaluator_ids else {}

        out: list[EvalExperimentOut] = []
        for r in rows:
            item = EvalExperimentOut.model_validate(r)
            if r.dataset_id:
                item.dataset_name = datasets.get(int(r.dataset_id))
            # 从 evaluationObjectConfig 抽 prompt 信息
            obj_cfg = r.evaluation_object_config or {}
            cfg = obj_cfg.get("config") if isinstance(obj_cfg, dict) else None
            if isinstance(cfg, dict):
                item.prompt_key = cfg.get("promptKey")
                item.prompt_version = cfg.get("version")
            # 从 evaluatorConfig 抽评估器名/版本
            names: list[str] = []
            vers: list[str] = []
            ids: list[str] = []
            for raw_cfg in (r.evaluator_config or []):
                if not isinstance(raw_cfg, dict):
                    continue
                try:
                    ev_id = int(raw_cfg.get("evaluatorVersionId"))
                except (TypeError, ValueError):
                    continue
                ver = versions.get(ev_id)
                if ver is None:
                    names.append("未知")
                    vers.append("")
                    continue
                names.append(evaluators.get(ver.evaluator_id, f"评估器#{ver.evaluator_id}"))
                vers.append(str(ver.version))
                ids.append(str(ver.evaluator_id))
            item.evaluator_name_list = names
            item.evaluator_version_list = vers
            item.evaluator_id_list = ids
            # 兼容旧字段：取第一个
            if names:
                item.evaluator_name = names[0]
                item.evaluator_version = vers[0]
            out.append(item)
        return out, len(out)

    async def _require(self, experiment_id: int) -> EvalExperiment:
        row = (
            await self.db.execute(
                select(EvalExperiment).where(
                    EvalExperiment.id == experiment_id, EvalExperiment.deleted == 0
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise _nf("实验不存在")
        return row

    async def get(self, experiment_id: int) -> EvalExperimentOut:
        row = await self._require(experiment_id)
        out, _ = await self._enrich_many([row])
        return out[0]

    # ==================================================================
    # 写
    # ==================================================================
    @staticmethod
    def _validate_evaluator_config(raw: Any) -> list[dict]:
        """校验并归一评估器配置。

        Java 只校验"数量 <= 5"和"能解析成数组"。这里同样，
        再加一条：每项必须有 `evaluatorVersionId`（没有就跑不了，
        等到执行时才报错，用户已经等了半天）。
        """
        try:
            parsed = parse_json_value(raw, default=[])
        except ValueError as exc:
            raise _bad(f"evaluatorConfig 不是合法 JSON：{exc}") from exc
        if not isinstance(parsed, list):
            raise _bad("evaluatorConfig 必须是数组")
        if len(parsed) > MAX_EVALUATORS:
            raise _bad(f"最多只能选择 {MAX_EVALUATORS} 个评估器")
        if not parsed:
            raise _bad("至少选择一个评估器")
        for idx, cfg in enumerate(parsed):
            if not isinstance(cfg, dict):
                raise _bad(f"evaluatorConfig 第 {idx + 1} 项不是对象")
            if not cfg.get("evaluatorVersionId"):
                raise _bad(f"evaluatorConfig 第 {idx + 1} 项缺少 evaluatorVersionId")
        return parsed

    async def create(self, dto: EvalExperimentIn, user_id: int) -> EvalExperimentOut:
        if not dto.dataset_version_id:
            raise _bad("必须指定数据集版本")

        dataset_version = (
            await self.db.execute(
                select(EvalDatasetVersion).where(
                    EvalDatasetVersion.id == int(dto.dataset_version_id),
                    EvalDatasetVersion.deleted == 0,
                )
            )
        ).scalar_one_or_none()
        if dataset_version is None:
            raise _nf("数据集版本不存在")

        evaluator_cfg = self._validate_evaluator_config(dto.evaluator_config)
        try:
            object_cfg = parse_json_value(dto.evaluation_object_config, default={})
        except ValueError as exc:
            raise _bad(f"evaluationObjectConfig 不是合法 JSON：{exc}") from exc

        now = datetime.now()
        row = EvalExperiment(
            id=new_id(),
            name=dto.name.strip(),
            description=dto.description,
            dataset_id=int(dto.dataset_id) if dto.dataset_id else dataset_version.dataset_id,
            dataset_version_id=int(dto.dataset_version_id),
            dataset_version=dto.dataset_version or dataset_version.version,
            evaluation_object_config=object_cfg if isinstance(object_cfg, dict) else {},
            evaluator_config=evaluator_cfg,
            status=STATUS_RUNNING,
            progress=0,
            user_id=user_id,
            create_time=now,
            update_time=now,
            deleted=0,
        )
        self.db.add(row)

        # 建一条 task 记录：前端「停止」写 cancel_requested，失败原因也存这
        task = TaskRecord(
            id=new_id(),
            name=f"评测实验: {row.name}",
            type="experiment_run",
            status="running",
            progress=0,
            payload={"experimentId": str(row.id)},
            cancel_requested=0,
            user_id=user_id,
            ref_id=row.id,
            create_time=now,
            update_time=now,
            started_at=now,
            deleted=0,
        )
        self.db.add(task)
        row.task_id = task.id
        await self.db.commit()

        # 丢到后台跑（进程内）
        _spawn_experiment(row.id, user_id)

        out, _ = await self._enrich_many([row])
        return out[0]

    async def update(self, experiment_id: int, dto: EvalExperimentIn) -> EvalExperimentOut:
        row = await self._require(experiment_id)
        if row.status == STATUS_RUNNING:
            raise _bad("运行中的实验不能修改，请先停止")

        values: dict[str, Any] = {"update_time": datetime.now()}
        if dto.name is not None and dto.name.strip():
            values["name"] = dto.name.strip()
        if dto.description is not None:
            values["description"] = dto.description
        if dto.dataset_id is not None:
            values["dataset_id"] = int(dto.dataset_id)
        if dto.dataset_version_id is not None:
            values["dataset_version_id"] = int(dto.dataset_version_id)
        if dto.dataset_version is not None:
            values["dataset_version"] = dto.dataset_version
        if dto.evaluation_object_config is not None:
            try:
                cfg = parse_json_value(dto.evaluation_object_config, default={})
            except ValueError as exc:
                raise _bad(f"evaluationObjectConfig 不是合法 JSON：{exc}") from exc
            values["evaluation_object_config"] = cfg if isinstance(cfg, dict) else {}
        if dto.evaluator_config is not None:
            values["evaluator_config"] = self._validate_evaluator_config(dto.evaluator_config)

        await self.db.execute(
            update(EvalExperiment).where(EvalExperiment.id == experiment_id).values(**values)
        )
        await self.db.commit()
        return await self.get(experiment_id)

    async def stop(self, experiment_id: int) -> None:
        """停止 = 给 task 打 `cancel_requested`，**执行器自己检查后退出**。

        为什么不直接把 status 改成 stopped：执行器还在跑，它每完成一条会写
        `progress`。如果这里改了 status，执行器下一轮又会把它改回 running。
        正确的协作方式是"只发信号"，状态由执行器改。
        """
        row = await self._require(experiment_id)
        if row.status != STATUS_RUNNING:
            raise _bad("只有运行中的实验才能停止")
        if row.task_id:
            await self.db.execute(
                update(TaskRecord)
                .where(TaskRecord.id == row.task_id)
                .values(cancel_requested=1, update_time=datetime.now())
            )
            await self.db.commit()
            logger.info("请求停止实验 %s（task=%s）", experiment_id, row.task_id)
        else:
            # 没有 task（理论上不会发生），直接置状态兜底
            await self.db.execute(
                update(EvalExperiment)
                .where(EvalExperiment.id == experiment_id)
                .values(status=STATUS_STOPPED, update_time=datetime.now())
            )
            await self.db.commit()

    async def delete(self, experiment_id: int) -> None:
        row = await self._require(experiment_id)
        if row.status == STATUS_RUNNING:
            raise _bad("运行中的实验不能删除，请先停止")
        # 级联删结果（Java 也是 removeByExperimentId）
        await self.db.execute(
            delete(EvalExperimentResult).where(
                EvalExperimentResult.experiment_id == experiment_id
            )
        )
        await self.db.execute(
            update(EvalExperiment)
            .where(EvalExperiment.id == experiment_id)
            .values(deleted=1, update_time=datetime.now())
        )
        await self.db.commit()

    async def restart(self, experiment_id: int) -> EvalExperimentOut:
        """重跑：清旧结果 + 重置状态 + 重新起任务。"""
        row = await self._require(experiment_id)
        if row.status == STATUS_RUNNING:
            raise _bad("实验正在运行中")

        await self.db.execute(
            delete(EvalExperimentResult).where(
                EvalExperimentResult.experiment_id == experiment_id
            )
        )
        now = datetime.now()
        task = TaskRecord(
            id=new_id(),
            name=f"重启实验: {row.name}",
            type="experiment_run",
            status="running",
            progress=0,
            payload={"experimentId": str(experiment_id)},
            cancel_requested=0,
            user_id=row.user_id,
            ref_id=row.id,
            create_time=now,
            update_time=now,
            started_at=now,
            deleted=0,
        )
        self.db.add(task)
        await self.db.execute(
            update(EvalExperiment)
            .where(EvalExperiment.id == experiment_id)
            .values(
                status=STATUS_RUNNING,
                progress=0,
                complete_time=None,
                task_id=task.id,
                update_time=now,
            )
        )
        await self.db.commit()

        _spawn_experiment(experiment_id, int(row.user_id or 0))
        return await self.get(experiment_id)

    # ==================================================================
    # 结果
    # ==================================================================
    async def overview(self, experiment_id: int) -> list[EvalExperimentOverviewOut]:
        """按评估器分组算平均分。

        `total_count` 用**结果行数**（不是数据集条数）—— Java 也是
        `entry.getValue().size()`。所以如果一条样本某个评估器失败了没落库，
        `total_count` 会小于数据集条数，`evaluatedCount == totalCount` 恒成立。
        """
        rows = (
            await self.db.execute(
                select(EvalExperimentResult).where(
                    EvalExperimentResult.experiment_id == experiment_id,
                    EvalExperimentResult.deleted == 0,
                )
            )
        ).scalars().all()

        grouped: dict[int, list[EvalExperimentResult]] = {}
        for r in rows:
            if r.evaluator_version_id is None:
                continue
            grouped.setdefault(int(r.evaluator_version_id), []).append(r)
        if not grouped:
            return []

        versions = {
            v.id: v
            for v in (
                await self.db.execute(
                    select(EvalEvaluatorVersion).where(
                        EvalEvaluatorVersion.id.in_(list(grouped))
                    )
                )
            ).scalars().all()
        }
        ev_ids = {v.evaluator_id for v in versions.values() if v.evaluator_id}
        names = {
            e.id: e.name
            for e in (
                await self.db.execute(
                    select(EvalEvaluator).where(EvalEvaluator.id.in_(ev_ids))
                )
            ).scalars().all()
        } if ev_ids else {}

        out: list[EvalExperimentOverviewOut] = []
        for version_id, items in grouped.items():
            scores = [float(x.score) for x in items if x.score is not None]
            avg = round(sum(scores) / len(scores), 2) if scores else 0.0
            ver = versions.get(version_id)
            out.append(
                EvalExperimentOverviewOut(
                    evaluator_version_id=str(version_id),
                    evaluator_name=(
                        names.get(ver.evaluator_id, f"评估器#{ver.evaluator_id}")
                        if ver
                        else "未知"
                    ),
                    evaluator_version=str(ver.version) if ver else None,
                    avg_score=avg,
                    evaluated_count=len(items),
                    total_count=len(items),
                )
            )
        # 稳定顺序（按版本 id 升序），否则前端每次刷新列顺序会变
        out.sort(key=lambda x: int(x.evaluator_version_id))
        return out

    async def list_results(
        self,
        experiment_id: int,
        evaluator_version_id: int | None,
        page_num: int = 1,
        page_size: int = 20,
    ) -> tuple[list[EvalExperimentResultOut], int]:
        conditions: list[Any] = [
            EvalExperimentResult.experiment_id == experiment_id,
            EvalExperimentResult.deleted == 0,
        ]
        if evaluator_version_id is not None:
            conditions.append(
                EvalExperimentResult.evaluator_version_id == evaluator_version_id
            )
        total = int(
            (
                await self.db.execute(
                    select(func.count())
                    .select_from(EvalExperimentResult)
                    .where(*conditions)
                )
            ).scalar()
            or 0
        )
        rows = (
            await self.db.execute(
                select(EvalExperimentResult)
                .where(*conditions)
                .order_by(
                    EvalExperimentResult.evaluation_time.desc().nulls_last(),
                    EvalExperimentResult.id.desc(),
                )
                .offset((max(page_num, 1) - 1) * page_size)
                .limit(page_size)
            )
        ).scalars().all()
        return [EvalExperimentResultOut.model_validate(r) for r in rows], total


# ======================================================================
# 执行引擎（后台任务）
# ======================================================================
# 进程内任务登记表：只用于防止同一个实验被并发跑两份（前端连点两次"重跑"）。
# 重启即丢 —— 丢了也没关系，rebuild 时会重新启动。
_RUNNING: dict[int, asyncio.Task] = {}


def _spawn_experiment(experiment_id: int, user_id: int) -> None:
    """把执行丢到后台。已在跑就跳过（幂等）。"""
    existing = _RUNNING.get(experiment_id)
    if existing is not None and not existing.done():
        logger.warning("实验 %s 已经在跑，跳过重复启动", experiment_id)
        return
    from app.services import task_recovery

    task = asyncio.create_task(
        task_recovery.tracked(experiment_id, _run_experiment(experiment_id, user_id))
    )
    _RUNNING[experiment_id] = task

    def _cleanup(_: asyncio.Task) -> None:
        _RUNNING.pop(experiment_id, None)

    task.add_done_callback(_cleanup)


async def _task_cancelled(task_id: int | None) -> bool:
    """读 cancel_requested（每轮都读一次库，不缓存）。"""
    if not task_id:
        return False
    async with SessionLocal() as db:
        flag = (
            await db.execute(
                select(TaskRecord.cancel_requested).where(TaskRecord.id == task_id)
            )
        ).scalar()
    return bool(flag)


async def _set_progress(experiment_id: int, task_id: int | None, done: int, total: int) -> None:
    pct = int(done * 100 / total) if total else 0
    now = datetime.now()
    async with SessionLocal() as db:
        await db.execute(
            update(EvalExperiment)
            .where(EvalExperiment.id == experiment_id)
            .values(progress=pct, update_time=now)
        )
        if task_id:
            await db.execute(
                update(TaskRecord)
                .where(TaskRecord.id == task_id)
                .values(progress=pct, message=f"{done}/{total}", update_time=now)
            )
        await db.commit()


async def _finish(
    experiment_id: int,
    task_id: int | None,
    *,
    status: str,
    error: str | None = None,
) -> None:
    now = datetime.now()
    values: dict[str, Any] = {
        "status": status,
        "complete_time": now,
        "update_time": now,
    }
    # 只有"成功"才把进度补到 100。停止/失败时保留实际进度，
    # 否则前端会显示"100% 但状态是失败"，自相矛盾。
    if status == STATUS_COMPLETED:
        values["progress"] = 100

    async with SessionLocal() as db:
        await db.execute(
            update(EvalExperiment).where(EvalExperiment.id == experiment_id).values(**values)
        )
        if task_id:
            await db.execute(
                update(TaskRecord)
                .where(TaskRecord.id == task_id)
                .values(
                    status="completed" if status == STATUS_COMPLETED else status,
                    error=error,
                    completed_at=now,
                    update_time=now,
                )
            )
        await db.commit()
    logger.info("实验 %s 结束：status=%s error=%s", experiment_id, status, error)


def _map_variables(
    item: dict[str, Any], mapping: list[dict], *, source_key: str, target_key: str
) -> dict[str, Any]:
    """按 variableMap 从数据集条目里取变量。

    `variableMap` 的形状（两种，来自不同地方）：
    - 评测对象：`[{"datasetColumn": "input", "promptVariable": "question"}]`
    - 评估器：  `[{"source": "input", "evaluatorVariable": "input"}]`

    映射不到的目标变量给空串而不是缺 key —— 模板里 `{{x}}` 找不到替换
    就会原样留着 `{{x}}`，发给模型很奇怪。
    """
    out: dict[str, Any] = {}
    for m in mapping or []:
        if not isinstance(m, dict):
            continue
        src = m.get(source_key)
        tgt = m.get(target_key)
        if not tgt:
            continue
        value = item.get(str(src)) if src else None
        out[str(tgt)] = "" if value is None else value
    return out


async def _load_context(experiment_id: int) -> dict[str, Any]:
    """一次性把执行需要的东西全捞出来（避免循环里反复查库）。"""
    async with SessionLocal() as db:
        exp = (
            await db.execute(
                select(EvalExperiment).where(EvalExperiment.id == experiment_id)
            )
        ).scalar_one_or_none()
        if exp is None:
            raise RuntimeError(f"实验 {experiment_id} 不存在")

        version = (
            await db.execute(
                select(EvalDatasetVersion).where(
                    EvalDatasetVersion.id == exp.dataset_version_id
                )
            )
        ).scalar_one_or_none()
        if version is None:
            raise RuntimeError("数据集版本不存在")

        ids: list[int] = []
        for raw in version.dataset_items or []:
            try:
                ids.append(int(raw))
            except (TypeError, ValueError):
                continue

        items = {
            i.id: i
            for i in (
                await db.execute(
                    select(EvalDatasetItem).where(EvalDatasetItem.id.in_(ids))
                )
            ).scalars().all()
        } if ids else {}

        # 按 version 里的顺序（进度和结果顺序都依赖它）
        samples: list[dict[str, Any]] = []
        for sid in ids:
            row = items.get(sid)
            if row is None or row.deleted != 0:
                continue
            content = row.data_content if isinstance(row.data_content, dict) else {}
            samples.append(content)

        # 评测对象：prompt 版本 → 模板 + 模型配置
        obj_cfg = exp.evaluation_object_config or {}
        cfg = obj_cfg.get("config") if isinstance(obj_cfg, dict) else {}
        cfg = cfg if isinstance(cfg, dict) else {}
        prompt_key = cfg.get("promptKey")
        prompt_version = cfg.get("version")

        prompt_row = (
            await db.execute(
                select(PromptVersion)
                .where(
                    PromptVersion.prompt_key == prompt_key,
                    PromptVersion.version == prompt_version,
                    PromptVersion.deleted == 0,
                )
                .order_by(PromptVersion.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if prompt_row is None:
            raise RuntimeError(
                f"Prompt 版本不存在：{prompt_key}@{prompt_version}"
            )

        # 评估器：每个 version id 的 prompt + model_config
        evaluators: list[dict[str, Any]] = []
        for raw_cfg in exp.evaluator_config or []:
            if not isinstance(raw_cfg, dict):
                continue
            try:
                ev_version_id = int(raw_cfg.get("evaluatorVersionId"))
            except (TypeError, ValueError):
                continue
            ver = (
                await db.execute(
                    select(EvalEvaluatorVersion).where(
                        EvalEvaluatorVersion.id == ev_version_id
                    )
                )
            ).scalar_one_or_none()
            if ver is None:
                continue
            name = (
                await db.execute(
                    select(EvalEvaluator.name).where(
                        EvalEvaluator.id == ver.evaluator_id
                    )
                )
            ).scalar()
            evaluators.append(
                {
                    "version_id": ev_version_id,
                    "name": name or f"评估器#{ver.evaluator_id}",
                    "version": ver.version,
                    "prompt": ver.prompt,
                    "model_config": ver.model_config,
                    "variable_map": raw_cfg.get("variableMap") or [],
                }
            )

        return {
            "experiment": exp,
            "object_type": (obj_cfg.get("type") if isinstance(obj_cfg, dict) else None)
            or "prompt",
            "variable_map": cfg.get("variableMap") or [],
            "prompt_template": prompt_row.template,
            "prompt_model_config": prompt_row.model_config,
            "samples": samples,
            "evaluators": evaluators,
        }


async def _run_one_sample(
    ctx: dict[str, Any], sample: dict[str, Any], sem: asyncio.Semaphore
) -> list[dict[str, Any]]:
    """跑一条样本：生成回答 → 每个评估器打分。返回待落库的结果行。"""
    async with sem:
        from app.services.eval_judge_service import EvalJudgeService

        # 生成阶段：用一个独立 session（长事务会卡住其他写入）
        async with SessionLocal() as db:
            judge = EvalJudgeService(db)
            prompt_vars = _map_variables(
                sample, ctx["variable_map"], source_key="datasetColumn",
                target_key="promptVariable",
            )
            try:
                actual = await judge.generate(
                    ctx["prompt_model_config"], ctx["prompt_template"], prompt_vars
                )
                gen_error: str | None = None
            except Exception as exc:  # noqa: BLE001
                # 生成失败不能让整个实验崩 —— 记下来，评估照跑（分数会是 0）
                logger.warning("样本生成失败：%s", exc)
                actual = ""
                gen_error = f"{type(exc).__name__}: {exc}"

        rows: list[dict[str, Any]] = []
        for ev in ctx["evaluators"]:
            ev_vars = _map_variables(
                sample, ev["variable_map"], source_key="source",
                target_key="evaluatorVariable",
            )
            # actual_output 是运行时产物，映射表里通常写 source=actual_output，
            # 但那条 source 在数据集里找不到 —— 所以这里显式覆盖。
            for key in ("actual_output", "output"):
                if key in ev_vars or key in (ev.get("variables") or []):
                    ev_vars[key] = actual
            # 兜底：评估器变量清单里明确要 actual_output 时一定给
            if "actual_output" not in ev_vars:
                ev_vars["actual_output"] = actual

            async with SessionLocal() as db:
                judge = EvalJudgeService(db)
                try:
                    score = await judge.judge(
                        ev["model_config"], ev["prompt"], ev_vars
                    )
                    final_score = score.score
                    reason = score.reason
                    if gen_error:
                        reason = f"[生成失败] {gen_error}；{reason or ''}"
                except Exception as exc:  # noqa: BLE001
                    logger.warning("评估失败 evaluator=%s: %s", ev["name"], exc)
                    final_score = 0.0
                    reason = f"评估失败: {type(exc).__name__}: {exc}"

            rows.append(
                {
                    "evaluator_version_id": ev["version_id"],
                    "evaluator_name": ev["name"],
                    "input": str(prompt_vars.get("input") or sample.get("input") or ""),
                    "actual_output": actual,
                    "reference_output": str(sample.get("reference_output") or ""),
                    "score": final_score,
                    "reason": reason,
                }
            )
        return rows


async def _run_experiment(experiment_id: int, user_id: int) -> None:
    """后台执行主体。**所有异常都收在这里**，绝不让它冒到事件循环。"""
    task_id: int | None = None
    try:
        ctx = await _load_context(experiment_id)
        samples = ctx["samples"]
        async with SessionLocal() as db:
            task_id = (
                await db.execute(
                    select(EvalExperiment.task_id).where(
                        EvalExperiment.id == experiment_id
                    )
                )
            ).scalar()

        if not samples:
            await _finish(
                experiment_id, task_id, status=STATUS_FAILED,
                error="数据集版本里没有可用的样本",
            )
            return

        if ctx["object_type"] != "prompt":
            await _finish(
                experiment_id, task_id, status=STATUS_FAILED,
                error=f"暂不支持评测对象类型：{ctx['object_type']}（目前只支持 prompt）",
            )
            return

        samples = samples[:MAX_SAMPLES_PER_RUN]
        total = len(samples)
        sem = asyncio.Semaphore(SAMPLE_CONCURRENCY)
        done = 0
        stopped = False

        logger.info(
            "实验 %s 开跑：%d 条样本 × %d 个评估器（并发 %d）",
            experiment_id, total, len(ctx["evaluators"]), SAMPLE_CONCURRENCY,
        )

        # 分批：并发跑一批，写一批结果，更新一次进度
        batch_size = SAMPLE_CONCURRENCY * 2
        for start in range(0, total, batch_size):
            if await _task_cancelled(task_id):
                stopped = True
                break

            chunk = samples[start : start + batch_size]
            results = await asyncio.gather(
                *[_run_one_sample(ctx, s, sem) for s in chunk]
            )

            now = datetime.now()
            async with SessionLocal() as db:
                for rows in results:
                    for r in rows:
                        db.add(
                            EvalExperimentResult(
                                id=new_id(),
                                experiment_id=experiment_id,
                                input=r["input"],
                                actual_output=r["actual_output"],
                                reference_output=r["reference_output"],
                                score=r["score"],
                                reason=r["reason"],
                                evaluator_version_id=r["evaluator_version_id"],
                                evaluator_name=r["evaluator_name"],
                                evaluation_time=now,
                                create_time=now,
                                update_time=now,
                                deleted=0,
                            )
                        )
                await db.commit()

            done += len(chunk)
            await _set_progress(experiment_id, task_id, done, total)

        await _finish(
            experiment_id, task_id,
            status=STATUS_STOPPED if stopped else STATUS_COMPLETED,
            error="用户已停止" if stopped else None,
        )

    except Exception as exc:  # noqa: BLE001
        logger.exception("实验 %s 执行失败", experiment_id)
        await _finish(
            experiment_id, task_id, status=STATUS_FAILED,
            error=f"{type(exc).__name__}: {exc}",
        )


def parse_json_field(raw: Any) -> Any:
    """给 API 层用的小工具（避免到处 import parse_json_value）。"""
    try:
        return parse_json_value(raw)
    except ValueError:
        return None


__all__ = [
    "EvalExperimentService",
    "STATUS_COMPLETED",
    "STATUS_DRAFT",
    "STATUS_FAILED",
    "STATUS_RUNNING",
    "STATUS_STOPPED",
    "parse_json_field",
]


def _unused_json() -> None:
    """`json` 保留导入（文档里举了 JSON 例子，且后续可能直接用）。"""
    _ = json
