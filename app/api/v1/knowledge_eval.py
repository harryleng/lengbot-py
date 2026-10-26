"""知识库 RAG 评估（对照 Java `KnowledgeEvalController`，10 个接口）。

出参口径：
- `generate` / `run` 返回 **Task 记录**（不是 benchmark/result 本身），
  前端拿 taskId 去任务中心轮询进度；
- download 直接回 JSONL 字节流（不是 Result 包装），前端 `responseType: 'blob'`。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response

from app.api.deps import CurrentUserId, DbSession
from app.schemas.common import Result
from app.schemas.knowledge_eval import (
    BenchmarkGenerateIn,
    BenchmarkItemOut,
    BenchmarkOut,
    EvalResultDetailOut,
    EvalResultOut,
    EvalRunIn,
)
from app.services.rag_eval_service import RagEvalBenchmarkService, RagEvalResultService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/knowledge", tags=["knowledge-eval"])

# Java：小文件（<=2MB）同步导入，>2MB 走异步任务
ASYNC_THRESHOLD = 2 * 1024 * 1024
MAX_UPLOAD = 10 * 1024 * 1024


def _not_found(exc: LookupError) -> HTTPException:
    return HTTPException(status_code=404, detail=str(exc))


def _bad(exc: ValueError) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


# ==================== 评估基准 ====================
@router.get(
    "/{knowledge_id}/eval/benchmarks",
    response_model=Result[list[BenchmarkOut]],
    summary="获取评估基准列表",
)
async def list_benchmarks(
    db: DbSession, user_id: CurrentUserId, knowledge_id: int
) -> Result[list[BenchmarkOut]]:
    return Result.ok(await RagEvalBenchmarkService(db, user_id).list_by_knowledge(knowledge_id))


@router.get(
    "/{knowledge_id}/eval/benchmarks/{benchmark_id}",
    response_model=Result[list[BenchmarkItemOut]],
    summary="获取评估基准详情（分页题目）",
)
async def benchmark_detail(
    db: DbSession,
    user_id: CurrentUserId,
    knowledge_id: int,
    benchmark_id: int,
    page_num: int = Query(default=1, alias="pageNum", ge=1),
    page_size: int = Query(default=20, alias="pageSize", ge=1, le=200),
) -> Result[list[BenchmarkItemOut]]:
    page = await RagEvalBenchmarkService(db, user_id).detail(
        knowledge_id, benchmark_id, page_num, page_size
    )
    return Result.ok(page.records)


@router.post(
    "/{knowledge_id}/eval/benchmarks/generate",
    response_model=Result[dict],
    summary="AI 自动生成评估基准（异步任务）",
)
async def generate_benchmark(
    dto: BenchmarkGenerateIn,
    db: DbSession,
    user_id: CurrentUserId,
    knowledge_id: int,
) -> Result[dict]:
    svc = RagEvalBenchmarkService(db, user_id)
    try:
        task = await svc.start_generate(
            knowledge_id=knowledge_id,
            name=dto.name,
            description=dto.description,
            count=dto.count,
            provider_id=int(dto.provider_id) if dto.provider_id else None,
            model_id=dto.model_id,
            neighbor_count=dto.neighbor_count or 3,
            user_id=user_id,
        )
    except LookupError as exc:
        raise _not_found(exc) from exc
    except ValueError as exc:
        raise _bad(exc) from exc
    return Result.ok(task)


@router.post(
    "/{knowledge_id}/eval/benchmarks/upload",
    response_model=Result[dict | BenchmarkOut],
    summary="上传 JSONL 评估基准",
)
async def upload_benchmark(
    db: DbSession,
    user_id: CurrentUserId,
    knowledge_id: int,
    name: str = Form(...),
    description: str | None = Form(default=None),
    file: UploadFile = File(...),
) -> Result[dict | BenchmarkOut]:
    if not file.filename:
        raise HTTPException(status_code=400, detail="仅支持 .jsonl 或 .json 格式文件")
    if not (file.filename.endswith(".jsonl") or file.filename.endswith(".json")):
        raise HTTPException(status_code=400, detail="仅支持 .jsonl 或 .json 格式文件")

    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="上传文件不能为空")
    if len(raw) > MAX_UPLOAD:
        raise HTTPException(status_code=400, detail="文件大小超过限制（最大 10MB）")

    svc = RagEvalBenchmarkService(db, user_id)
    try:
        # Java >2MB 走异步任务（临时文件 + BENCHMARK_IMPORT 任务）。
        # Python 版 2MB 以上的 JSONL 意味着上万行，同步解析也就几秒，
        # 单独做一条异步导入链路的收益抵不上多一份任务状态机 —— 先同步。
        benchmark = await svc.upload_sync(knowledge_id, name, description, raw)
    except ValueError as exc:
        raise _bad(exc) from exc
    except LookupError as exc:
        raise _not_found(exc) from exc
    return Result.ok(benchmark)


@router.delete(
    "/{knowledge_id}/eval/benchmarks/{benchmark_id}",
    response_model=Result[None],
    summary="删除评估基准",
)
async def delete_benchmark(
    db: DbSession, user_id: CurrentUserId, knowledge_id: int, benchmark_id: int
) -> Result[None]:
    try:
        await RagEvalBenchmarkService(db, user_id).delete(knowledge_id, benchmark_id)
    except LookupError as exc:
        raise _not_found(exc) from exc
    return Result.ok(None)


@router.get(
    "/{knowledge_id}/eval/benchmarks/{benchmark_id}/download",
    summary="下载评估基准为 JSONL",
)
async def download_benchmark(
    db: DbSession, user_id: CurrentUserId, knowledge_id: int, benchmark_id: int
) -> Response:
    svc = RagEvalBenchmarkService(db, user_id)
    # 权限：能看详情才能下载（走同一个 member 校验）
    await svc.detail(knowledge_id, benchmark_id, 1, 1)
    try:
        jsonl = await svc.download_jsonl(benchmark_id)
    except LookupError as exc:
        raise _not_found(exc) from exc
    return Response(
        content=jsonl.encode("utf-8"),
        media_type="application/octet-stream",
        headers={"Content-Disposition": "attachment; filename=benchmark.jsonl"},
    )


# ==================== 评估结果 ====================
@router.post(
    "/{knowledge_id}/eval/run",
    response_model=Result[dict],
    summary="运行 RAG 评估（异步任务）",
)
async def run_evaluation(
    dto: EvalRunIn,
    db: DbSession,
    user_id: CurrentUserId,
    knowledge_id: int,
) -> Result[dict]:
    svc = RagEvalResultService(db, user_id)
    try:
        task = await svc.start_run(
            knowledge_id=knowledge_id,
            benchmark_id=int(dto.benchmark_id),
            answer_provider_id=(
                int(dto.answer_provider_id) if dto.answer_provider_id else None
            ),
            answer_model_id=dto.answer_model_id,
            judge_provider_id=int(dto.judge_provider_id) if dto.judge_provider_id else None,
            judge_model_id=dto.judge_model_id,
            user_id=user_id,
        )
    except LookupError as exc:
        raise _not_found(exc) from exc
    except ValueError as exc:
        raise _bad(exc) from exc
    return Result.ok(task)


@router.get(
    "/{knowledge_id}/eval/results",
    response_model=Result[list[EvalResultOut]],
    summary="获取评估历史",
)
async def list_results(
    db: DbSession,
    user_id: CurrentUserId,
    knowledge_id: int,
    page_num: int = Query(default=1, alias="pageNum", ge=1),
    page_size: int = Query(default=20, alias="pageSize", ge=1, le=200),
) -> Result[list[EvalResultOut]]:
    page = await RagEvalResultService(db, user_id).list_by_knowledge(
        knowledge_id, page_num, page_size
    )
    return Result.ok(page.records)


@router.get(
    "/{knowledge_id}/eval/results/{result_id}",
    response_model=Result[list[EvalResultDetailOut]],
    summary="获取评估结果详情（分页明细）",
)
async def result_detail(
    db: DbSession,
    user_id: CurrentUserId,
    knowledge_id: int,
    result_id: int,
    page_num: int = Query(default=1, alias="pageNum", ge=1),
    page_size: int = Query(default=20, alias="pageSize", ge=1, le=200),
    error_only: bool = Query(default=False, alias="errorOnly"),
) -> Result[list[EvalResultDetailOut]]:
    page = await RagEvalResultService(db, user_id).detail(
        result_id, page_num, page_size, error_only
    )
    return Result.ok(page.records)


@router.delete(
    "/{knowledge_id}/eval/results/{result_id}",
    response_model=Result[None],
    summary="删除评估结果",
)
async def delete_result(
    db: DbSession, user_id: CurrentUserId, knowledge_id: int, result_id: int
) -> Result[None]:
    try:
        await RagEvalResultService(db, user_id).delete(knowledge_id, result_id)
    except LookupError as exc:
        raise _not_found(exc) from exc
    return Result.ok(None)
