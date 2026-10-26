"""RAG 评估引擎：检索指标 + LLM 生成/评判（对照 Java `RagEvaluationEngine`）。

业务层（基准/结果/任务调度）在 `rag_eval_service.py`。

## 聚合口径（Java 注释明说"设计如此"）

- `retrieval` = 各题 recall@K / f1@K 的均值（**不含 precision**）；
- `overallScore` = 上面 8 个检索指标的均值，**不含 answer.accuracy** ——
  综合分偏"检索质量"，这是有意设计，别"修好"它。

## 限流

每题最多 3 次 LLM 调用（生成/答题/评判），Java 里 `LLM_CALL_DELAY_MS=1000`
每次调用后睡 1 秒防 429。Python 用 `asyncio.sleep` 等价实现。
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.snowflake import new_id
from app.models.entities import (
    Chunk,
    EvalRagBenchmarkItem,
    EvalRagResult,
    EvalRagResultDetail,
)
from app.services.embedding_client import embed_query, to_pgvector

logger = logging.getLogger(__name__)

K_VALUES = (1, 3, 5, 10)
LLM_CALL_DELAY_MS = 1000

# 评估检索：Java `embeddingService.searchSimilarSql(knowledgeId, vec, 10, 0.0)`
# 不带 document.deleted 过滤（评估要的是纯向量召回视角），与检索测试接口不同
EVAL_SEARCH_SQL = """
    SELECT c.id   AS chunk_id,
           c.content AS content,
           1 - (e.vector <=> CAST(:vec AS vector)) AS score
    FROM embedding e
    JOIN chunk c ON c.id = e.chunk_id
    WHERE e.knowledge_id = :kb_id AND c.status = 'vectorized'
    ORDER BY e.vector <=> CAST(:vec AS vector)
    LIMIT :top_k
"""

BENCHMARK_GEN_PROMPT = """你将基于以下上下文生成一个可由上下文准确回答的问题与标准答案。
要求：
1. 问题具体明确，控制在30字以内
2. 答案精准简洁，控制在80字以内，直接回答核心要点
3. 仅返回一个JSON对象，不要包含其他文字
4. 键为 query、gold_answer、gold_chunk_ids。gold_chunk_ids 必须是上述上下文片段的ID子集

上下文：
%s
"""

JUDGE_PROMPT = """你是一个公正的评判者，请评估AI生成的答案相对于标准答案的准确性。

问题：%s

标准答案：
%s

AI生成的答案：
%s

评分标准（0.0 ~ 1.0 连续分值）：
- 1.0：核心事实完全正确，与标准答案一致
- 0.8 ~ 0.9：核心事实正确，有少量非关键信息遗漏或多余
- 0.5 ~ 0.7：部分核心事实正确，但有明显遗漏或部分错误
- 0.2 ~ 0.4：仅少部分事实正确，大部分关键信息缺失或错误
- 0.0：完全错误、无关或与标准答案矛盾

评判规则：
1. 忽略措辞、标点符号、格式上的差异
2. 只关注核心事实是否准确
3. 如果AI回答"无法回答"或类似表述，给 0.0

请返回以下JSON格式的结果（不要包含其他文本、Markdown 或注释）：
{"score": 0.85, "reasoning": "用一两句话简要说明判定理由，不超过50字"}
"""

ANSWER_GEN_PROMPT = """基于以下参考文档回答用户问题。要求精准简洁，直接回答核心要点，
控制在100字以内，不要展开解释。如果文档中没有相关信息，请回答"根据已有信息无法回答该问题"。

参考文档：
%s

