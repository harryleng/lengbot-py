"""工作流图解析。

数据来源：`agent_version.config.graph`
```json
{
  "nodes": [{"id": "start_1", "type": "start", "data": {"label": "开始"}}, ...],
  "edges": [{"id": "e_start", "source": "start_1", "target": "api_1",
             "sourceHandle": "out", "targetHandle": "in"}, ...]
}
```

对照 Java：`WorkflowExecutorService` 里那套节点/边遍历。

**`sourceHandle` 是条件分支的关键**：
普通节点走 `out`，`condition` 节点根据判断结果走 `out_a` / `out_b`。
所以「下一个节点是谁」必须由「当前节点 + 走哪个 handle」两个参数决定，
不能只看当前节点 —— 那会把条件分支走成一条直线。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# 节点类型（对齐 Java NodeType 枚举的 code）
NODE_START = "start"
NODE_END = "end"
NODE_INPUT = "input"
NODE_OUTPUT = "output"
NODE_LLM = "llm"
NODE_CONDITION = "condition"
NODE_RETRIEVAL = "retrieval"
NODE_TOOL = "tool"
NODE_SCRIPT = "script"
NODE_API = "api"
NODE_VARIABLE = "variable"
NODE_VARIABLE_HANDLE = "variable_handle"
NODE_CONFIRM = "confirm"
NODE_CLASSIFIER = "classifier"
NODE_LOOP = "loop"
NODE_BATCH = "batch"
NODE_MCP = "mcp"

DEFAULT_HANDLE = "out"


@dataclass(slots=True)
class Edge:
    id: str
    source: str
    target: str
    source_handle: str = DEFAULT_HANDLE
    target_handle: str = "in"


@dataclass(slots=True)
class WorkflowGraph:
    nodes: dict[str, dict[str, Any]] = field(default_factory=dict)
    edges: list[Edge] = field(default_factory=list)
    # (source, handle) → target，O(1) 查下一条边
    _route: dict[tuple[str, str], str] = field(default_factory=dict)
    # 原始 JSON。挂起时要把整张图存进快照 —— 恢复时不能依赖"重新查库"，
    # 因为库里的版本可能在等待确认期间被改过。
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def parse(cls, raw: dict[str, Any] | None) -> WorkflowGraph:
        if not raw:
            raise ValueError("工作流图为空")
        nodes: dict[str, dict[str, Any]] = {}
        for node in raw.get("nodes") or []:
            node_id = node.get("id")
            if node_id:
                nodes[str(node_id)] = node

        edges: list[Edge] = []
        route: dict[tuple[str, str], str] = {}
        for e in raw.get("edges") or []:
            src, tgt = e.get("source"), e.get("target")
            if not src or not tgt:
                continue
            edge = Edge(
                id=str(e.get("id") or f"{src}->{tgt}"),
                source=str(src),
                target=str(tgt),
                source_handle=str(e.get("sourceHandle") or DEFAULT_HANDLE),
                target_handle=str(e.get("targetHandle") or "in"),
            )
            edges.append(edge)
            route[(edge.source, edge.source_handle)] = edge.target

        if not nodes:
            raise ValueError("工作流没有节点")
        return cls(nodes=nodes, edges=edges, _route=route, raw=raw)

    # ------------------------------------------------------------------

    def node_type(self, node_id: str) -> str:
        return str(self.nodes.get(node_id, {}).get("type") or "")

    def node_data(self, node_id: str) -> dict[str, Any]:
        return dict(self.nodes.get(node_id, {}).get("data") or {})

    def entry_node(self) -> str:
        """入口节点：优先找 type=start，没有就找没有任何入边的节点。"""
        for node_id in self.nodes:
            if self.node_type(node_id) == NODE_START:
                return node_id
        targets = {e.target for e in self.edges}
        for node_id in self.nodes:
            if node_id not in targets:
                return node_id
        raise ValueError("找不到入口节点（没有 start 节点，也没有无入边的节点）")

    def next_node(self, node_id: str, handle: str = DEFAULT_HANDLE) -> str | None:
        """按 (当前节点, 走的 handle) 找下一个节点。

        先精确匹配 handle；找不到时退回 default `out` ——
        因为有些节点只连了一条边但没写 sourceHandle。
        """
        target = self._route.get((node_id, handle))
        if target is None and handle != DEFAULT_HANDLE:
            target = self._route.get((node_id, DEFAULT_HANDLE))
        return target

    def describe(self) -> str:
        lines = [f"图: {len(self.nodes)} 节点 / {len(self.edges)} 边"]
        for node_id, node in self.nodes.items():
            data = node.get("data") or {}
            label = data.get("label") or ""
            lines.append(f"  {node_id:<16} {node.get('type'):<12} {label}")
        return "\n".join(lines)
