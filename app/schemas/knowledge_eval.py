"""知识库 RAG 评估的出参/入参。

对照 Java `KnowledgeEvalController`：
- 列表出参就是实体本身（MyBatis-Plus 实体直出），camelCase；
- 分页是 MP `Page<T>` 的形状（records/total/size/current/pages）；
- `run` / `generate` 返回的是 **Task 记录**（前端拿 taskId 去任务中心看进度）。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from app.schemas.common import CamelModel, PageResult


# ---------------- 基准 ----------------
class BenchmarkOut(CamelModel):
    id: str
    knowledge_id: str
    name: str | None = None
    description: str | None = None
    question_count: int = 0
    status: str | None = None
    # 列表页才算（抽样 5 条），详情接口不给
    has_gold_chunks: bool | None = None
    has_gold_answer: bool | None = None
    create_time: datetime | None = None
    update_time: datetime | None = None


class BenchmarkItemOut(CamelModel):
    id: str
    benchmark_id: str
    query: str | None = None
    # 库里是 JSON 数组字符串，出参保持字符串（前端 JSON.parse）
    gold_chunk_ids: str | None = None
    gold_answer: str | None = None
    sort_order: int = 0


class BenchmarkGenerateIn(BaseModel):
    """前端发 camelCase（providerId / modelId / neighborCount）。"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    name: str = Field(min_length=1, max_length=128)
    description: str | None = None
    count: int = Field(default=10, ge=1, le=50)
    provider_id: str | None = None
    model_id: str | None = None
    neighbor_count: int | None = Field(default=None, ge=1, le=10)


# ---------------- 评估结果 ----------------
class EvalRunIn(BaseModel):
    """前端发 camelCase（benchmarkId / answerProviderId / answerModelId /
    judgeProviderId / judgeModelId）。缺 alias 时字段被静默忽略，
    表现是「接口 200 但一直在跑空跑」。"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    benchmark_id: str
    answer_provider_id: str | None = None
    answer_model_id: str | None = None
    judge_provider_id: str | None = None
    judge_model_id: str | None = None


class EvalResultOut(CamelModel):
    id: str
    knowledge_id: str
    benchmark_id: str
    benchmark_name: str | None = None
    status: str | None = None
    overall_score: float | None = None
    # 库里是 JSON 文本，出参保持字符串；前端会 JSON.parse
    retrieval_json: str | None = None
    answer_json: str | None = None
    config_json: str | None = None
    duration_ms: int | None = None
    analysis: str | None = None
    error: str | None = None
    create_time: datetime | None = None
    update_time: datetime | None = None


class EvalResultDetailOut(CamelModel):
    id: str
    result_id: str
    query: str | None = None
    gold_chunk_ids: str | None = None
    gold_answer: str | None = None
    generated_answer: str | None = None
    retrieved_chunk_ids: str | None = None
    retrieval_scores: str | None = None
    answer_score: float | None = None
    answer_reasoning: str | None = None
    sort_order: int = 0


class TaskOut(CamelModel):
    """`generate` / `run` 返回的任务记录（对齐 Java `Task` 实体直出）。"""

    id: str
    name: str | None = None
    type: str | None = None
    status: str | None = None
    progress: int | None = None
    message: str | None = None
    payload: str | dict | None = None
    result: str | None = None
    error: str | None = None
    user_id: str | None = None
    ref_id: str | None = None
    create_time: datetime | None = None
    update_time: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None


# 类型别名：让 response_model 声明短一点
BenchmarkPage = PageResult[BenchmarkItemOut]
ResultPage = PageResult[EvalResultOut]
DetailPage = PageResult[EvalResultDetailOut]
