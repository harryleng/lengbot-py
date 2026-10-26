"""评测模块的 DTO（评测集 / 评估器 / 实验）。

对照 Java：`EvalDatasetCreateDTO`、`EvalDatasetItemCreateDTO`、`EvalDatasetVersionCreateDTO`、
`EvalEvaluatorCreateDTO`、`EvalEvaluatorVersionCreateDTO`、`EvalExperimentCreateDTO`。

**这组接口的出参有个统一规律**：所有 jsonb 列（`columnsConfig` / `dataContent` /
`datasetItems` / `variables` / `modelConfig` / `evaluationObjectConfig` /
`evaluatorConfig`）**出参都是 JSON 字符串**。

原因：Java 实体用 `JsonbTypeHandler` 把 jsonb 当 String 读写，Jackson 就序列化成字符串了。
前端也按字符串解析（`ExperimentDetail.vue` 里直接 `JSON.parse(exp.evaluationObjectConfig)`
—— 给对象会 `TypeError: JSON.parse: unexpected character`）。

**入参两种都要接受**：前端保存时 `JSON.stringify` 后提交（字符串），
但脚本/测试更愿意直接传对象。所以 write 侧用 `parse_json_value` 兼容两者。

`modelConfig` 这个字段名在 Pydantic v2 里是保留名 —— 用 `model_config_json` +
`validation_alias=AliasChoices(...)`（和 `schemas/prompt.py` 同样的处理，那里踩过坑：
只给 alias 会导致 `from_attributes` 静默取默认值）。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import AliasChoices, Field, field_validator

from app.schemas.common import CamelModel
from app.schemas.prompt import parse_json_value, to_json_text

# 单个实验最多挂几个评估器（对齐 Java `MAX_EVALUATORS`）
MAX_EVALUATORS = 5


# ======================================================================
# 评测集
# ======================================================================
class EvalDatasetIn(CamelModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: str | None = Field(None, max_length=500)
    # 入参可能是字符串也可能是数组
    columns_config: Any | None = None


class EvalDatasetOut(CamelModel):
    id: str
    name: str
    description: str | None = None
    columns_config: str = "[]"
    user_id: str | None = None
    create_time: datetime | None = None
    update_time: datetime | None = None

    @field_validator("id", "user_id", mode="before")
    @classmethod
    def _id_to_str(cls, v: Any) -> str | None:
        return None if v is None else str(v)

    @field_validator("columns_config", mode="before")
    @classmethod
    def _jsonb_to_text(cls, v: Any) -> str:
        # 列表用 `[]` 兜底（`JSON.parse('{}')` 也能跑，但语义不对）
        if v is None:
            return "[]"
        return to_json_text(v)


class EvalDatasetItemIn(CamelModel):
    dataset_id: str | None = None
    # 单条时是对象/字符串；批量时读 data_contents
    data_content: Any | None = None
    data_contents: list[Any] | None = None

    @field_validator("dataset_id", mode="before")
    @classmethod
    def _id_to_str(cls, v: Any) -> str | None:
        return None if v is None else str(v)


class EvalDatasetItemOut(CamelModel):
    id: str
    dataset_id: str
    data_content: str = "{}"
    create_time: datetime | None = None
    update_time: datetime | None = None

    @field_validator("id", "dataset_id", mode="before")
    @classmethod
    def _id_to_str(cls, v: Any) -> str | None:
        return None if v is None else str(v)

    @field_validator("data_content", mode="before")
    @classmethod
    def _jsonb_to_text(cls, v: Any) -> str:
        if v is None:
            return "{}"
        return to_json_text(v)


class EvalDatasetVersionIn(CamelModel):
    dataset_id: str
    version: str = Field(..., min_length=1, max_length=50)

    @field_validator("dataset_id", mode="before")
    @classmethod
    def _id_to_str(cls, v: Any) -> str:
        return str(v)


class EvalDatasetVersionOut(CamelModel):
    id: str
    dataset_id: str
    version: str
    data_count: int = 0
    status: str = "draft"
    # 数据项 **id 数组**（不是内容）
    dataset_items: str = "[]"
    create_time: datetime | None = None
    update_time: datetime | None = None

    @field_validator("id", "dataset_id", mode="before")
    @classmethod
    def _id_to_str(cls, v: Any) -> str | None:
        return None if v is None else str(v)

    @field_validator("dataset_items", mode="before")
    @classmethod
    def _jsonb_to_text(cls, v: Any) -> str:
        if v is None:
            return "[]"
        # id 数组里的元素也要转字符串 —— 19 位雪花在 JS 里会失真
        if isinstance(v, list):
            return to_json_text([str(x) for x in v])
        return to_json_text(v)


# ======================================================================
# 评估器
# ======================================================================
class EvalEvaluatorIn(CamelModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: str | None = Field(None, max_length=500)
    # 逗号分隔的字符串（对齐 Java String）
    tags: str | None = Field(None, max_length=500)
    # 大部分接口不用，但前端编辑表单会整体回传
    id: str | None = None

    @field_validator("id", mode="before")
    @classmethod
    def _id_to_str(cls, v: Any) -> str | None:
        return None if v is None else str(v)


class EvalEvaluatorOut(CamelModel):
    id: str
    name: str
    description: str | None = None
    tags: str | None = None
    user_id: str | None = None
    create_time: datetime | None = None
    update_time: datetime | None = None

    @field_validator("id", "user_id", mode="before")
    @classmethod
    def _id_to_str(cls, v: Any) -> str | None:
        return None if v is None else str(v)


class EvalEvaluatorVersionIn(CamelModel):
    evaluator_id: str
    version: str = Field(..., min_length=1, max_length=50)
    prompt: str = Field(..., min_length=1)
    variables: Any | None = None
    # Pydantic v2 保留名，走 alias
    model_config_json: Any | None = Field(
        None,
        alias="modelConfig",
        validation_alias=AliasChoices("modelConfig", "model_config"),
    )

    @field_validator("evaluator_id", mode="before")
    @classmethod
    def _id_to_str(cls, v: Any) -> str:
        return str(v)


class EvalEvaluatorVersionOut(CamelModel):
    id: str
    evaluator_id: str
    version: str
    prompt: str | None = None
    variables: str = "[]"
    model_config_json: str = Field(
        "{}",
        alias="modelConfig",
        validation_alias=AliasChoices("modelConfig", "model_config"),
    )
    status: str = "draft"
    create_time: datetime | None = None
    update_time: datetime | None = None

    @field_validator("id", "evaluator_id", mode="before")
    @classmethod
    def _id_to_str(cls, v: Any) -> str | None:
        return None if v is None else str(v)

    @field_validator("variables", mode="before")
    @classmethod
    def _vars_to_text(cls, v: Any) -> str:
        if v is None:
            return "[]"
        return to_json_text(v)

    @field_validator("model_config_json", mode="before")
    @classmethod
    def _cfg_to_text(cls, v: Any) -> str:
        if v is None:
            return "{}"
        return to_json_text(v)


class EvalEvaluatorTemplateOut(CamelModel):
    id: str
    evaluator_template_key: str
    template_desc: str | None = None
    template: str | None = None
    variables: str | None = None
    model_config_json: str = Field(
        "{}",
        alias="modelConfig",
        validation_alias=AliasChoices("modelConfig", "model_config"),
    )

    @field_validator("id", mode="before")
    @classmethod
    def _id_to_str(cls, v: Any) -> str:
        return str(v)

    @field_validator("model_config_json", mode="before")
    @classmethod
    def _cfg_to_text(cls, v: Any) -> str:
        if v is None:
            return "{}"
        return to_json_text(v)


class EvalEvaluatorTestIn(CamelModel):
    evaluator_version_id: str
    # `{变量名: 值}` 的字符串（Java 也是 String）
    variables: str | None = None

    @field_validator("evaluator_version_id", mode="before")
    @classmethod
    def _id_to_str(cls, v: Any) -> str:
        return str(v)


class EvalScoreOut(CamelModel):
    score: float = 0.0
    reason: str | None = None

    @field_validator("score", mode="before")
    @classmethod
    def _to_float(cls, v: Any) -> float:
        if v is None:
            return 0.0
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0


# ======================================================================
# 实验
# ======================================================================
class EvalExperimentIn(CamelModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: str | None = Field(None, max_length=500)
    dataset_id: str | None = None
    dataset_version_id: str | None = None
    dataset_version: str | None = None
    evaluation_object_config: Any | None = None
    evaluator_config: Any | None = None

    @field_validator("dataset_id", "dataset_version_id", mode="before")
    @classmethod
    def _id_to_str(cls, v: Any) -> str | None:
        return None if v is None else str(v)


class EvalExperimentOut(CamelModel):
    id: str
    name: str
    description: str | None = None
    dataset_id: str | None = None
    dataset_version_id: str | None = None
    dataset_version: str | None = None
    # 出参是 JSON 字符串（前端 JSON.parse）
    evaluation_object_config: str = "{}"
    evaluator_config: str = "[]"
    status: str = "draft"
    progress: int = 0
    complete_time: datetime | None = None
    task_id: str | None = None
    create_time: datetime | None = None
    update_time: datetime | None = None

    # ---- 联表填出来的展示字段（Java 用 @TableField(exist=false)） ----
    dataset_name: str | None = None
    prompt_key: str | None = None
    prompt_version: str | None = None
    evaluator_name: str | None = None
    evaluator_version: str | None = None
    evaluator_name_list: list[str] = Field(default_factory=list)
    evaluator_version_list: list[str] = Field(default_factory=list)
    evaluator_id_list: list[str] = Field(default_factory=list)

    @field_validator(
        "id", "dataset_id", "dataset_version_id", "task_id", mode="before"
    )
    @classmethod
    def _id_to_str(cls, v: Any) -> str | None:
        return None if v is None else str(v)

    @field_validator("evaluation_object_config", mode="before")
    @classmethod
    def _obj_to_text(cls, v: Any) -> str:
        if v is None:
            return "{}"
        return to_json_text(v)

    @field_validator("evaluator_config", mode="before")
    @classmethod
    def _eval_to_text(cls, v: Any) -> str:
        if v is None:
            return "[]"
        return to_json_text(v)


class EvalExperimentResultOut(CamelModel):
    id: str
    experiment_id: str
    input: str | None = None
    actual_output: str | None = None
    reference_output: str | None = None
    # numeric → float。给 Decimal 前端拿到的会是字符串，图表画不出来
    score: float | None = None
    reason: str | None = None
    evaluator_version_id: str | None = None
    evaluator_name: str | None = None
    evaluation_time: datetime | None = None
    create_time: datetime | None = None
    update_time: datetime | None = None

    @field_validator(
        "id", "experiment_id", "evaluator_version_id", mode="before"
    )
    @classmethod
    def _id_to_str(cls, v: Any) -> str | None:
        return None if v is None else str(v)

    @field_validator("score", mode="before")
    @classmethod
    def _to_float(cls, v: Any) -> float | None:
        if v is None:
            return None
        if isinstance(v, Decimal):
            return float(v)
        try:
            return float(v)
        except (TypeError, ValueError):
            return None


class EvalExperimentOverviewOut(CamelModel):
    """每个评估器一行的汇总（平均分 + 已评条数）。"""

    evaluator_version_id: str
    evaluator_name: str | None = None
    evaluator_version: str | None = None
    avg_score: float = 0.0
    evaluated_count: int = 0
    total_count: int = 0

    @field_validator("evaluator_version_id", mode="before")
    @classmethod
    def _id_to_str(cls, v: Any) -> str:
        return str(v)

    @field_validator("avg_score", mode="before")
    @classmethod
    def _to_float(cls, v: Any) -> float:
        if v is None:
            return 0.0
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0


__all__ = [
    "MAX_EVALUATORS",
    "EvalDatasetIn",
    "EvalDatasetItemIn",
    "EvalDatasetItemOut",
    "EvalDatasetOut",
    "EvalDatasetVersionIn",
    "EvalDatasetVersionOut",
    "EvalEvaluatorIn",
    "EvalEvaluatorOut",
    "EvalEvaluatorTemplateOut",
    "EvalEvaluatorTestIn",
    "EvalEvaluatorVersionIn",
    "EvalEvaluatorVersionOut",
    "EvalExperimentIn",
    "EvalExperimentOut",
    "EvalExperimentOverviewOut",
    "EvalExperimentResultOut",
    "EvalScoreOut",
    "parse_json_value",
]
