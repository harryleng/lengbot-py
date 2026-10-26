"""RAG 评估的基准 / 结果业务层 + 异步任务执行。

任务模型沿用本项目的既有约定（见 `TaskRecord` 实体注释）：
**进程内 `asyncio.create_task`，重启即丢** —— 这是已知限制，
不是持久队列。前端任务中心只是"有个地方看进度/取消"。
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import SessionLocal
from app.core.snowflake import new_id
from app.models.entities import (
    EvalRagBenchmark,
    EvalRagBenchmarkItem,
    EvalRagResult,
    EvalRagResultDetail,
    TaskRecord,
)
from app.schemas.common import PageResult
from app.schemas.knowledge_eval import (
    BenchmarkItemOut,
    BenchmarkOut,
    EvalResultDetailOut,
    EvalResultOut,
)
from app.services import rag_eval_engine as engine
from app.services.knowledge_service import KnowledgeService
from app.services.token_budget_service import BudgetExceeded

logger = logging.getLogger(__name__)


def _to_benchmark_out(row: EvalRagBenchmark, **extra: object) -> BenchmarkOut:
    base = {
        "id": str(row.id),
        "knowledgeId": str(row.knowledge_id),
        "name": row.name,
        "description": row.description,
        "questionCount": row.question_count,
        "status": row.status,
        "createTime": row.create_time,
        "updateTime": row.update_time,
    }
    base.update(extra)
    return BenchmarkOut(**base)


def _to_item_out(row: EvalRagBenchmarkItem) -> BenchmarkItemOut:
    return BenchmarkItemOut(
        id=str(row.id),
        benchmarkId=str(row.benchmark_id),
        query=row.query,
        goldChunkIds=row.gold_chunk_ids,
        goldAnswer=row.gold_answer,
        sortOrder=row.sort_order,
    )


def _to_result_out(row: EvalRagResult) -> EvalResultOut:
    return EvalResultOut(
        id=str(row.id),
        knowledgeId=str(row.knowledge_id),
        benchmarkId=str(row.benchmark_id),
        benchmarkName=row.benchmark_name,
        status=row.status,
        overallScore=row.overall_score,
        retrievalJson=row.retrieval_json,
        answerJson=row.answer_json,
        configJson=row.config_json,
        durationMs=row.duration_ms,
        analysis=row.analysis,
        error=row.error,
        createTime=row.create_time,
        updateTime=row.update_time,
    )


def _to_detail_out(row: EvalRagResultDetail) -> EvalResultDetailOut:
    return EvalResultDetailOut(
        id=str(row.id),
        resultId=str(row.result_id),
        query=row.query,
        goldChunkIds=row.gold_chunk_ids,
        goldAnswer=row.gold_answer,
        generatedAnswer=row.generated_answer,
        retrievedChunkIds=row.retrieved_chunk_ids,
        retrievalScores=row.retrieval_scores,
        answerScore=row.answer_score,
        answerReasoning=row.answer_reasoning,
        sortOrder=row.sort_order,
    )


async def _create_task(
    db: AsyncSession,
    *,
    task_type: str,
    name: str,
    user_id: int,
    ref_id: int,
    payload: dict,
) -> TaskRecord:
    """⚠️ 必须 async：`db.commit()` 是协程。
    写成普通 def 的话 commit 的 coroutine 不会被 await，
    task 行**根本不会落库**，而接口照样返回 200 + 内存里的 task 对象 ——
    前端看到"运行中"，实际什么都没发生（本模块第一次跑就栽在这）。"""
    now = datetime.now()
    row = TaskRecord(
        id=new_id(),
        name=name,
        type=task_type,
        status="running",
        progress=0,
        # JSONB 列直接收 dict；手动 json.dumps 会双重编码，
        # 读回来 json.loads 一次得到的还是字符串
        payload=payload,
        cancel_requested=0,
        user_id=user_id,
        ref_id=ref_id,
        create_time=now,
        update_time=now,
        started_at=now,
    )
    db.add(row)
    await db.commit()
    return row


def _decode_payload(raw: object) -> dict:
    """TaskRecord.payload 读回来有三种形态：dict（正常）/ str（双重编码的旧数据）/ None。"""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
            if isinstance(parsed, str):
                # 双重编码：再解一层
                inner = json.loads(parsed)
                if isinstance(inner, dict):
                    return inner
        except json.JSONDecodeError:
            pass
    return {}


def _task_out(row: TaskRecord) -> dict:
    payload = _decode_payload(row.payload)
    return {
        "id": str(row.id),
        "name": row.name,
        "type": row.type,
        "status": row.status,
        "progress": row.progress,
        "message": row.message,
        "payload": payload,
        "result": row.result,
        "error": row.error,
        "userId": str(row.user_id) if row.user_id else None,
        "refId": str(row.ref_id) if row.ref_id else None,
        "createTime": row.create_time,
        "updateTime": row.update_time,
        "startedAt": row.started_at,
        "completedAt": row.completed_at,
    }


class RagEvalBenchmarkService:
    def __init__(self, db: AsyncSession, user_id: int) -> None:
        self.db = db
        self._kb = KnowledgeService(db, user_id)

    async def list_by_knowledge(self, knowledge_id: int) -> list[BenchmarkOut]:
        await self._kb.ensure_member(knowledge_id)
        stmt = (
            select(EvalRagBenchmark)
            .where(EvalRagBenchmark.knowledge_id == knowledge_id)
            .where(EvalRagBenchmark.deleted == 0)
            .order_by(EvalRagBenchmark.create_time.desc())
        )
        rows = (await self.db.execute(stmt)).scalars().all()

        outs: list[BenchmarkOut] = []
        for row in rows:
            # hasGold* 用抽样 5 条算（Java 同款"够用就好"口径：
            # 全量扫会 N+1，而指标只是给前端显示两个徽标）
            sample = (
                await self.db.execute(
                    select(EvalRagBenchmarkItem)
                    .where(EvalRagBenchmarkItem.benchmark_id == row.id)
                    .limit(5)
                )
            ).scalars().all()
            outs.append(
                _to_benchmark_out(
                    row,
                    hasGoldChunks=any(
                        i.gold_chunk_ids and i.gold_chunk_ids != "[]" for i in sample
                    ),
                    hasGoldAnswer=any(i.gold_answer and i.gold_answer.strip() for i in sample),
                )
            )
        return outs

    async def detail(
        self, knowledge_id: int, benchmark_id: int, page_num: int, page_size: int
    ) -> PageResult[BenchmarkItemOut]:
        row = await self._get(benchmark_id)
        if row is not None:
            await self._kb.ensure_member(row.knowledge_id)
        count_stmt = (
            select(func.count()).select_from(EvalRagBenchmarkItem)
            .where(EvalRagBenchmarkItem.benchmark_id == benchmark_id)
        )
        total = int((await self.db.execute(count_stmt)).scalar() or 0)
        stmt = (
            select(EvalRagBenchmarkItem)
            .where(EvalRagBenchmarkItem.benchmark_id == benchmark_id)
            .order_by(EvalRagBenchmarkItem.sort_order)
            .offset((page_num - 1) * page_size)
            .limit(page_size)
        )
        rows = (await self.db.execute(stmt)).scalars().all()
        return PageResult.of([_to_item_out(r) for r in rows], total, page_num, page_size)

    async def create_empty(
        self, knowledge_id: int, name: str, description: str | None
    ) -> EvalRagBenchmark:
        await self._kb.ensure_role(knowledge_id, "developer")
        now = datetime.now()
        row = EvalRagBenchmark(
            id=new_id(),
            knowledge_id=knowledge_id,
            name=name,
            description=description,
            question_count=0,
            status="generating",
            create_time=now,
            update_time=now,
            deleted=0,
        )
        self.db.add(row)
        await self.db.commit()
        return row

    async def start_generate(
        self,
        knowledge_id: int,
        name: str,
        description: str | None,
        count: int,
        provider_id: int | None,
        model_id: str | None,
        neighbor_count: int,
        user_id: int,
    ) -> dict:
        benchmark = await self.create_empty(knowledge_id, name, description)
        payload = {
            "benchmarkId": str(benchmark.id),
            "knowledgeId": str(knowledge_id),
            "count": count,
            "neighborCount": neighbor_count,
        }
        if provider_id:
            payload["providerId"] = str(provider_id)
        if model_id:
            payload["modelId"] = model_id
        task = await _create_task(
            self.db,
            task_type="benchmark_generate",
            name=f"AI 生成基准 - {name}",
            user_id=user_id,
            ref_id=benchmark.id,
            payload=payload,
        )
        from app.services import task_recovery

        asyncio.create_task(task_recovery.tracked(task.id, _run_benchmark_generate(task.id)))
        return _task_out(task)

    async def delete(self, knowledge_id: int, benchmark_id: int) -> None:
        await self._kb.ensure_role(knowledge_id, "developer")
        row = await self._get(benchmark_id)
        if row is None or row.knowledge_id != knowledge_id:
            raise LookupError("评估基准不存在")
        row.deleted = 1
        row.update_time = datetime.now()
        await self.db.commit()
        # 题目是物理删（Java 同：没有逻辑删除列）
        await self.db.execute(
            delete(EvalRagBenchmarkItem).where(EvalRagBenchmarkItem.benchmark_id == benchmark_id)
        )
        await self.db.commit()

    async def download_jsonl(self, benchmark_id: int) -> str:
        rows = (
            await self.db.execute(
                select(EvalRagBenchmarkItem)
                .where(EvalRagBenchmarkItem.benchmark_id == benchmark_id)
                .order_by(EvalRagBenchmarkItem.sort_order)
            )
        ).scalars().all()
        lines = []
        for r in rows:
            obj = {"query": r.query}
            if r.gold_answer:
                obj["gold_answer"] = r.gold_answer
            if r.gold_chunk_ids:
                try:
                    obj["gold_chunk_ids"] = json.loads(r.gold_chunk_ids)
                except json.JSONDecodeError:
                    obj["gold_chunk_ids"] = []
            lines.append(json.dumps(obj, ensure_ascii=False))
        return "\n".join(lines)

    async def upload_sync(
        self, knowledge_id: int, name: str, description: str | None, raw: bytes
    ) -> BenchmarkOut:
        await self._kb.ensure_role(knowledge_id, "developer")
        records, errors = engine.parse_jsonl_lines(raw)
        if not records:
            raise ValueError("；".join(errors) or "JSONL 文件内容为空")
        if errors:
            # 有坏行就整份拒绝：半份基准跑出来的指标没法解释
            raise ValueError("；".join(errors[:5]))

        now = datetime.now()
        row = EvalRagBenchmark(
            id=new_id(),
            knowledge_id=knowledge_id,
            name=name,
            description=description,
            question_count=0,
            status="ready",
            create_time=now,
            update_time=now,
            deleted=0,
        )
        self.db.add(row)
        await self.db.flush()

        items = engine.items_from_jsonl(row.id, records)
        self.db.add_all(items)
        row.question_count = len(items)
        await self.db.commit()
        return _to_benchmark_out(row)

    async def _get(self, benchmark_id: int) -> EvalRagBenchmark | None:
        return await self.db.get(EvalRagBenchmark, benchmark_id)


class RagEvalResultService:
    def __init__(self, db: AsyncSession, user_id: int) -> None:
        self.db = db
        self._kb = KnowledgeService(db, user_id)

    async def create(self, knowledge_id: int, benchmark_id: int) -> EvalRagResult:
        await self._kb.ensure_role(knowledge_id, "developer")
        benchmark = await self.db.get(EvalRagBenchmark, benchmark_id)
        if benchmark is None or benchmark.knowledge_id != knowledge_id:
            raise LookupError("评估基准不存在")
        now = datetime.now()
        row = EvalRagResult(
            id=new_id(),
            knowledge_id=knowledge_id,
            benchmark_id=benchmark.id,
            benchmark_name=benchmark.name,
            status="RUNNING",
            create_time=now,
            update_time=now,
            deleted=0,
        )
        self.db.add(row)
        await self.db.commit()
        return row

    async def start_run(
        self,
        knowledge_id: int,
        benchmark_id: int,
        answer_provider_id: int | None,
        answer_model_id: str | None,
        judge_provider_id: int | None,
        judge_model_id: str | None,
        user_id: int,
    ) -> dict:
        result = await self.create(knowledge_id, benchmark_id)
        payload: dict[str, str] = {
            "resultId": str(result.id),
            "benchmarkId": str(benchmark_id),
            "knowledgeId": str(knowledge_id),
        }
        if answer_provider_id:
            payload["answerProviderId"] = str(answer_provider_id)
        if answer_model_id:
            payload["answerModelId"] = answer_model_id
        if judge_provider_id:
            payload["judgeProviderId"] = str(judge_provider_id)
        if judge_model_id:
            payload["judgeModelId"] = judge_model_id

        task = await _create_task(
            self.db,
            task_type="rag_evaluation",
            name=f"RAG评估({result.benchmark_name})",
            user_id=user_id,
            ref_id=result.id,
            payload=payload,
        )
        from app.services import task_recovery

        asyncio.create_task(task_recovery.tracked(task.id, _run_evaluation(task.id)))
        return _task_out(task)

    async def list_by_knowledge(
        self, knowledge_id: int, page_num: int, page_size: int
    ) -> PageResult[EvalResultOut]:
        await self._kb.ensure_member(knowledge_id)
        base = (
            select(EvalRagResult)
            .where(EvalRagResult.knowledge_id == knowledge_id)
            .where(EvalRagResult.deleted == 0)
        )
        total = int(
            (await self.db.execute(select(func.count()).select_from(base.subquery()))).scalar()
            or 0
        )
        stmt = base.order_by(EvalRagResult.create_time.desc()).offset(
            (page_num - 1) * page_size
        ).limit(page_size)
        rows = (await self.db.execute(stmt)).scalars().all()
        return PageResult.of([_to_result_out(r) for r in rows], total, page_num, page_size)

    async def detail(
        self, result_id: int, page_num: int, page_size: int, error_only: bool
    ) -> PageResult[EvalResultDetailOut]:
        row = await self.db.get(EvalRagResult, result_id)
        if row is not None:
            await self._kb.ensure_member(row.knowledge_id)

        base = select(EvalRagResultDetail).where(EvalRagResultDetail.result_id == result_id)
        if error_only:
            # errorOnly = 没打分的（NULL）或判了 0 分的；
            # answer_score IS NULL 是"没配评判模型"，不是"满分"，别用 == 0 一刀切
            base = base.where(
                or_(EvalRagResultDetail.answer_score.is_(None), EvalRagResultDetail.answer_score == 0.0)
            )
        total = int(
            (await self.db.execute(select(func.count()).select_from(base.subquery()))).scalar()
            or 0
        )
        stmt = base.order_by(EvalRagResultDetail.sort_order).offset(
            (page_num - 1) * page_size
        ).limit(page_size)
        rows = (await self.db.execute(stmt)).scalars().all()
        return PageResult.of([_to_detail_out(r) for r in rows], total, page_num, page_size)

    async def delete(self, knowledge_id: int, result_id: int) -> None:
        await self._kb.ensure_role(knowledge_id, "developer")
        row = await self.db.get(EvalRagResult, result_id)
        if row is None or row.knowledge_id != knowledge_id:
            raise LookupError("评估结果不存在")
        row.deleted = 1
        row.update_time = datetime.now()
        await self.db.commit()
        await self.db.execute(
            delete(EvalRagResultDetail).where(EvalRagResultDetail.result_id == result_id)
        )
        await self.db.commit()


# ======================================================================
# 异步任务执行体（独立 session，不挂在请求事务上）
# ======================================================================
async def _task_progress(task_id: int, progress: int, message: str) -> None:
    async with SessionLocal() as db:
        await db.execute(
            update(TaskRecord)
            .where(TaskRecord.id == task_id)
            .values(progress=progress, message=message, update_time=datetime.now())
        )
        await db.commit()


async def _task_finish(task_id: int, status: str, result_text: str | None, error: str | None) -> None:
    now = datetime.now()
    async with SessionLocal() as db:
        await db.execute(
            update(TaskRecord)
            .where(TaskRecord.id == task_id)
            .values(
                status=status,
                result=result_text,
                error=error,
                progress=100 if status == "success" else None,
                completed_at=now,
                update_time=now,
            )
        )
        await db.commit()


def _is_cancelled(row: TaskRecord) -> bool:
    return bool(row.cancel_requested)


async def _run_benchmark_generate(task_id: int) -> None:
    """AI 生成基准题目。独立连接跑，进度写 task 表。"""
    async with SessionLocal() as db:
        task = (await db.execute(select(TaskRecord).where(TaskRecord.id == task_id))).scalars().first()
        if task is None:
            return
        payload = _decode_payload(task.payload)
        benchmark_id = int(payload["benchmarkId"])
        knowledge_id = int(payload["knowledgeId"])
        count = int(payload.get("count") or 10)
        neighbor = int(payload.get("neighborCount") or 3)
        provider_id = int(payload["providerId"]) if payload.get("providerId") else None
        model_id = payload.get("modelId")

    try:
        await _task_progress(task_id, 10, "正在收集知识库分块...")

        async with SessionLocal() as db:
            def progress_cb(p: int) -> None:
                pass  # 引擎内是同步循环，进度由下面按批次写

            items = await engine.generate_benchmark_items(
                db, knowledge_id, count, provider_id, model_id, neighbor,
                progress_cb=None,
            )
        if not items:
            raise RuntimeError("基准题目生成失败：未能生成任何有效题目")

        await _task_progress(task_id, 90, f"已生成 {len(items)} 题，正在保存...")

        async with SessionLocal() as db:
            for i, item in enumerate(items):
                item.benchmark_id = benchmark_id
                item.sort_order = i
                db.add(item)
            benchmark = await db.get(EvalRagBenchmark, benchmark_id)
            if benchmark is not None:
                benchmark.question_count = len(items)
                benchmark.status = "ready"
                benchmark.update_time = datetime.now()
            await db.commit()

        # 任务被取消：基准标记 failed
        async with SessionLocal() as db:
            row = (await db.execute(select(TaskRecord).where(TaskRecord.id == task_id))).scalars().first()
            if row and _is_cancelled(row):
                await db.execute(
                    update(EvalRagBenchmark)
                    .where(EvalRagBenchmark.id == benchmark_id)
                    .values(status="failed", update_time=datetime.now())
                )
                await db.commit()
                await _task_finish(task_id, "cancelled", None, "用户取消")
                return

        await _task_finish(task_id, "success", f"生成 {len(items)} 题", None)
    except Exception as exc:  # noqa: BLE001
        logger.error("[RagEval] 基准生成失败 task=%s: %s", task_id, exc)
        async with SessionLocal() as db:
            await db.execute(
                update(EvalRagBenchmark)
                .where(EvalRagBenchmark.id == benchmark_id)
                .values(status="failed", update_time=datetime.now())
            )
            await db.commit()
        await _task_finish(task_id, "failed", None, str(exc))


async def _run_evaluation(task_id: int) -> None:
    """逐题评估：加载基准 → 每题一次独立事务 → 聚合 → AI 分析。"""
    async with SessionLocal() as db:
        task = (await db.execute(select(TaskRecord).where(TaskRecord.id == task_id))).scalars().first()
        if task is None:
            return
        payload = _decode_payload(task.payload)
        result_id = int(payload["resultId"])
        benchmark_id = int(payload["benchmarkId"])
        knowledge_id = int(payload["knowledgeId"])
        answer_provider_id = int(payload["answerProviderId"]) if payload.get("answerProviderId") else None
        answer_model_id = payload.get("answerModelId")
        judge_provider_id = int(payload["judgeProviderId"]) if payload.get("judgeProviderId") else None
        judge_model_id = payload.get("judgeModelId")

    start = datetime.now()

    async def _fail(err: str) -> None:
        async with SessionLocal() as db:
            await db.execute(
                update(EvalRagResult)
                .where(EvalRagResult.id == result_id)
                .values(
                    status="FAILED", error=err,
                    duration_ms=int((datetime.now() - start).total_seconds() * 1000),
                    update_time=datetime.now(),
                )
            )
            await db.commit()
        await _task_finish(task_id, "failed", None, err)

    try:
        async with SessionLocal() as db:
            items = (
                await db.execute(
                    select(EvalRagBenchmarkItem)
                    .where(EvalRagBenchmarkItem.benchmark_id == benchmark_id)
                    .order_by(EvalRagBenchmarkItem.sort_order)
                )
            ).scalars().all()
        if not items:
            await _fail("基准题目为空")
            return

        for i, item in enumerate(items):
            async with SessionLocal() as db:
                task_row = (await db.execute(
                    select(TaskRecord).where(TaskRecord.id == task_id)
                )).scalars().first()
                if task_row and _is_cancelled(task_row):
                    await _fail("用户取消")
                    return
            try:
                async with SessionLocal() as db:
                    detail = await engine.evaluate_question(
                        db, item, knowledge_id,
                        answer_provider_id, answer_model_id,
                        judge_provider_id, judge_model_id,
                    )
                    detail.result_id = result_id
                    detail.sort_order = i
                    db.add(detail)
                    await db.commit()
            except BudgetExceeded:
                # 预算超限整场终止（Java checkBudget 抛 BizException 同语义）：
                # 继续跑只会每题都空转到闸上，白打一条错误明细
                raise
            except Exception as exc:  # noqa: BLE001
                # 单题失败记一条错误明细，继续跑 —— 这是批处理，不是事务
                logger.error("[RagEval] 题目评估失败 %s: %s", item.query[:50], exc)
                async with SessionLocal() as db:
                    db.add(
                        EvalRagResultDetail(
                            id=new_id(),
                            result_id=result_id,
                            query=item.query,
                            gold_chunk_ids=item.gold_chunk_ids,
                            gold_answer=item.gold_answer,
                            generated_answer=f"评估失败: {exc}",
                            sort_order=i,
                        )
                    )
                    await db.commit()
            await _task_progress(
                task_id, 10 + int((i + 1) / len(items) * 70),
                f"正在评估 ({i + 1}/{len(items)})...",
            )

        # 聚合
        async with SessionLocal() as db:
            details = (
                await db.execute(
                    select(EvalRagResultDetail)
                    .where(EvalRagResultDetail.result_id == result_id)
                    .order_by(EvalRagResultDetail.sort_order)
                )
            ).scalars().all()
            metrics = engine.aggregate_metrics(list(details))
            result = await db.get(EvalRagResult, result_id)
            if result is None:
                return
            result.status = "COMPLETED"
            result.duration_ms = int((datetime.now() - start).total_seconds() * 1000)
            result.overall_score = metrics.get("overallScore", 0.0)
            if "retrieval" in metrics:
                result.retrieval_json = json.dumps(metrics["retrieval"], ensure_ascii=False)
            if "answer" in metrics:
                result.answer_json = json.dumps(metrics["answer"], ensure_ascii=False)
            result.config_json = json.dumps(
                {
                    "answerProviderId": str(answer_provider_id) if answer_provider_id else "null",
                    "answerModelId": answer_model_id or "",
                    "judgeProviderId": str(judge_provider_id) if judge_provider_id else "null",
                    "judgeModelId": judge_model_id or "",
                },
                ensure_ascii=False,
            )
            await db.commit()

        # AI 分析报告（失败不影响 COMPLETED）
        try:
            async with SessionLocal() as db:
                result = await db.get(EvalRagResult, result_id)
                details = (
                    await db.execute(
                        select(EvalRagResultDetail)
                        .where(EvalRagResultDetail.result_id == result_id)
                        .order_by(EvalRagResultDetail.sort_order)
                    )
                ).scalars().all()
                metrics = engine.aggregate_metrics(list(details))
                analysis_provider = judge_provider_id
                report = await engine.generate_analysis(
                    result, list(details), metrics, analysis_provider
                )
                if report:
                    result.analysis = report
                    await db.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[RagEval] AI 分析生成失败: %s", exc)

        await _task_progress(task_id, 100, "评估完成")
        await _task_finish(task_id, "success", f"RAG评估完成, resultId={result_id}", None)
    except Exception as exc:  # noqa: BLE001
        logger.error("[RagEval] 评估任务失败 task=%s: %s", task_id, exc)
        await _fail(str(exc))
