"""工作流编排页的服务端逻辑：草稿 / 发布 / 校验 / 版本 / IO Schema。

对应 Java `WorkflowConfigServiceImpl` + `AgentVersionServiceImpl` 里工作流那一半。

**存储约定**（两处都存，别搞混）：

| 位置 | 内容 | 谁写 |
|---|---|---|
| `agent_version` where status='draft' and version=0 | 编辑中的图 | POST /workflow/draft 自动保存 |
| `agent_version` where status='published' and version>=1 | 每次发布的快照 | POST /workflow/publish |

`config` 的外层不是图本身，而是 `{"kind": "workflow", "graph": {...}}`。
Java 的 `extractWorkflowGraph` 兼容了"外层直接是图"的老数据，这里也兼容。

**Agent 级的三个字段跟着变**：`agent.version`（最新发布版本号）、
`agent.status`（draft / published / published_editing）、`agent.publish_time`。
`published_editing` 这个中间态是真实的语义：改过草稿但没重新发布，
前端顶栏会显示「有未发布的修改」。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.snowflake import new_id
from app.models.entities import Agent, AgentVersion
from app.workflow.graph import (
    NODE_API,
    NODE_BATCH,
    NODE_CLASSIFIER,
    NODE_CONDITION,
    NODE_CONFIRM,
    NODE_END,
    NODE_INPUT,
    NODE_LLM,
    NODE_LOOP,
    NODE_MCP,
    NODE_OUTPUT,
    NODE_RETRIEVAL,
    NODE_SCRIPT,
    NODE_START,
    NODE_TOOL,
    NODE_VARIABLE,
    NODE_VARIABLE_HANDLE,
)

logger = logging.getLogger(__name__)

KIND_WORKFLOW = "workflow"
STATUS_DRAFT = "draft"
STATUS_PUBLISHED = "published"
STATUS_PUBLISHED_EDITING = "published_editing"

# ----------------------------------------------------------------------
# 节点类型分三档
# ----------------------------------------------------------------------
# Java `NodeType` 枚举的全部 23 种。出现在图里必须是其中之一，否则前端画布
# 根本渲染不出来 —— 那是数据坏了，不是"Python 没实现"。
ALL_NODE_TYPES = {
    NODE_START,
    NODE_END,
    NODE_INPUT,
    NODE_OUTPUT,
    NODE_LLM,
    NODE_RETRIEVAL,
    NODE_TOOL,
    NODE_SCRIPT,
    NODE_API,
    NODE_VARIABLE,
    NODE_VARIABLE_HANDLE,
    NODE_CONDITION,
    NODE_CONFIRM,
    NODE_CLASSIFIER,
    NODE_LOOP,
    NODE_BATCH,
    NODE_MCP,
    # Java 里还有这几个，Python 侧统一按"未实现"处理
    "app_component",
    "parameter_extractor",
    "llm_classifier",
    "http",
    "delay",
}

# Python 执行器**已实现**的类型。不在这个集合里但合法的类型，发布时会明确
# 告警 —— 发布一个跑不了的图，等于把"节点报错"推迟到用户聊天那一刻。
IMPLEMENTED_NODE_TYPES = {
    NODE_START,
    NODE_END,
    NODE_INPUT,
    NODE_OUTPUT,
    NODE_LLM,
    NODE_RETRIEVAL,
    NODE_TOOL,
    NODE_SCRIPT,
    NODE_API,
    NODE_VARIABLE,
    NODE_VARIABLE_HANDLE,
    NODE_CONDITION,
    NODE_CONFIRM,
}

# 允许多条出边的类型：分支节点天然多出；loop/batch 是并行语义；
# end 后面不连任何东西，但历史数据里偶尔有残留边。
MULTI_OUT_ALLOWED = {NODE_CONDITION, NODE_CLASSIFIER, NODE_LOOP, NODE_BATCH, NODE_END}

# 非 start 节点必须至少连一条边
NO_CONNECTION_EXEMPT = {NODE_START}

NODE_TYPE_LABELS = {
    NODE_START: "开始",
    NODE_END: "结束",
    NODE_INPUT: "输入",
    NODE_OUTPUT: "输出",
    NODE_LLM: "大模型",
    NODE_RETRIEVAL: "知识检索",
    NODE_TOOL: "工具",
    NODE_SCRIPT: "代码执行",
    NODE_API: "HTTP 请求",
    NODE_VARIABLE: "变量赋值",
    NODE_VARIABLE_HANDLE: "变量处理",
    NODE_CONDITION: "条件分支",
    NODE_CONFIRM: "人工确认",
    NODE_CLASSIFIER: "意图分类",
    NODE_LOOP: "循环",
    NODE_BATCH: "批处理",
    NODE_MCP: "MCP 工具",
}


def node_label(node: dict[str, Any]) -> str:
    data = node.get("data") if isinstance(node.get("data"), dict) else {}
    label = str(data.get("label") or "").strip()
    if label:
        return label
    return NODE_TYPE_LABELS.get(str(node.get("type") or ""), str(node.get("id") or "节点"))


# ----------------------------------------------------------------------
# 校验（纯函数，不查库 —— 除了子流程引用那条，由调用方注入 agent 查询）
# ----------------------------------------------------------------------
def validate_graph(
    graph: dict[str, Any] | None,
    *,
    agent_id: int | None = None,
    sub_workflow_checker: Any = None,
) -> list[str]:
    """返回错误列表（空数组 = 通过）。前端会把每条都展示出来。

    **为什么不只是"看起来对不对"**：这些规则里有几条是踩过坑的：
    - condition 缺 `out_c` 默认路径 → 所有条件都不成立时，流程**卡死在那里**，
      既不报错也不结束，用户看到的是"一直转圈"。
    - 非分支节点多条出边 → 引擎只沿第一条走，第二条上的节点**永远不执行**，
      但画布上是连着的，用户以为会并行。
    - 环路 → 执行器靠 `MAX_STEPS` 兜底，跑满 50 步才停，烧掉大量 token 后
      报一个"步数超限"，看不出是哪条边回指造成的。
    """
    errors: list[str] = []
    if not graph or not (graph.get("nodes") or []):
        return ["工作流节点为空"]

    nodes: list[dict[str, Any]] = list(graph.get("nodes") or [])
    edges: list[dict[str, Any]] = list(graph.get("edges") or [])

    start_ids = [n for n in nodes if str(n.get("type")) == NODE_START]
    if not start_ids:
        errors.append("缺少开始节点")
    elif len(start_ids) > 1:
        errors.append("只能有一个开始节点")

    if not any(str(n.get("type")) == NODE_END for n in nodes):
        errors.append("缺少结束节点")

    connected: set[str] = set()
    for edge in edges:
        for key in ("source", "target"):
            value = edge.get(key)
            if value is not None:
                connected.add(str(value))

    node_ids = {str(n.get("id")) for n in nodes if n.get("id") is not None}

    for node in nodes:
        node_type = str(node.get("type") or "")
        node_id = str(node.get("id") or "")
        data = node.get("data") if isinstance(node.get("data"), dict) else {}

        if node_type not in NO_CONNECTION_EXEMPT and node_id not in connected:
            errors.append(f"节点未连接: {node_id}")

        if node_type == NODE_LLM:
            if not data.get("providerId"):
                errors.append(f"LLM节点未选择提供商: {node_id}")
            if not data.get("modelId"):
                errors.append(f"LLM节点未选择模型: {node_id}")
        elif node_type == NODE_RETRIEVAL and not data.get("knowledgeId"):
            errors.append(f"知识检索节点未选择知识库: {node_id}")
        elif node_type == NODE_TOOL and not data.get("toolId"):
            errors.append(f"工具节点未选择工具: {node_id}")
        elif node_type == NODE_CONDITION:
            has_default = any(
                str(e.get("source")) == node_id
                and str(e.get("sourceHandle")) == "out_c"
                for e in edges
            )
            if not has_default:
                errors.append(f"条件分支节点缺少默认路径: {node_id}")
        elif node_type == "app_component" and sub_workflow_checker is not None:
            errors.extend(
                sub_workflow_checker(node_id, data.get("componentCode"), agent_id, errors)
            )

        if node_type not in ALL_NODE_TYPES:
            errors.append(f"未知节点类型: {node_type}")
        elif node_type not in IMPLEMENTED_NODE_TYPES:
            # 刻意当**错误**而不是警告：前端只有 errors 一个通道，混进警告就等于
            # 允许发布。而发布出去之后，用户聊天时会在那个节点上报"尚未实现"，
            # 那时再回头查是哪一步，比现在拦住要贵得多。
            errors.append(
                f"节点「{node_label(node)}」的类型 {node_type} 尚未在 Python 版实现"
                f"（执行到该节点会中止）: {node_id}"
            )

    # 多出边
    out_counts: dict[str, int] = {}
    for edge in edges:
        src = edge.get("source")
        if src is None:
            continue
        out_counts[str(src)] = out_counts.get(str(src), 0) + 1
    node_by_id = {str(n.get("id")): n for n in nodes if n.get("id") is not None}
    for src, count in out_counts.items():
        if count <= 1:
            continue
        src_type = str((node_by_id.get(src) or {}).get("type") or "")
        if src_type in MULTI_OUT_ALLOWED:
            continue
        label = node_label(node_by_id.get(src) or {"id": src})
        errors.append(
            f"「{label}」有 {count} 条出边"
            "：当前引擎仅会沿第一条出边继续执行，不会并行跑多条分支。"
            "请删除多余连线，或改用「条件分支 / 意图分类」节点。"
        )

    # 环路
    cycle = _detect_cycle(node_ids, edges)
    if cycle:
        errors.append(f"工作流存在环路: {cycle}")

    return errors


def _detect_cycle(node_ids: set[str], edges: list[dict[str, Any]]) -> str | None:
    """DFS 找环，返回一条能看懂的环路径（`a → b → a`）。

    **只报"存在环"没用**：用户看不出是哪几个节点首尾相连，
    画布上 20 个节点根本找不过来。所以带上路径。
    """
    adj: dict[str, list[str]] = {}
    for edge in edges:
        src, tgt = edge.get("source"), edge.get("target")
        if src is None or tgt is None:
            continue
        adj.setdefault(str(src), []).append(str(tgt))

    WHITE, GRAY, BLACK = 0, 1, 2
    color = dict.fromkeys(node_ids, WHITE)
    parent: dict[str, str] = {}

    def dfs(node: str) -> str | None:
        color[node] = GRAY
        for nxt in adj.get(node, []):
            if nxt not in color:
                continue
            if color[nxt] == GRAY:
                # 回溯出一条路径
                path = [nxt]
                cur = node
                while cur != nxt and cur in parent:
                    path.append(cur)
                    cur = parent[cur]
                path.append(nxt if cur == nxt else cur)
                return " → ".join(reversed(path))
            if color[nxt] == WHITE:
                parent[nxt] = node
                found = dfs(nxt)
                if found:
                    return found
        color[node] = BLACK
        return None

    for nid in list(node_ids):
        if color.get(nid) == WHITE:
            found = dfs(nid)
            if found:
                return found
    return None


def build_io_schema(graph: dict[str, Any] | None) -> dict[str, Any]:
    """从图里提子流程的输入/输出签名（供 app_component 节点做参数映射）。

    `query` / `input` 永远在输入里 —— 子流程被调用时，父流程至少要能传这两样。
    其余输入来自 `input` 节点的 `outputParams`（前端两种驼峰/下划线写法都有）。
    """
    input_keys: list[str] = ["query", "input"]
    for node in (graph or {}).get("nodes") or []:
        if str(node.get("type")) != NODE_INPUT:
            continue
        data = node.get("data") if isinstance(node.get("data"), dict) else {}
        for field in ("outputParams", "output_params"):
            params = data.get(field)
            if not isinstance(params, list):
                continue
            for row in params:
                if not isinstance(row, dict):
                    continue
                key = str(row.get("key") or "").strip()
                if key and key not in input_keys:
                    input_keys.append(key)

    return {
        "inputs": [{"key": k, "type": "String"} for k in input_keys],
        "outputs": [{"key": "result", "type": "String", "description": "工作流最终输出"}],
    }


# ----------------------------------------------------------------------
# Service
# ----------------------------------------------------------------------
class WorkflowEditorService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # -- 基础查询 -------------------------------------------------------

    async def _require_agent(self, agent_id: int) -> Agent:
        agent = (
            await self.db.execute(
                select(Agent).where(Agent.id == agent_id, Agent.deleted == 0)
            )
        ).scalar_one_or_none()
        if agent is None:
            from fastapi import HTTPException, status

            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent 不存在")
        return agent

    async def _draft_row(self, agent_id: int) -> AgentVersion | None:
        return (
            await self.db.execute(
                select(AgentVersion)
                .where(
                    AgentVersion.agent_id == agent_id,
                    AgentVersion.status == STATUS_DRAFT,
                    AgentVersion.deleted == 0,
                )
                .order_by(AgentVersion.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    async def _published_row(
        self, agent_id: int, version: int | None = None
    ) -> AgentVersion | None:
        stmt = select(AgentVersion).where(
            AgentVersion.agent_id == agent_id,
            AgentVersion.status == STATUS_PUBLISHED,
            AgentVersion.deleted == 0,
        )
        if version is None:
            stmt = stmt.order_by(AgentVersion.version.desc())
        else:
            stmt = stmt.where(AgentVersion.version == version)
        return (await self.db.execute(stmt.limit(1))).scalar_one_or_none()

    @staticmethod
    def _extract_graph(row: AgentVersion | None) -> dict[str, Any] | None:
        """从 `agent_version.config` 里挖出图。

        新版是 `{"kind":"workflow","graph":{...}}`，早期直接存图。
        两种都要认 —— 库里有 4 个 agent 的版本是早期格式。
        """
        if row is None or not row.config:
            return None
        cfg = row.config
        if isinstance(cfg.get("graph"), dict):
            return cfg["graph"]
        if isinstance(cfg.get("nodes"), list):
            return cfg
        return None

    async def load_definition(
        self, agent_id: int, *, use_draft: bool = False
    ) -> dict[str, Any] | None:
        """取运行时用的图。

        **语义照抄 Java `loadWorkflowDefinition`**：
        - `use_draft=True`  → 只用草稿
        - `use_draft=False` → 优先最新发布版，**没有发布版才回退草稿**

        回退这条很关键：新建的工作流一次都没发布过，草稿就是它唯一的图；
        不回退的话，用户画完一保存就去聊天，会被告知"没有可运行的工作流"。

        （原 `chat_stream_service` 里自己写了一段 `node_count > 0 ORDER BY
        version DESC` 的 SQL，用 `node_count` 当"有没有图"的判据 —— 但那是
        缓存字段，历史版本的 node_count 普遍是 0，等于把有效的发布版筛掉了。
        统一走这里。）
        """
        if use_draft:
            return self._extract_graph(await self._draft_row(agent_id))
        graph = self._extract_graph(await self._published_row(agent_id))
        if graph is None:
            graph = self._extract_graph(await self._draft_row(agent_id))
        return graph

    # -- 编辑器状态 -----------------------------------------------------

    async def get_editor_state(self, agent_id: int) -> dict[str, Any]:
        agent = await self._require_agent(agent_id)
        draft = await self._draft_row(agent_id)
        published = await self._published_row(agent_id)

        state: dict[str, Any] = {
            "draft": self._extract_graph(draft),
            "published": self._extract_graph(published),
            "publishedVersion": agent.version or 0,
            "status": agent.status or STATUS_DRAFT,
        }
        if draft is not None:
            state["draftVersionId"] = str(draft.id)
        return state

    # -- 草稿 -----------------------------------------------------------

    async def save_draft(self, agent_id: int, graph: dict[str, Any] | None) -> None:
        """存草稿，**不校验**（用户画到一半也要能存）。

        副作用：已发布的 agent 存完草稿变 `published_editing` ——
        前端顶栏据此显示"有未发布的修改"。
        """
        agent = await self._require_agent(agent_id)
        await self._upsert_draft(agent, graph)
        await self.db.commit()

    async def _upsert_draft(self, agent: Agent, graph: dict[str, Any] | None) -> None:
        """写草稿行 + 状态迁移，**不提交**（由调用方决定事务边界）。"""
        now = datetime.now()
        snapshot = self._snapshot(graph)
        node_count = len((graph or {}).get("nodes") or [])
        edge_count = len((graph or {}).get("edges") or [])

        draft = await self._draft_row(int(agent.id))
        if draft is None:
            self.db.add(
                AgentVersion(
                    id=new_id(),
                    agent_id=int(agent.id),
                    user_id=agent.user_id,
                    version=0,
                    status=STATUS_DRAFT,
                    config=snapshot,
                    node_count=node_count,
                    edge_count=edge_count,
                    create_time=now,
                    update_time=now,
                    deleted=0,
                )
            )
        else:
            draft.config = snapshot
            draft.node_count = node_count
            draft.edge_count = edge_count
            draft.update_time = now

        if (agent.status or "") == STATUS_PUBLISHED:
            agent.status = STATUS_PUBLISHED_EDITING
            agent.update_time = now

    # -- 发布 -----------------------------------------------------------

    async def publish(self, agent_id: int, graph: dict[str, Any] | None) -> dict[str, Any]:
        """发布。**先校验，有错直接 400** —— 前端只展示 message，所以拼成一句话。

        整个发布是**一次提交**：发布版快照、草稿同步、agent 的版本号/状态
        三处要么全成功要么全回滚。

        第一版里这里调了 `save_draft()`，而它内部自己 commit —— 结果发布版
        行先被提交、agent.version 还没改。这时如果进程崩了，库里有 v24 的
        快照但 agent 说自己在 v23；下次发布会再写一行 v24，列表里就出现两个
        同版本号且都标不了"当前"。事务边界必须收在一处。
        """
        from fastapi import HTTPException, status

        errors = validate_graph(graph, agent_id=agent_id)
        if errors:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="；".join(errors)
            )

        agent = await self._require_agent(agent_id)
        now = datetime.now()
        next_version = (agent.version or 0) + 1

        self.db.add(
            AgentVersion(
                id=new_id(),
                agent_id=agent_id,
                user_id=agent.user_id,
                version=next_version,
                status=STATUS_PUBLISHED,
                config=self._snapshot(graph),
                node_count=len((graph or {}).get("nodes") or []),
                edge_count=len((graph or {}).get("edges") or []),
                description=str((graph or {}).get("publishDescription") or "").strip() or None,
                publish_time=now,
                create_time=now,
                update_time=now,
                deleted=0,
            )
        )

        # 草稿同步成刚发布的内容 —— 否则用户发布后草稿还是旧的，会以为没生效
        # 再点一次，白涨一个版本号。（这里 agent.status 还是发布前的值，
        # 所以不会被 _upsert_draft 改成 published_editing。）
        await self._upsert_draft(agent, graph)

        agent.version = next_version
        agent.status = STATUS_PUBLISHED
        agent.publish_time = now
        agent.update_time = now
        await self.db.commit()

        return {"version": next_version, "status": STATUS_PUBLISHED}

    # -- 版本 -----------------------------------------------------------

    async def list_versions(self, agent_id: int) -> list[dict[str, Any]]:
        agent = await self._require_agent(agent_id)
        rows = (
            await self.db.execute(
                select(AgentVersion)
                .where(
                    AgentVersion.agent_id == agent_id,
                    AgentVersion.status == STATUS_PUBLISHED,
                    AgentVersion.deleted == 0,
                )
                .order_by(AgentVersion.version.desc())
            )
        ).scalars().all()

        draft = await self._draft_row(agent_id)
        current = agent.version or 0
        return [
            {
                "id": str(row.id),
                "version": row.version,
                "publishedAt": row.publish_time,
                "nodeCount": row.node_count or 0,
                "edgeCount": row.edge_count or 0,
                "current": row.version == current,
                "description": row.description,
                "draftVersionId": str(draft.id) if draft else None,
            }
            for row in rows
        ]

    async def get_version_graph(self, agent_id: int, version: int) -> dict[str, Any]:
        from fastapi import HTTPException, status

        row = await self._published_row(agent_id, version)
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=f"版本 v{version} 不存在"
            )
        graph = self._extract_graph(row) or {}
        # 前端拿它直接渲染画布，所以要带 publishedVersion 让它知道在看历史
        out = dict(graph)
        out.setdefault("publishedVersion", version)
        return out

    async def restore_version(self, agent_id: int, version: int) -> None:
        """把历史版本**写回草稿**（不动已发布的部分）。

        注意语义：这只改草稿，用户还要再点一次"发布"才真正生效。
        Java 也是这个行为（`restorePublishedToDraft`）。
        """
        graph = await self.get_version_graph(agent_id, version)
        graph.pop("publishedVersion", None)
        await self.save_draft(agent_id, graph)

    # -- IO Schema ------------------------------------------------------

    async def get_io_schema(self, agent_id: int) -> dict[str, Any]:
        from fastapi import HTTPException, status

        agent = await self._require_agent(agent_id)
        if (agent.agent_type or "") != "workflow":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="目标 Agent 不是工作流类型"
            )
        if not agent.version:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="工作流尚未发布"
            )
        graph = await self.load_definition(agent_id, use_draft=False)
        if not graph or not graph.get("nodes"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="工作流定义为空"
            )
        schema = build_io_schema(graph)
        schema["agentId"] = str(agent_id)
        schema["agentName"] = agent.name
        schema["publishedVersion"] = agent.version
        return schema

    # -- 内部 -----------------------------------------------------------

    @staticmethod
    def _snapshot(graph: dict[str, Any] | None) -> dict[str, Any]:
        """包成 `{"kind":"workflow","graph":{...}}`，顺手剥掉画布专用的字段。"""
        g = dict(graph or {})
        g.pop("publishedVersion", None)
        wrapped: dict[str, Any] = {
            "nodes": g.get("nodes") or [],
            "edges": g.get("edges") or [],
        }
        if g.get("globalConfig") is not None:
            wrapped["globalConfig"] = g["globalConfig"]
        return {"kind": KIND_WORKFLOW, "graph": wrapped}
