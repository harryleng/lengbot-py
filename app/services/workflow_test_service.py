"""工作流调试运行（编排页「调试」抽屉 / 单节点调试）。

对应 Java `WorkflowConfigServiceImpl.testRun` / `testNode` / `testRunStream`。

**一次调试要做的事**（顺序不能错）：

1. 决定用哪张图 —— 请求体带了 `graph` 就用它（画布上还没保存的最新内容），
   否则按 `useDraft` 从库里读。**这是编排页的核心体验**：用户改完点"调试"，
   必须跑他眼前看到的图，而不是上一次自动保存的版本。
2. 立刻落一条 `running` 记录 —— 流程可能要跑十几秒，中途崩了也要留下痕迹，
   否则用户只会看到"点了没反应"。
3. 跑执行器、收集事件。
4. 收尾写 `completed` / `failed` / `suspended`。

**为什么 REST 和 SSE 走同一个内部生成器**：两条路径的语义完全一样，只是
一个边跑边推、一个跑完一次性返回。写成两份迟早出现"流式能看到节点事件、
非流式看不到"这种差异。

前端实际用的是 SSE（`testWorkflowStream`），REST 版留给脚本和单测。
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.services.workflow_editor_service import WorkflowEditorService
from app.services.workflow_test_run_service import WorkflowTestRunService, new_run_id
from app.workflow.events import to_sse_event
from app.workflow.executor import WorkflowExecutor
from app.workflow.graph import WorkflowGraph
from app.workflow.nodes import WorkflowContext, get_handler
from app.workflow.state import ExecState

logger = logging.getLogger(__name__)

# 单节点调试时给 handler 用的默认上下文变量（对齐 Java）
DEFAULT_QUERY = "测试输入"


class WorkflowTestService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.editor = WorkflowEditorService(db)
        self.runs = WorkflowTestRunService(db)

    # ------------------------------------------------------------------
    # 公共：取图
    # ------------------------------------------------------------------
    async def _resolve_graph(
        self, agent_id: int, req: dict[str, Any]
    ) -> tuple[dict[str, Any], bool]:
        """返回 (图, 是否用的草稿)。

        请求体里的 `graph` 优先级最高 —— 它代表"画布当前状态"。
        这时 `usedDraft` 记 True，因为图确实还没进发布版。
        """
        graph = req.get("graph")
        if isinstance(graph, dict) and (graph.get("nodes") or []):
            return graph, True

        use_draft = req.get("useDraft")
        use_draft = True if use_draft is None else bool(use_draft)
        loaded = await self.editor.load_definition(agent_id, use_draft=use_draft)
        if not loaded:
            from fastapi import HTTPException, status

            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="工作流为空，请先配置节点"
            )
        return loaded, use_draft

    @staticmethod
    def _initial_variables(req: dict[str, Any]) -> dict[str, Any]:
        """调试入参 → 变量池。

        `input` 和 `query` 都要有：不同节点的 `inputVariable` 写的可能是
        `{{query}}`（检索类）也可能是 `{{input}}`（生成类）。只填一个，
        另一类节点就会拿到空字符串去跑 —— 表现为"检索不到任何东西"或
        "让模型根据空内容回答"，很难一眼看出是入参没传进去。
        """
        variables: dict[str, Any] = {}
        raw = req.get("input")
        if raw is not None:
            variables["input"] = raw
            variables["query"] = raw
        variables.setdefault("query", DEFAULT_QUERY)
        variables.setdefault("input", variables["query"])

        history = req.get("conversationHistory")
        if history:
            variables["conversationHistory"] = history
        return variables

    def _ctx(self, agent_id: int, user_id: int) -> WorkflowContext:
        return WorkflowContext(
            agent_id=agent_id,
            user_id=user_id,
            allow_private_network=settings.workflow_allow_private_network,
        )

    # ------------------------------------------------------------------
    # 内部统一执行
    # ------------------------------------------------------------------
    async def execute(
        self,
        agent_id: int,
        user_id: int,
        req: dict[str, Any],
    ) -> AsyncIterator[dict[str, Any]]:
        """跑一次调试。yield **内部格式**事件，调用方负责翻译。

        事件流最后一定有一个 `workflow_done`；出错时是
        `{"type": "workflow_done", "error": ...}`（不是异常），
        这样 SSE 和 REST 都能用同一套判据收尾。
        """
        graph_raw, used_draft = await self._resolve_graph(agent_id, req)
        run_id = new_run_id()
        test_mode = str(req.get("testMode") or "generation")

        await self.runs.start(
            agent_id=agent_id,
            user_id=user_id,
            run_id=run_id,
            user_input=req.get("input"),
            test_mode=test_mode,
            used_draft=used_draft,
            graph=graph_raw,
        )

        started = time.time()
        node_events: list[dict[str, Any]] = []
        output: str | None = None
        error: str | None = None
        suspended = False
        variables: dict[str, Any] = {}

        try:
            graph = WorkflowGraph.parse(graph_raw)
        except ValueError as exc:
            error = str(exc)
            await self.runs.finish(
                run_id=run_id,
                output=None,
                node_events=[],
                variables=None,
                error=error,
                suspended=False,
                duration_ms=int((time.time() - started) * 1000),
            )
            yield {"type": "error", "message": error}
            yield {"type": "workflow_done", "output": None, "error": error, "runId": run_id}
            return

        executor = WorkflowExecutor(graph, self._ctx(agent_id, user_id), run_id=run_id)
        try:
            async for ev in executor.run(self._initial_variables(req)):
                # 每次 yield 前先翻译 —— 存进 test_run 的也要是前端格式，
                # 这样"实时看的"和"事后翻历史"长得一样，前端一套渲染逻辑够用。
                sse_ev = to_sse_event(ev)
                if sse_ev.get("type") == "workflow_node_complete":
                    node_events.append(sse_ev)
                elif sse_ev.get("type") == "workflow_complete":
                    output = sse_ev.get("output")
                    error = sse_ev.get("error")
                    variables = sse_ev.get("variables") or {}
                elif sse_ev.get("type") == "workflow_suspended":
                    suspended = True
                sse_ev.setdefault("runId", run_id)
                yield sse_ev
        except Exception as exc:  # noqa: BLE001
            logger.exception("调试运行异常 agent=%s run=%s", agent_id, run_id)
            error = f"{type(exc).__name__}: {exc}"
            yield {"type": "error", "message": error, "runId": run_id}
            yield {"type": "workflow_done", "output": None, "error": error, "runId": run_id}
        finally:
            # 客户端中途断开（AbortController）也会走到这里 —— 记录标 failed，
            # 别让它永远挂在 running 状态上，否则前端显示"运行中"再也不变。
            await self.runs.finish(
                run_id=run_id,
                output=str(output) if output is not None else None,
                node_events=node_events,
                variables=variables or None,
                error=error,
                suspended=suspended,
                duration_ms=int((time.time() - started) * 1000),
            )

    # ------------------------------------------------------------------
    # REST 版
    # ------------------------------------------------------------------
    async def run_sync(self, agent_id: int, user_id: int, req: dict[str, Any]) -> dict[str, Any]:
        events: list[dict[str, Any]] = []
        output: str | None = None
        variables: dict[str, Any] = {}
        error: str | None = None
        suspended = False
        run_id: str | None = None

        async for ev in self.execute(agent_id, user_id, req):
            events.append(ev)
            run_id = str(ev.get("runId") or run_id or "")
            if ev.get("type") == "workflow_complete":
                output = ev.get("output")
                variables = ev.get("variables") or {}
                error = ev.get("error")
            elif ev.get("type") == "workflow_suspended":
                suspended = True

        return {
            "output": str(output) if output is not None else (error or ""),
            "nodeEvents": [e for e in events if e.get("type", "").startswith("workflow_")],
            "usedDraft": True,
            "variables": variables,
            "suspended": suspended,
            "runId": run_id,
            "testRunId": None,
        }

    # ------------------------------------------------------------------
    # SSE 版
    # ------------------------------------------------------------------
    async def stream(self, agent_id: int, user_id: int, req: dict[str, Any]) -> AsyncIterator[str]:
        """SSE 帧格式与聊天一致：`[STATUS]` + JSON、最后 `[DONE]` + JSON。

        前端 `processSseLines` 就是按这个约定解析的，改格式它一个字都读不出来。
        """
        import json as _json

        output: Any = None
        variables: dict[str, Any] = {}
        error: str | None = None
        suspended = False
        confirm_form: dict[str, Any] | None = None
        run_id: str | None = None
        events: list[dict[str, Any]] = []

        async for ev in self.execute(agent_id, user_id, req):
            events.append(ev)
            run_id = str(ev.get("runId") or run_id or "")
            t = ev.get("type")
            if t == "workflow_complete":
                output = ev.get("output")
                variables = ev.get("variables") or {}
                error = ev.get("error")
            elif t == "workflow_suspended":
                suspended = True
            elif t == "workflow_confirm_required":
                confirm_form = ev.get("confirmForm")

            # 必须是 `data:` 前缀 + 空行结尾 —— 前端 processSseLines 是按行
            # 找 `data:` 的，裸字符串一个事件都收不到（静默无输出）。
            yield "data:" + _status_frame(ev) + "\n\n"

        done = {
            "runId": run_id,
            "output": str(output) if output is not None else "",
            "variables": variables,
            "suspended": suspended,
            "error": error,
            "nodeEvents": [e for e in events if str(e.get("type", "")).startswith("workflow_")],
        }
        if confirm_form:
            done["confirmForm"] = confirm_form
        yield "data:[DONE]" + _json.dumps(done, ensure_ascii=False, default=str) + "\n\n"

    # ------------------------------------------------------------------
    # 单节点调试
    # ------------------------------------------------------------------
    async def run_node(
        self, agent_id: int, user_id: int, req: dict[str, Any]
    ) -> dict[str, Any]:
        """只跑一个节点 —— 用户点节点上的「测试」用。

        **不落 test_run**：这不是一次流程运行，落进去会污染"最近 50 条"列表，
        让用户找不到真正跑完的整条记录（Java 的 testNode 也不落库）。
        """
        node_id = str(req.get("nodeId") or "")
        if not node_id:
            from fastapi import HTTPException, status

            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="缺少节点 ID")

        graph_raw, used_draft = await self._resolve_graph(agent_id, req)
        try:
            graph = WorkflowGraph.parse(graph_raw)
        except ValueError as exc:
            from fastapi import HTTPException, status

            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

        if node_id not in graph.nodes:
            from fastapi import HTTPException, status

            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=f"图上没有节点 {node_id}"
            )

        node_type = graph.node_type(node_id)
        data = graph.node_data(node_id)
        label = str(data.get("label") or node_id)

        # 入参：前端传的是 {key: value} 的明文，直接进变量池
        variables: dict[str, Any] = {}
        for key, value in (req.get("inputParams") or {}).items():
            if key and str(key).strip():
                variables[str(key)] = "" if value is None else value
        if not str(variables.get("query") or "").strip():
            variables["query"] = variables.get("input") or DEFAULT_QUERY
        if not str(variables.get("input") or "").strip():
            variables["input"] = variables["query"]

        state = ExecState(variables=variables)
        state.current = node_id
        started = time.time()

        def _event(ok: bool, message: str | None = None, output: Any = None) -> dict[str, Any]:
            ev: dict[str, Any] = {
                "type": "workflow_node_complete",
                "nodeId": node_id,
                "nodeType": node_type,
                "nodeLabel": label,
                "success": ok,
                "durationMs": int((time.time() - started) * 1000),
            }
            if message:
                ev["message"] = message
            if output is not None:
                ev["output"] = output
            return ev

        try:
            result = await get_handler(node_type)(data, state, self._ctx(agent_id, user_id))
        except Exception as exc:  # noqa: BLE001
            # 单节点调试**不抛 HTTP 错误**：用户正在调参，报错要能直接看到，
            # 前端也是按结构化结果渲染的。抛 400 会让抽屉里只剩一句"请求失败"。
            logger.warning("单节点调试失败 node=%s: %s", node_id, exc)
            return {
                "output": str(exc),
                "nodeEvents": [_event(False, str(exc))],
                "usedDraft": used_draft,
                "variables": dict(state.variables),
                "suspended": False,
            }

        output = result.output
        text = output if isinstance(output, str) else _dump(output)
        return {
            "output": text,
            "nodeEvents": [_event(True, output=_dump(output))],
            "usedDraft": used_draft,
            "variables": dict(state.variables),
            "suspended": bool(result.suspended),
        }


def _status_frame(ev: dict[str, Any]) -> str:
    """`[STATUS]` + JSON。"""
    import json as _json

    return "[STATUS]" + _json.dumps(ev, ensure_ascii=False, default=str)


def _dump(value: Any, limit: int = 600) -> str:
    import json as _json

    try:
        text = _json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(value)
    return text if len(text) <= limit else text[:limit] + "…"
