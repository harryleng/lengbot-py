"""工作流内部事件 → 前端 SSE 事件的翻译层。

为什么需要这一层：执行器用的是 `_end` / `done` 这种内部命名，
前端（`useChatWorkflowStream.js` / `useWorkflowTestLive.js` 的
`WORKFLOW_SSE_EVENT_TYPES` 白名单）认的是 `workflow_node_complete` /
`workflow_complete`。名字对不上，**前端会静默丢弃**——不报错、不显示，
调试时只看到"点了运行什么也没发生"。

之前这段逻辑私有在 `ChatStreamService._to_sse_event` 里。编排页的调试流
也要同样翻译，与其复制一份，不如提出来 —— 两份实现迟早会漂移，
改了一处忘了另一处，就又变成"聊天能看、调试看不到"。

`success` 字段是必须补的：前端靠 `event.success === false` 判断节点失败
并给节点画红框，光有 `status` 它不认。
"""

from __future__ import annotations

from typing import Any

# 前端白名单里的事件类型（抄自 ui/src/api/workflowTestStream.js）
FRONTEND_EVENT_TYPES = frozenset(
    {
        "workflow_node_start",
        "workflow_node_complete",
        "workflow_node_retry",
        "workflow_node_failure",
        "workflow_complete",
        "workflow_llm_chunk",
        "workflow_confirm_required",
        "workflow_suspended",
        "error",
    }
)


def to_sse_event(ev: dict[str, Any]) -> dict[str, Any]:
    """内部事件 → 前端认的事件。非工作流事件原样返回。"""
    t = ev.get("type")
    if t == "workflow_node_end":
        out = dict(ev)
        out["type"] = "workflow_node_complete"
        out["success"] = ev.get("status") == "success"
        return out
    if t == "workflow_done":
        out = dict(ev)
        out["type"] = "workflow_complete"
        # 失败时前端读 `error`；成功时清掉，避免上一轮的残留值误导
        if not out.get("error"):
            out.pop("error", None)
        return out
    return ev
