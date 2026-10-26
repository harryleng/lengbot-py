"""工作流节点处理器。

每种节点类型一个 async 函数，签名统一：
    async def handle_xxx(data: dict, state: ExecState, ctx: WorkflowContext) -> NodeResult

配置字段名严格对齐前端存的 JSON（实测自 `agent_version.config.graph`）：

| 类型 | 关键配置 |
|---|---|
| input | `outputParams: [{key, type, defaultValue}]` |
| variable | `variableName`, `variableValue`（可含 `{{}}`） |
| llm | `sysPrompt`, `promptTemplate`, `temperature`, `enableStreaming` |
| retrieval | `knowledgeId`, `topK`, `threshold`, `inputVariable` |
| condition | `conditionGroups: [{rules: [{variable, operator, value}], relation, sourceHandle}]` |
| output | `output`（可含 `{{}}`） |
| confirm | `message`, `formFields`（尚未实现，见 executor 的挂起分支） |

**作用域是平铺的**：实测条件节点写的是 `{{statusCode}}` 而不是
`{{api_1.statusCode}}`，说明节点输出的 dict 会直接 merge 进全局变量，
不是只挂在 node_outputs 下。所以每个节点执行完都要把 output 平铺进 variables。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from app.workflow.state import ExecState, render

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class WorkflowContext:
    """执行一个工作流需要的外部依赖。"""

    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    llm_max_tokens: int = 2048
    knowledge_ids: list[int] = field(default_factory=list)
    user_id: int | None = None
    agent_id: int | None = None
    session_id: int | None = None
    # 是否允许 api 节点访问内网/回环地址。
    # 默认 False（挡住 SSRF）；自托管部署要调自家服务时由配置打开。
    # 不用环境变量兜底到节点配置里 —— 那等于"用户能自己关掉防护"。
    allow_private_network: bool = False

    def to_snapshot(self) -> dict[str, Any]:
        """挂起时要存进快照的部分（恢复时重建 ctx 用）。"""
        return {
            "llm_base_url": self.llm_base_url,
            "llm_api_key": self.llm_api_key,
            "llm_model": self.llm_model,
            "llm_max_tokens": self.llm_max_tokens,
            "knowledge_ids": list(self.knowledge_ids),
            "user_id": self.user_id,
            "agent_id": self.agent_id,
            "session_id": self.session_id,
            "allow_private_network": self.allow_private_network,
        }

    @classmethod
    def from_snapshot(cls, data: dict[str, Any]) -> WorkflowContext:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass(slots=True)
class NodeResult:
    output: Any = None
    handle: str = "out"
    finished: bool = False
    suspended: bool = False
    suspend_payload: dict[str, Any] | None = None


# ----------------------------------------------------------------------
# 各类型节点
# ----------------------------------------------------------------------


async def handle_start(data: dict, state: ExecState, ctx: WorkflowContext) -> NodeResult:
    return NodeResult(output={})


async def handle_input(data: dict, state: ExecState, ctx: WorkflowContext) -> NodeResult:
    """流程输入：把外部入参按 outputParams 声明取出来。

    值优先从已有 variables 取（调用方传进来的），取不到用 defaultValue。
    """
    params = data.get("outputParams") or []
    out: dict[str, Any] = {}
    for p in params:
        key = p.get("key")
        if not key:
            continue
        value = state.variables.get(key)
        if value is None:
            value = p.get("defaultValue", "")
        out[key] = value
        state.set(key, value)
    return NodeResult(output=out)


async def handle_variable(data: dict, state: ExecState, ctx: WorkflowContext) -> NodeResult:
    """变量赋值：`variableName = render(variableValue)`。"""
    name = data.get("variableName")
    if not name:
        return NodeResult(output={})
    value = render(data.get("variableValue", ""), state)
    state.set(str(name), value)
    return NodeResult(output={str(name): value})


async def handle_llm(data: dict, state: ExecState, ctx: WorkflowContext) -> NodeResult:
    """LLM 节点：渲染提示词 → 调用模型 → 输出文本。

    **模型配置是节点级的**，不是 Agent 级的 —— 实测 llm 节点的 data 里写着
    `providerId` / `modelId` / `temperature`，而 workflow 型 Agent 的 config 是空 `{}`。
    这一点和 chat 型 Agent 完全不同（那种才把 provider 放在 agent.config 里）。

    节点没配 provider 时才回落到 ctx（调用方注入的默认值）。
    输出同时写进 `llmOutput`（实测工作流里用的是 `{{llmOutput}}`）。
    """
    from app.services.llm_client import LlmConfig, stream_chat

    prompt = render(data.get("promptTemplate") or data.get("prompt") or "", state)
    system = render(data.get("sysPrompt") or data.get("systemPrompt") or "", state)
    if not prompt:
        return NodeResult(output={"llmOutput": ""})

    base_url, api_key = ctx.llm_base_url, ctx.llm_api_key
    model = ctx.llm_model
    provider_id = data.get("providerId")
    if provider_id:
        resolved = await _resolve_provider(int(provider_id))
        if resolved:
            base_url, api_key = resolved
    if data.get("modelId"):
        model = str(data["modelId"])

    messages: list[dict[str, Any]] = []
    if system:
        messages.append({"role": "system", "content": str(system)})
    messages.append({"role": "user", "content": str(prompt)})

    cfg = LlmConfig(
        base_url=base_url,
        api_key=api_key,
        model=model,
        max_tokens=ctx.llm_max_tokens,
        temperature=data.get("temperature"),
    )
    parts: list[str] = []
    async for ev in stream_chat(cfg, messages):
        if ev.kind == "text":
            parts.append(ev.text)
    text = "".join(parts)
    state.set("llmOutput", text)
    logger.info("LLM 节点产出 %d 字符（model=%s）", len(text), model)
    return NodeResult(output={"llmOutput": text})


_provider_cache: dict[int, tuple[str, str]] = {}


async def _resolve_provider(provider_id: int) -> tuple[str, str] | None:
    """按 providerId 取 base_url / api_key（进程内缓存）。"""
    if provider_id in _provider_cache:
        return _provider_cache[provider_id]
    from sqlalchemy import text as sql_text

    from app.core.db import SessionLocal

    async with SessionLocal() as db:
        row = (
            await db.execute(
                sql_text("SELECT base_url, api_key FROM model_provider WHERE id = :id"),
                {"id": provider_id},
            )
        ).first()
    if row is None or not row[0] or not row[1]:
        logger.warning("provider %s 不存在或未配置 key", provider_id)
        return None
    _provider_cache[provider_id] = (row[0], row[1])
    return _provider_cache[provider_id]


async def handle_retrieval(data: dict, state: ExecState, ctx: WorkflowContext) -> NodeResult:
    """知识检索节点：向量召回 → 输出片段列表和拼接后的上下文。"""
    from app.services.rag_service import search

    query = render(data.get("inputVariable") or "", state)
    kb_id = data.get("knowledgeId")
    # 节点级 knowledgeId 优先；没有就用 Agent 绑定的
    kb_ids = [int(kb_id)] if kb_id else ctx.knowledge_ids
    top_k = int(data.get("topK") or 5)
    threshold = data.get("threshold")

    results = await search(str(query), kb_ids, top_k=top_k, min_score=threshold)
    if not results:
        # 空结果有两种原因，必须区分开，否则用户以为"知识库里没有"
        # ① 库里真没有相关内容  ② 节点配的 threshold 太高，把结果全过滤了
        raw = await search(str(query), kb_ids, top_k=top_k, min_score=None)
        if raw:
            best = max(float(r.get("score") or 0) for r in raw)
            logger.warning(
                "检索节点过滤掉全部结果：阈值=%.2f，最高分=%.3f（%d 条候选）",
                float(threshold or 0),
                best,
                len(raw),
            )
        else:
            logger.warning("检索节点真的没召回任何内容（query=%r, kb=%s）", query, kb_ids)
    context = "\n\n".join(
        f"[{(r.get('document_name') or '文档')}] {r.get('content') or ''}" for r in results
    )
    out = {
        "results": results,
        "context": context,
        "count": len(results),
        # ⚠️ 变量名是 `retrievalResult`（单数）—— 实测下游 variable_handle 节点的
        # templateContent 里写的是 `{{retrievalResult}}`。名字差一个字，模板就渲染成空，
        # LLM 会一本正经地说"知识库里没有相关内容"（最难查的那类 bug：不报错，只是答非所问）。
        "retrievalResult": context,
    }
    state.set("retrievalContext", context)
    state.set("retrievalResults", results)
    state.set("retrievalResult", context)
    return NodeResult(output=out)


async def handle_variable_handle(data: dict, state: ExecState, ctx: WorkflowContext) -> NodeResult:
    """变量处理节点：把检索结果拼进提示词模板。

    实测配置（`data`）：
        handleType: "template"
        templateContent: "以下是知识库检索到的相关内容：\\n\\n{{retrievalResult}}\\n\\n..."
        groups / groupStrategy: "firstNotNull"（多组取第一个非空）

    **输出变量名固定是 `output`** —— 下游 LLM 节点的 promptTemplate 写的是
    `用户问题：{{query}}\\n\\n{{output}}`。这一点不看真实配置猜不到。
    """
    template = data.get("templateContent")
    if template:
        text = render(str(template), state)
        state.set("output", text)
        return NodeResult(output={"output": text})

    # 非 template 模式（groups / firstNotNull）暂不实现，原样通过
    logger.warning("variable_handle 节点非 template 模式，已跳过")
    return NodeResult(output={})


def _compare(actual: Any, operator: str, expected: Any) -> bool:
    """条件运算符。缺一个就要出 bug，所以这里是全集（对齐前端算子列表）。"""
    a = "" if actual is None else actual
    e = "" if expected is None else expected
    op = (operator or "eq").lower()

    if op == "eq":
        return str(a) == str(e)
    if op == "neq":
        return str(a) != str(e)
    if op == "gt":
        return _as_num(a) > _as_num(e)
    if op == "gte":
        return _as_num(a) >= _as_num(e)
    if op == "lt":
        return _as_num(a) < _as_num(e)
    if op == "lte":
        return _as_num(a) <= _as_num(e)
    if op == "contains":
        return str(e) in str(a)
    if op == "not_contains":
        return str(e) not in str(a)
    if op == "starts_with":
        return str(a).startswith(str(e))
    if op == "ends_with":
        return str(a).endswith(str(e))
    if op == "empty":
        return not str(a).strip()
    if op == "not_empty":
        return bool(str(a).strip())
    logger.warning("未知条件运算符 %r，按 False 处理", operator)
    return False


def _as_num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


async def handle_condition(data: dict, state: ExecState, ctx: WorkflowContext) -> NodeResult:
    """条件分支：按顺序检查每组，命中就返回该组的 sourceHandle。

    规则里的 `variable` 只写变量名（`statusCode`），不写节点前缀 ——
    因为节点输出已经平铺进 variables 了。
    """
    groups = data.get("conditionGroups") or []
    for idx, group in enumerate(groups):
        rules = group.get("rules") or []
        relation = str(group.get("relation") or "and").lower()
        results = [
            _compare(
                state.lookup(str(r.get("variable") or "")),
                str(r.get("operator") or "eq"),
                r.get("value"),
            )
            for r in rules
        ]
        if not results:
            continue
        matched = all(results) if relation == "and" else any(results)
        if matched:
            handle = str(group.get("sourceHandle") or f"out_{chr(97 + idx)}")
            logger.info("条件节点命中第 %d 组 → %s", idx + 1, handle)
            return NodeResult(output={"matched": idx + 1}, handle=handle)

    # 一组都没命中：走第一个 handle（与前端"else 分支"的常见约定一致）
    return NodeResult(output={"matched": None}, handle="out_a")


async def handle_output(data: dict, state: ExecState, ctx: WorkflowContext) -> NodeResult:
    """流程输出：渲染模板后作为最终结果。"""
    text = render(data.get("output") or data.get("text") or "", state)
    state.set("output", text)
    return NodeResult(output={"output": text}, finished=True)


async def handle_end(data: dict, state: ExecState, ctx: WorkflowContext) -> NodeResult:
    return NodeResult(output={}, finished=True)


async def handle_confirm(data: dict, state: ExecState, ctx: WorkflowContext) -> NodeResult:
    """人工确认节点（HITL）：执行到这里**挂起**，等人填表。

    对齐 Java `ConfirmNodeProcessor`：
        suspendPayload = {nodeId, message, formFields}
    message 默认 `"请填写以下信息并确认后继续"`。
    没有任何 formFields 时给一个 `info` 字段占位（前端靠它渲染"确认/取消"按钮）。

    注意：这里返回 `suspended=True` 而不是直接等 —— 挂起态由 executor 存进
    `suspended_runs`，之后走 `/workflow/resume` 从 `next_node_id` 继续。
    """
    message = render(data.get("message") or "请填写以下信息并确认后继续", state)
    fields = _resolve_form_fields(data, state)
    return NodeResult(
        suspended=True,
        handle="out",
        suspend_payload={
            "nodeId": state.current,
            "message": str(message),
            "formFields": fields,
        },
    )


def _resolve_form_fields(data: dict, state: ExecState) -> list[dict[str, Any]]:
    """把节点配置里的 formFields 渲染成前端要的结构。

    支持的 type（对齐 Java ConfirmNodeProcessor）：text / textarea / number /
    select / radio / checkbox / date / info
    """
    raw = data.get("formFields")
    if not raw:
        return [{"key": "confirmed", "label": "确认继续", "type": "info", "required": False}]

    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        key = item.get("key")
        if not key:
            continue
        field: dict[str, Any] = {
            "key": str(key),
            "label": str(render(item.get("label") or key, state)),
            "type": str(item.get("type") or "text"),
            "required": bool(item.get("required", False)),
        }
        for extra in ("options", "placeholder", "defaultValue", "help", "rows"):
            if item.get(extra) is not None:
                field[extra] = item[extra]
        out.append(field)
    return out or [{"key": "confirmed", "label": "确认继续", "type": "info", "required": False}]


class UnsupportedNodeType(ValueError):
    """工作流里出现了本实现还没支持的节点类型。"""


async def handle_noop(data: dict, state: ExecState, ctx: WorkflowContext) -> NodeResult:
    """未实现的节点类型：**抛异常中断**，不再"记日志原样通过"。

    ⚠️ 这里推翻了 2026-10 早期的设计。原来的注释写着"跳过比失败更有用"，
    那个理由是错的，实际后果是：

        api 节点被跳过 → 没有 body
        → script 节点被跳过 → 没有 title/chartData
        → condition 拿不到 statusCode → 条件恒不成立，走错分支
        → llm 拿着空变量瞎编
        → **整条流程 status=success，用户看到一段编出来的答案**

    也就是说 noop 把"功能缺失"伪装成了"执行成功"。这和之前踩的
    `retrievalResult` 变量名写错是同一类问题：链路全绿，答案全错，
    而且排查时没有任何线索。宁可当场失败并说清原因。

    异常会被 `executor._loop` 的 try/except 接住，转成
    `workflow_node_end(status=error)` + `workflow_done(error=...)`，
    前端能正常显示失败节点和原因。

    注意这个函数只是兜底 —— `get_handler` 会给未知类型返回一个带类型名的闭包，
    错误信息里那句"类型 xxx"才准确（node 的 type 在外层对象上，不在 data 里）。
    """
    raise UnsupportedNodeType(
        f"节点 {state.current} 尚未在 Python 版实现，工作流已中止。"
        f"已支持: {', '.join(sorted(HANDLERS))}"
    )


HANDLERS = {
    "start": handle_start,
    "input": handle_input,
    "variable": handle_variable,
    "variable_handle": handle_variable_handle,
    "llm": handle_llm,
    "retrieval": handle_retrieval,
    "condition": handle_condition,
    "output": handle_output,
    "end": handle_end,
    "confirm": handle_confirm,
}


def get_handler(node_type: str):
    """按类型取处理函数。未知类型返回一个**必然抛异常**的闭包。

    用闭包而不是直接返回 handle_noop：node 的 type 存在外层 node 对象上，
    `data` 里没有它（`graph.node_data()` 只返回 data 字段）。
    闭包能捕获 `node_type`，错误信息才能说清"是哪种类型没实现"，
    否则只能报"未知类型"，等于让人自己去翻 JSON 找。
    """
    handler = HANDLERS.get(node_type)
    if handler is not None:
        return handler

    async def _unsupported(data: dict, state: ExecState, ctx: WorkflowContext) -> NodeResult:
        raise UnsupportedNodeType(
            f"节点 {state.current} 的类型 '{node_type}' 尚未在 Python 版实现，"
            f"工作流已中止。已支持: {', '.join(sorted(HANDLERS))}"
        )

    return _unsupported


def dump(value: Any, limit: int = 400) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        text = str(value)
    return text if len(text) <= limit else text[:limit] + "…"


# ----------------------------------------------------------------------
# 外部能力节点（api / script / tool）
# ----------------------------------------------------------------------
# **必须在文件底部导入**：`nodes_external` 反过来要 import 本模块的
# `NodeResult` / `WorkflowContext`。放在顶部就是循环导入 ——
# 那时这两个类还没定义，python 会报 ImportError。
# 放底部时本模块已执行到这一行，类都定义完了，导入能成功。
from app.workflow.nodes_external import (  # noqa: E402
    handle_api,
    handle_script,
    handle_tool,
)

HANDLERS.update(
    {
        "api": handle_api,
        "script": handle_script,
        "tool": handle_tool,
    }
)
