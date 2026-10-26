"""HITL 验证：跑到 confirm 节点挂起 → 带表单数据恢复 → 完成。

流程（手工构造，不依赖库里有 confirm 节点的工作流）：
    start → variable(草稿) → confirm(人工确认) → llm(按意见生成) → output → end

验证三个关键点：
1. 挂起事件里带 runId + confirmForm（前端靠它渲染表单）
2. resume 时表单值写进变量池（下游 `{{approved}}` / `{{remark}}` 能取到）
3. 恢复后**从 next_node_id 继续**，confirm 节点不重跑

跑法：
    uv run python -m scripts.hitl_check
"""

from __future__ import annotations

import asyncio
import json

from app.workflow.executor import WorkflowExecutor
from app.workflow.graph import WorkflowGraph
from app.workflow.nodes import WorkflowContext
from app.workflow.runs import suspended_runs

PROVIDER_ID = "2090277031821160450"  # deepseek-v4
MODEL_ID = "deepseek-flash"

GRAPH = {
    "nodes": [
        {"id": "start_1", "type": "start", "data": {"label": "开始"}},
        {
            "id": "var_1",
            "type": "variable",
            "data": {
                "label": "准备草稿",
                "variableName": "draft",
                "variableValue": "多 Agent 系统的收益被高估了，真正起作用的是基础模型。",
            },
        },
        {
            "id": "confirm_1",
            "type": "confirm",
            "data": {
                "label": "人工确认",
                "message": "请审阅这段结论，确认是否继续润色",
                "formFields": [
                    {
                        "key": "approved",
                        "label": "是否通过",
                        "type": "radio",
                        "options": ["通过", "驳回"],
                        "required": True,
                    },
                    {
                        "key": "remark",
                        "label": "修改意见",
                        "type": "textarea",
                        "placeholder": "例如：补充数据来源",
                        "required": False,
                    },
                ],
            },
        },
        {
            "id": "llm_1",
            "type": "llm",
            "data": {
                "label": "按意见润色",
                "providerId": PROVIDER_ID,
                "modelId": MODEL_ID,
                "sysPrompt": "你是资深编辑，语言简洁有力。",
                "promptTemplate": (
                    "原始结论：{{draft}}\n\n"
                    "审核结果：{{approved}}\n"
                    "修改意见：{{remark}}\n\n"
                    "请据此把结论改写成一两句话。"
                ),
            },
        },
        {"id": "output_1", "type": "output", "data": {"label": "输出", "output": "{{llmOutput}}"}},
        {"id": "end_1", "type": "end", "data": {"label": "结束"}},
    ],
    "edges": [
        {"id": "e1", "source": "start_1", "target": "var_1", "sourceHandle": "out"},
        {"id": "e2", "source": "var_1", "target": "confirm_1", "sourceHandle": "out"},
        {"id": "e3", "source": "confirm_1", "target": "llm_1", "sourceHandle": "out"},
        {"id": "e4", "source": "llm_1", "target": "output_1", "sourceHandle": "out"},
        {"id": "e5", "source": "output_1", "target": "end_1", "sourceHandle": "out"},
    ],
}

FORM_DATA = {"approved": "通过", "remark": "补一句：多 Agent 更适合可并行的任务"}


async def main() -> None:
    graph = WorkflowGraph.parse(GRAPH)
    ctx = WorkflowContext(user_id=1, agent_id=0)
    executor = WorkflowExecutor(graph, ctx)

    print("=" * 70)
    print("阶段 1：执行到 confirm 节点")
    print("=" * 70)
    run_id: str | None = None
    async for ev in executor.run({}):
        t = ev.get("type")
        if t == "workflow_node_start":
            print(f"  ▶ {ev['nodeLabel']} ({ev['nodeType']})")
        elif t == "workflow_node_end":
            print(f"    ✅ handle={ev.get('handle')} suspended={ev.get('suspended')}")
        elif t == "workflow_confirm_required":
            run_id = ev["runId"]
            form = ev.get("confirmForm") or {}
            print("\n  ⏸  需要人工确认")
            print(f"     runId       = {run_id}")
            print(f"     message     = {form.get('message')}")
            print(f"     formFields  = {json.dumps(form.get('formFields'), ensure_ascii=False)}")
        elif t == "workflow_suspended":
            print(f"     suspended   = nodeId={ev.get('nodeId')} runId={ev.get('runId')}")
        elif t == "workflow_done":
            print(f"     ⚠️ 流程在挂起前就结束了: {ev}")

    if not run_id:
        print("❌ 没有产生挂起")
        return

    snapshot = suspended_runs.get(run_id)
    assert snapshot is not None
    print(f"\n  快照内容: 挂起节点={snapshot.suspend_node_id} 下一个={snapshot.next_node_id} "
          f"已执行={snapshot.step} 步 变量={list(snapshot.variables.keys())}")

    print()
    print("=" * 70)
    print(f"阶段 2：带表单恢复 {json.dumps(FORM_DATA, ensure_ascii=False)}")
    print("=" * 70)

    # 模拟 API 的行为：取出快照（一次性）
    suspended_runs.pop(run_id)
    executor2 = WorkflowExecutor(WorkflowGraph.parse(snapshot.graph_raw), ctx)

    output = None
    async for ev in executor2.resume(snapshot, FORM_DATA):
        t = ev.get("type")
        if t == "workflow_node_start":
            print(f"  ▶ {ev['nodeLabel']} ({ev['nodeType']})")
        elif t == "workflow_node_end":
            print(f"    ✅ {str(ev.get('output'))[:120]}")
        elif t == "workflow_done":
            output = ev.get("output")
            print(f"\n完成: steps={ev.get('steps')}")

    print("\n最终输出：")
    print(f"  {output}")

    print()
    print("=" * 70)
    print("校验")
    print("=" * 70)
    ok1 = isinstance(output, str) and len(output) > 10
    print(f"  {'✅' if ok1 else '❌'} 恢复后有实际产出")
    # 如果表单值没传进 LLM，模型会凭空编；传进去了才会提到"并行"
    ok2 = isinstance(output, str) and ("并行" in output or "补齐" in output or "补充" in output)
    print(f"  {'✅' if ok2 else '⚠️ '} LLM 输出体现了表单里的修改意见（关键词：并行/补充）")
    print(f"  {'✅' if suspended_runs.get(run_id) is None else '❌'} 快照已消费（不能重复恢复）")


if __name__ == "__main__":
    asyncio.run(main())
