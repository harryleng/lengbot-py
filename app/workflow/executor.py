"""工作流执行引擎：从 start 节点沿边走，执行到 end / output / confirm 为止。

对照 Java：`WorkflowExecutorService.runExecutionLoop` + `resumeAfterConfirm`。

**五个设计要点**：

1. **走哪条边由「节点 + handle」共同决定**
   普通节点返回 `handle="out"`，condition 返回 `out_a`/`out_b`。
   只看节点不看 handle，条件分支就会走成直线。

2. **节点输出平铺进全局变量**
   实测条件节点写的是 `{{statusCode}}` 而不是 `{{api_1.statusCode}}`，
   说明前面节点的 dict 输出是 merge 到全局的。

3. **未实现的节点类型直接报错，不静默跳过**
   早期这里是"记日志原样通过"，后果是：缺的节点不产出变量 → 后续 condition
   拿不到判断依据、llm 拿着空变量瞎编 → **整条流程显示成功、答案是编的**。
   失败从"节点报错"变成了"答案错"，更难查。现在抛 `UnsupportedNodeType`。

4. **执行步数有硬上限**
   工作流可能有环，没有上限就是死循环。超限报错而不是静默停止。

5. **HITL 挂起 = 存快照 + 停住**
   confirm 节点返回 `suspended=True` 后，把「图 + 变量 + 下一步」存进
   `suspended_runs`，发 `workflow_confirm_required` 事件，然后 return。
   之后 `/workflow/resume` 取出快照、把表单值写进变量、**从 next_node_id 继续** ——
   confirm 节点本身不会重跑（它已经执行完了）。
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from typing import Any

from app.workflow.graph import NODE_END, NODE_OUTPUT, WorkflowGraph
from app.workflow.nodes import WorkflowContext, dump, get_handler
from app.workflow.runs import SuspendedRun, suspended_runs
from app.workflow.state import ExecState

logger = logging.getLogger(__name__)

MAX_STEPS = 50  # 单个流程最多执行多少个节点


class WorkflowExecutor:
    def __init__(
        self,
        graph: WorkflowGraph,
        ctx: WorkflowContext,
        *,
        max_steps: int = MAX_STEPS,
        run_id: str | None = None,
    ) -> None:
        self.graph = graph
        self.ctx = ctx
        self.max_steps = max_steps
        # 编排页调试时由调用方指定（要写进 workflow_test_run.run_id），
        # 聊天链路不传，挂起时自动生成。
        self.run_id = run_id

    # ------------------------------------------------------------------
    # 对外入口
    # ------------------------------------------------------------------

    async def run(
        self,
        inputs: dict[str, Any] | None = None,
        *,
        content_offset: int = 0,
    ) -> AsyncIterator[dict[str, Any]]:
        """从头执行。"""
        state = ExecState(variables=dict(inputs or {}))
        async for ev in self._loop(self.graph, self.graph.entry_node(), state, content_offset):
            yield ev

    async def resume(
        self,
        snapshot: SuspendedRun,
        form_data: dict[str, Any] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """从挂起点继续。

        关键：**用快照里的图**，不是 self.graph。等待确认期间用户可能改了工作流，
        用新图续跑会导致"前半段用旧逻辑、后半段用新逻辑"的诡异状态。
        """
        graph = WorkflowGraph.parse(snapshot.graph_raw)
        state = ExecState(
            variables=dict(snapshot.variables),
            node_outputs=dict(snapshot.node_outputs),
            step=snapshot.step,
        )
        # 表单值直接进变量池 —— 下游节点写 `{{approved}}` / `{{备注}}` 就能取到
        for key, value in (form_data or {}).items():
            state.set(str(key), value)
        state.set("__confirmForm", form_data or {})

        if not snapshot.next_node_id:
            yield self._done(state, state.variables.get("output"))
            return

        logger.info(
            "工作流恢复: runId=%s 从 %s 继续（已执行 %d 步）",
            snapshot.run_id,
            snapshot.next_node_id,
            snapshot.step,
        )
        async for ev in self._loop(graph, snapshot.next_node_id, state, 0):
            yield ev

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------

    @staticmethod
    def _done(state: ExecState, output: Any = None, error: str | None = None) -> dict[str, Any]:
        """构造 `workflow_done` 事件。

        带上 `variables` 快照是给**编排页调试**用的：结果面板要显示"跑完后
        变量池里都有什么"，否则用户只能一个个节点点开看输出。
        聊天链路忽略这个字段，多带一点不影响。
        """
        ev: dict[str, Any] = {
            "type": "workflow_done",
            "output": output,
            "steps": state.step,
            "variables": dict(state.variables),
        }
        if error:
            ev["error"] = error
        return ev

    async def _loop(
        self,
        graph: WorkflowGraph,
        start_node: str,
        state: ExecState,
        content_offset: int,
    ) -> AsyncIterator[dict[str, Any]]:
        node_id: str | None = start_node
        final_output: Any = None

        while node_id:
            if state.step >= self.max_steps:
                logger.error("超过最大执行步数 %d，流程中止于 %s", self.max_steps, node_id)
                yield {
                    "type": "workflow_node_end",
                    "nodeId": node_id,
                    "status": "error",
                    "message": f"超过最大执行步数 {self.max_steps}（可能存在环）",
                }
                yield self._done(state, None, error="step_limit")
                return

            node_type = graph.node_type(node_id)
            data = graph.node_data(node_id)
            label = str(data.get("label") or node_id)
            state.current = node_id
            state.step += 1

            yield {
                "type": "workflow_node_start",
                "nodeId": node_id,
                "nodeType": node_type,
                "nodeLabel": label,
                "step": state.step,
                "contentOffset": content_offset,
            }

            handler = get_handler(node_type)
            node_started = time.time()
            try:
                result = await handler(data, state, self.ctx)
            except Exception as exc:  # noqa: BLE001
                logger.exception("节点 %s(%s) 执行失败", node_id, node_type)
                yield {
                    "type": "workflow_node_end",
                    "nodeId": node_id,
                    "nodeType": node_type,
                    "nodeLabel": label,
                    "status": "error",
                    "message": str(exc),
                    "durationMs": int((time.time() - node_started) * 1000),
                }
                yield self._done(state, None, error=str(exc))
                return

            if isinstance(result.output, dict):
                state.variables.update(result.output)
            state.node_outputs[node_id] = result.output
            if node_type == NODE_OUTPUT and isinstance(result.output, dict):
                final_output = result.output.get("output")

            yield {
                "type": "workflow_node_end",
                "nodeId": node_id,
                "nodeType": node_type,
                "nodeLabel": label,
                "status": "success",
                "handle": result.handle,
                "suspended": result.suspended,
                "output": dump(result.output, 300),
                # 编排页的节点卡片要显示"这一步花了多久"，没有这个字段就一直是空的。
                # 实测前端 `setReplayNodeStatus(nodeId, status, event.durationMs)`。
                "durationMs": int((time.time() - node_started) * 1000),
            }

            # ---------- HITL 挂起 ----------
            if result.suspended:
                payload = result.suspend_payload or {}
                next_id = graph.next_node(node_id, result.handle)
                snapshot = suspended_runs.create(
                    run_id=self.run_id,
                    agent_id=self.ctx.agent_id,
                    graph_raw=graph.raw,
                    variables=dict(state.variables),
                    node_outputs=dict(state.node_outputs),
                    suspend_node_id=node_id,
                    next_node_id=next_id,
                    step=state.step,
                    ctx=self.ctx.to_snapshot(),
                    user_id=self.ctx.user_id,
                    session_id=self.ctx.session_id,
                )
                confirm_form = {**payload, "runId": snapshot.run_id}
                logger.info(
                    "工作流挂起: runId=%s node=%s 表单字段=%d 下一个节点=%s",
                    snapshot.run_id,
                    node_id,
                    len(payload.get("formFields") or []),
                    next_id,
                )
                # 两个事件都发：前端靠 confirm_required 建表单，靠 suspended 收尾 UI
                yield {
                    "type": "workflow_confirm_required",
                    "runId": snapshot.run_id,
                    "nodeId": node_id,
                    "nodeLabel": label,
                    "confirmForm": confirm_form,
                    "contentOffset": content_offset,
                }
                yield {
                    "type": "workflow_suspended",
                    "runId": snapshot.run_id,
                    "nodeId": node_id,
                    "nodeType": node_type,
                    "nodeLabel": label,
                    "payload": payload,
                    "contentOffset": content_offset,
                }
                return

            if result.finished or node_type == NODE_END:
                yield self._done(
                    state,
                    final_output if final_output is not None else state.variables.get("output"),
                )
                return

            next_id = graph.next_node(node_id, result.handle)
            if next_id is None:
                logger.warning("节点 %s 没有后继（handle=%s），流程结束", node_id, result.handle)
                yield self._done(
                    state,
                    final_output if final_output is not None else state.variables.get("output"),
                )
                return
            node_id = next_id

        yield self._done(state, final_output)