用户问题：%s
"""

ANALYSIS_SYSTEM_PROMPT = (
    "你是一个RAG系统评估分析专家。请根据评估结果数据，给出简洁的分析报告。\n"
    "要求：\n"
    "1. 总体评价（1-2句话）\n"
    "2. 检索质量分析（如果检索指标偏低，指出可能原因）\n"
    "3. 答案质量分析（如果答案准确率偏低，指出常见错误模式）\n"
    "4. 改进建议（2-3条具体可操作的建议）\n"
    "回复使用中文，控制在300字以内。"
)

_THINK_RE = re.compile(r"<think>.*?</think>|<thinking>.*?</thinking>", re.S | re.I)


def extract_json(text_value: str | None) -> str | None:
    """从 LLM 响应里抠 JSON：剥 `<think>` 块和 markdown 代码围栏。

    推理模型（DeepSeek-R1 / MiniMax-M3）会先吐一大段思考再给结论，
    不剥掉 json.loads 必炸。
    """
    if text_value is None:
        return None
    cleaned = _THINK_RE.sub("", text_value).strip()
    if cleaned.startswith("```"):
        first_nl = cleaned.find("\n")
        last_fence = cleaned.rfind("```")
        if first_nl > 0 and last_fence > first_nl:
            return cleaned[first_nl + 1 : last_fence].strip()
    return cleaned


def parse_json_string_list(raw: str | None) -> list[str]:
    if not raw or not raw.strip():
        return []
    try:
        parsed = json.loads(raw)
        return [str(x) for x in parsed] if isinstance(parsed, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


# ---------------- 检索指标 ----------------
def precision_at_k(retrieved: list[str], relevant: list[str], k: int) -> float:
    if not retrieved or k <= 0:
        return 0.0
    top = set(retrieved[:k])
    rel = set(relevant)
    return len(top & rel) / k


def recall_at_k(retrieved: list[str], relevant: list[str], k: int) -> float:
    if not relevant:
        return 0.0
    top = set(retrieved[:k])
    rel = set(relevant)
    return len(top & rel) / len(rel)


def f1_at_k(retrieved: list[str], relevant: list[str], k: int) -> float:
    p = precision_at_k(retrieved, relevant, k)
    r = recall_at_k(retrieved, relevant, k)
    if p + r == 0:
        return 0.0
    return 2 * p * r / (p + r)


# ---------------- LLM 调用 ----------------
async def call_llm(provider_id: int | None, model_id: str | None, prompt: str) -> str | None:
    """直接按 providerId+modelId 调 LLM。没配时回落系统默认对话模型。

    返回 None 表示调用失败（不抛错）：评估是"尽力而为"的批处理，
    单题失败应该记下来继续跑，而不是让整场评估崩掉。

    **唯一例外是预算超限**（`BudgetExceeded`）——Java 的 checkBudget
    就在 EvalChatServiceImpl 每次 LLM 调用前抛 BizException 让整场评估
    终止；继续跑只会每题都空转。放行路径在此之外的失败仍返回 None。
    """
    # 预算闸（Java EvalChatServiceImpl:62-63 同位置）：估算口径同 Java
    # estimateTokens —— 字符数 × 1.2。user_id=0（系统调用，Java 同款），
    # 实际只受单次上限 + 全局日限额约束。
    from app.core.db import SessionLocal as _SL
    from app.services.token_budget_service import check_budget

    async with _SL() as bdb:
        await check_budget(bdb, 0, int(len(prompt) * 1.2))

    from app.services.llm_client import LlmConfig, stream_chat

    pid = provider_id
    mid = model_id
    if pid is None or not mid:
        from app.core.db import SessionLocal
        from app.services.system_config_service import SystemConfigService

        async with SessionLocal() as db:
            default = await SystemConfigService(db).get_chat()
        pid = pid or int(default.get("providerId") or 0) or None
        mid = mid or default.get("modelId")
    if not pid or not mid:
        return None

    from app.workflow.nodes import _resolve_provider

    resolved = await _resolve_provider(int(pid))
    if not resolved:
        return None
    base_url, api_key = resolved

    cfg = LlmConfig(base_url=base_url, api_key=api_key, model=str(mid), max_tokens=2048)
    parts: list[str] = []
    try:
        async for ev in stream_chat(cfg, [{"role": "user", "content": prompt}]):
            if ev.kind == "text":
                parts.append(ev.text)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[RagEval] LLM 调用失败: %s", exc)
        return None
    finally:
        # 限流：每次调用后等 1s，避免 429
        await asyncio.sleep(LLM_CALL_DELAY_MS / 1000)
    return "".join(parts)


# ---------------- 基准生成 ----------------
async def generate_benchmark_items(
    db: AsyncSession,
    knowledge_id: int,
    count: int,
    provider_id: int | None,
    model_id: str | None,
    neighbor_count: int = 3,
    progress_cb: Any = None,
) -> list[EvalRagBenchmarkItem]:
    """AI 生成考题：随机选 anchor chunk + 前后 N 个邻居当上下文喂给 LLM。

    `max_attempts = max(count*5, 50)`：LLM 会返回非法 JSON / 无效 chunkId，
    单次成功率不高，靠多试几轮凑够 count 条。
    """
    rows = (
        await db.execute(
            select(Chunk.id, Chunk.content)
            .where(Chunk.knowledge_id == knowledge_id)
            .where(Chunk.status == "vectorized")
        )
    ).all()
    if not rows:
        return []

    chunk_content = {int(r.id): r.content for r in rows}
    chunk_ids = list(chunk_content.keys())

    items: list[EvalRagBenchmarkItem] = []
    rng = random.Random()
    max_attempts = max(count * 5, 50)

    for _attempt in range(max_attempts):
        if len(items) >= count:
            break
        anchor_id = rng.choice(chunk_ids)
        anchor_idx = chunk_ids.index(anchor_id)
        context_ids = [anchor_id]
        for offset in range(-neighbor_count, neighbor_count + 1):
            if offset == 0:
                continue
            ni = anchor_idx + offset
            if 0 <= ni < len(chunk_ids):
                context_ids.append(chunk_ids[ni])

        ctx = "".join(f"片段ID={cid}\n{chunk_content[cid]}\n\n" for cid in context_ids)
        result = await call_llm(provider_id, model_id, BENCHMARK_GEN_PROMPT % ctx)
        if not result or not result.strip():
            continue

        try:
            node = json.loads(extract_json(result) or "")
        except json.JSONDecodeError:
            continue
        if not all(k in node for k in ("query", "gold_answer", "gold_chunk_ids")):
            continue
        query = str(node.get("query") or "")
        gold_answer = str(node.get("gold_answer") or "")
        if not query.strip() or not gold_answer.strip():
            continue

        valid_ids = set(context_ids)
        gold_chunk_ids: list[str] = []
        raw_ids = node.get("gold_chunk_ids")
        if isinstance(raw_ids, list):
            for v in raw_ids:
                try:
                    cid = int(str(v))
                except ValueError:
                    continue
                if cid in valid_ids:
                    gold_chunk_ids.append(str(cid))
        if not gold_chunk_ids:
            continue

        items.append(
            EvalRagBenchmarkItem(
                id=new_id(),
                benchmark_id=0,  # 落库前由调用方回填
                query=query,
                gold_answer=gold_answer,
                gold_chunk_ids=json.dumps(gold_chunk_ids, ensure_ascii=False),
                sort_order=len(items),
            )
        )
        if progress_cb:
            progress_cb(30 + int(len(items) / count * 60))

    return items


# ---------------- 单题评估 ----------------
async def evaluate_question(
    db: AsyncSession,
    item: EvalRagBenchmarkItem,
    knowledge_id: int,
    answer_provider_id: int | None,
    answer_model_id: str | None,
    judge_provider_id: int | None,
    judge_model_id: str | None,
) -> EvalRagResultDetail:
    detail = EvalRagResultDetail(
        id=new_id(),
        result_id=0,
        query=item.query,
        gold_chunk_ids=item.gold_chunk_ids,
        gold_answer=item.gold_answer,
    )

    # 1. 向量检索 top10
    try:
        vec = await embed_query(item.query)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[RagEval] query 向量化失败: %s", exc)
        vec = None

    retrieved: list[str] = []
    search_results: list[dict[str, Any]] = []
    if vec is not None:
        rows = (
            await db.execute(
                text(EVAL_SEARCH_SQL),
                {"vec": to_pgvector(vec), "kb_id": knowledge_id, "top_k": 10},
            )
        ).all()
        for r in rows:
            retrieved.append(str(int(r.chunk_id)))
            search_results.append({"chunk_id": int(r.chunk_id), "content": r.content})
    detail.retrieved_chunk_ids = json.dumps(retrieved, ensure_ascii=False)

    # 2. 检索指标（有 gold_chunk_ids 才算）
    gold_ids = parse_json_string_list(item.gold_chunk_ids)
    if gold_ids:
        scores: dict[str, float] = {}
        for k in K_VALUES:
            scores[f"precision@{k}"] = round(precision_at_k(retrieved, gold_ids, k), 4)
            scores[f"recall@{k}"] = round(recall_at_k(retrieved, gold_ids, k), 4)
            scores[f"f1@{k}"] = round(f1_at_k(retrieved, gold_ids, k), 4)
        detail.retrieval_scores = json.dumps(scores, ensure_ascii=False)
    else:
        detail.retrieval_scores = "{}"

    # 3. 生成答案 + 4. LLM 评判
    if answer_provider_id and search_results:
        docs = "".join(
            f"[{i}] {r['content']}\n\n" for i, r in enumerate(search_results, start=1)
        )
        generated = await call_llm(
            answer_provider_id, answer_model_id, ANSWER_GEN_PROMPT % (docs, item.query)
        )
        detail.generated_answer = generated

        if (
            item.gold_answer
            and item.gold_answer.strip()
            and judge_provider_id
            and generated
        ):
            judge_raw = await call_llm(
                judge_provider_id,
                judge_model_id,
                JUDGE_PROMPT % (item.query, item.gold_answer, generated),
            )
            if not judge_raw or not judge_raw.strip():
                detail.answer_score = 0.0
                detail.answer_reasoning = "LLM 评判调用失败"
            else:
                try:
                    node = json.loads(extract_json(judge_raw) or "")
                    detail.answer_score = float(node.get("score", 0.0))
                    detail.answer_reasoning = str(node.get("reasoning", ""))
                except (json.JSONDecodeError, TypeError, ValueError):
                    detail.answer_score = 0.0
                    detail.answer_reasoning = f"评分结果解析失败: {judge_raw[:200]}"

    return detail


def aggregate_metrics(details: list[EvalRagResultDetail]) -> dict[str, Any]:
    """聚合口径照抄 Java：retrieval 只聚合 recall/f1，overallScore 不含答案分。"""
    retrieval_agg = {f"{m}@{k}": 0.0 for k in K_VALUES for m in ("recall", "f1")}
    retrieval_count = 0
    total_answer = 0.0
    answer_count = 0

    for d in details:
        if d.retrieval_scores and d.retrieval_scores != "{}":
            try:
                scores = json.loads(d.retrieval_scores)
                for key in retrieval_agg:
                    retrieval_agg[key] += float(scores.get(key, 0.0))
                retrieval_count += 1
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
        if d.answer_score is not None:
            total_answer += d.answer_score
            answer_count += 1

    result: dict[str, Any] = {}
    avg_retrieval: dict[str, float] = {}
    if retrieval_count > 0:
        avg_retrieval = {k: round(v / retrieval_count, 4) for k, v in retrieval_agg.items()}
    result["retrieval"] = avg_retrieval

    if answer_count > 0:
        result["answer"] = {
            "accuracy": round(total_answer / answer_count, 4),
            "total": answer_count,
            "correct": int(total_answer),
        }

    overall = sum(avg_retrieval.values()) / len(avg_retrieval) if avg_retrieval else 0.0
    result["overallScore"] = round(overall, 4)
    return result


def _truncate(value: str | None, limit: int) -> str:
    if not value:
        return ""
    return value if len(value) <= limit else value[:limit] + "..."


async def generate_analysis(
    result: EvalRagResult,
    details: list[EvalRagResultDetail],
    metrics: dict[str, Any],
    provider_id: int | None,
) -> str:
    """末步 AI 分析报告：优先用评判模型，否则系统默认。

    失败只 log 不抛 —— 报告是锦上添花，不能让 COMPLETED 的结果变 FAILED。
    """
    context = ["## 评估概况"]
    context.append(f"- 基准名称：{result.benchmark_name or ''}")
    context.append(f"- 综合评分：{(result.overall_score or 0) * 100:.1f}%")
    context.append(f"- 题目总数：{len(details)}")
    context.append(f"- 耗时：{result.duration_ms or 0}ms\n")

    if metrics.get("retrieval"):
        context.append("## 检索指标")
        for k, v in metrics["retrieval"].items():
            text_v = f"{v * 100:.1f}%" if isinstance(v, (int, float)) else v
            context.append(f"- {k}: {text_v}")
        context.append("")

    if metrics.get("answer"):
        context.append("## 答案指标")
        for k, v in metrics["answer"].items():
            text_v = f"{v * 100:.1f}%" if isinstance(v, float) and k == "accuracy" else v
            context.append(f"- {k}: {text_v}")
        context.append("")

    error_items = [d for d in details if d.answer_score is not None and d.answer_score < 1.0]
    context.append("## 错误统计")
    context.append(f"- 错误题目数：{len(error_items)}/{len(details)}")
    if error_items:
        context.append("- 错误题目列表：")
        for d in error_items[:5]:
            context.append(
                f"  - 问题：{d.query}，AI答案：{_truncate(d.generated_answer, 50)}"
                f"，正确答案：{_truncate(d.gold_answer, 50)}"
            )

    report = await call_llm(
        provider_id, None, ANALYSIS_SYSTEM_PROMPT + "\n\n" + "\n".join(context)
    )
    return (report or "").strip()


# ---------------- JSONL 解析（上传共用） ----------------
def parse_jsonl_lines(raw: bytes) -> tuple[list[dict[str, Any]], list[str]]:
    """逐行校验 JSONL，返回 (合法行, 错误列表)。

    校验口径照 Java：每行必须合法 JSON 且 query 非空；
    gold_chunk_ids 允许缺省，出现时必须是字符串数组。
    """
    lines: list[dict[str, Any]] = []
    errors: list[str] = []
    for idx, line in enumerate(raw.decode("utf-8", errors="replace").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            node = json.loads(line)
        except json.JSONDecodeError as exc:
            errors.append(f"第 {idx} 行 JSON 解析失败: {exc}")
            continue
        if not isinstance(node, dict) or not str(node.get("query") or "").strip():
            errors.append(f"第 {idx} 行缺少 query 字段或为空")
            continue
        lines.append(node)
    return lines, errors


def items_from_jsonl(
    benchmark_id: int, records: list[dict[str, Any]]
) -> list[EvalRagBenchmarkItem]:
    items: list[EvalRagBenchmarkItem] = []
    for i, node in enumerate(records):
        gold_ids = node.get("gold_chunk_ids")
        items.append(
            EvalRagBenchmarkItem(
                id=new_id(),
                benchmark_id=benchmark_id,
                query=str(node.get("query")),
                gold_answer=(
                    str(node["gold_answer"]) if node.get("gold_answer") is not None else None
                ),
                gold_chunk_ids=(
                    json.dumps([str(x) for x in gold_ids], ensure_ascii=False)
                    if isinstance(gold_ids, list)
                    else None
                ),
                sort_order=i,
            )
        )
    return items
