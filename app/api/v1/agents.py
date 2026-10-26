"""Agent 管理接口（对齐前端 `src/api/agent.js` + `workflow.js` 的 agent 部分）。

**路由顺序是这里的头号坑**：
    /api/agents/workflow-examples     ← 静态路径，必须最先注册
    /api/agents/{agent_id}            ← 动态路径，必须在其后

FastAPI 按注册顺序匹配。`/{agent_id}` 先注册的话，`GET /api/agents/workflow-examples`
会被它吃掉 → `int("workflow-examples")` 失败 → 422，前端只看到"参数错误"。
（同样的坑在 `chat_sessions.py` 的 `tool-calls/{id}/result` 和
`model_providers.py` 的 `presets` 各踩过一次。）

工作流编辑器那 14 个接口（`/workflow/*`）**不在本文件**，见 `workflow.py`。
"""

from typing import Annotated, Any

from fastapi import APIRouter, Body, Query
from sqlalchemy import func, or_, select

from app.api.deps import CurrentUserId, DbSession
from app.models.entities import Agent, AgentVersion
from app.schemas.agent import AgentDetailOut, AgentOut
from app.schemas.agent_ext import AgentIn
from app.schemas.common import PageResult, Result
from app.services.agent_service import AgentService

router = APIRouter(prefix="/api/agents", tags=["agents"])

# 5 组关联绑定的字段名（config 里的 key）
_BINDING_FIELDS = {
    "knowledge": "knowledges",
    "tools": "tools",
    "mcp-servers": "mcpServers",
    "subagents": "subagents",
    "skills": "skills",
}


def _svc(db: DbSession) -> AgentService:
    return AgentService(db)


# ======================================================================
# 静态路径（必须最先注册）
# ======================================================================


@router.get("/workflow-examples", response_model=Result[list[dict]], summary="内置示例工作流列表")
async def list_workflow_examples(user_id: CurrentUserId) -> Result[list[dict]]:
    """Java 侧是硬编码的示例清单。Python 版暂返回空数组 —— 前端有兜底
    （`examples || []` 空列表只是"从示例创建"里没有可选项，不影响已有 Agent）。
    """
    return Result.ok([])


@router.post("/workflow-examples/{key}", response_model=Result[AgentDetailOut],
             summary="从示例创建工作流 Agent（未实现示例库）")
async def create_from_example(key: str, user_id: CurrentUserId) -> Result[AgentDetailOut]:
    return Result.fail(f"Python 版尚未内置示例工作流（key={key}）", code=400)


# ======================================================================
# 列表 / 创建 / 更新
# ======================================================================


@router.get("", response_model=Result[PageResult[AgentOut]], summary="分页查询 Agent 列表")
async def list_agents(
    db: DbSession,
    user_id: CurrentUserId,
    page_num: int = Query(1, alias="pageNum"),
    page_size: int = Query(50, alias="pageSize"),
    name: str | None = Query(None),
    agent_type: str | None = Query(None, alias="agentType"),
    include_default: bool = Query(True, alias="includeDefault"),
) -> Result[PageResult[AgentOut]]:
    """**必须过滤 `deleted == 0`**。

    原来只过滤 `status != 'archived'`，但软删是置 `deleted=1`、`status` 不动 ——
    删掉的 Agent 会照样出现在列表里，点进去详情 404。
    """
    conditions = [Agent.deleted == 0]
    if name:
        like = f"%{name}%"
        conditions.append(or_(Agent.name.ilike(like), Agent.description.ilike(like)))
    if agent_type:
        conditions.append(Agent.agent_type == agent_type)

    total = (await db.execute(select(func.count()).select_from(Agent).where(*conditions))).scalar()
    rows = (
        await db.execute(
            select(Agent)
            .where(*conditions)
            .order_by(Agent.is_default.desc().nulls_last(), Agent.update_time.desc().nulls_last())
            .offset((page_num - 1) * page_size)
            .limit(page_size)
        )
    ).scalars().all()

    return Result.ok(
        PageResult.of(
            records=[AgentOut.model_validate(a) for a in rows],
            total=int(total or 0),
            current=page_num,
            size=page_size,
        )
    )


@router.post("", response_model=Result[AgentDetailOut], summary="创建 Agent")
async def create_agent(dto: AgentIn, db: DbSession, user_id: CurrentUserId) -> Result[AgentDetailOut]:
    return Result.ok(await _svc(db).create(dto, user_id))


@router.put("", response_model=Result[AgentDetailOut], summary="更新 Agent")
async def update_agent(dto: AgentIn, db: DbSession, user_id: CurrentUserId) -> Result[AgentDetailOut]:
    """`config` 是**合并**不是替换 —— 见 `agent_service.update` 的注释。"""
    return Result.ok(await _svc(db).update(dto))


# ======================================================================
# 详情 / 删除 / 克隆 / 默认
# ======================================================================


@router.get("/{agent_id}", response_model=Result[AgentDetailOut], summary="获取 Agent 详情")
async def get_agent(agent_id: int, db: DbSession, user_id: CurrentUserId) -> Result[AgentDetailOut]:
    return Result.ok(await _svc(db).get(agent_id))


@router.delete("/{agent_id}", response_model=Result[None], summary="删除 Agent（级联版本和会话）")
async def delete_agent(agent_id: int, db: DbSession, user_id: CurrentUserId) -> Result[None]:
    await _svc(db).delete(agent_id)
    return Result.ok(None)


@router.post("/{agent_id}/clone", response_model=Result[AgentDetailOut], summary="克隆 Agent")
async def clone_agent(agent_id: int, db: DbSession, user_id: CurrentUserId) -> Result[AgentDetailOut]:
    return Result.ok(await _svc(db).clone(agent_id, user_id))


@router.put("/{agent_id}/default", response_model=Result[None], summary="设为默认 Agent")
async def set_default(agent_id: int, db: DbSession, user_id: CurrentUserId) -> Result[None]:
    await _svc(db).set_default(agent_id)
    return Result.ok(None)


# ======================================================================
# 关联绑定（5 组 × GET/PUT，逻辑完全一样）
# ======================================================================


@router.get("/{agent_id}/knowledge", response_model=Result[list[str]], summary="绑定的知识库 ID")
async def get_knowledge(agent_id: int, db: DbSession, user_id: CurrentUserId) -> Result[list[str]]:
    return Result.ok(await _svc(db).get_bindings(agent_id, _BINDING_FIELDS["knowledge"]))


@router.put("/{agent_id}/knowledge", response_model=Result[None], summary="更新知识库绑定")
async def put_knowledge(
    agent_id: int,
    db: DbSession,
    user_id: CurrentUserId,
    body: Annotated[Any, Body()] = None,
) -> Result[None]:
    """body 是**裸数组** `["id1","id2"]`（对齐 Java `@RequestBody List<String>`）。"""
    await _svc(db).update_bindings(agent_id, _BINDING_FIELDS["knowledge"], body)
    return Result.ok(None)


@router.get("/{agent_id}/tools", response_model=Result[list[str]], summary="绑定的工具 ID")
async def get_tools(agent_id: int, db: DbSession, user_id: CurrentUserId) -> Result[list[str]]:
    return Result.ok(await _svc(db).get_bindings(agent_id, _BINDING_FIELDS["tools"]))


@router.put("/{agent_id}/tools", response_model=Result[None], summary="更新工具绑定")
async def put_tools(
    agent_id: int,
    db: DbSession,
    user_id: CurrentUserId,
    body: Annotated[Any, Body()] = None,
) -> Result[None]:
    await _svc(db).update_bindings(agent_id, _BINDING_FIELDS["tools"], body)
    return Result.ok(None)


@router.get("/{agent_id}/tools/detail", response_model=Result[list[dict]], summary="绑定的工具详情")
async def get_tools_detail(agent_id: int, db: DbSession, user_id: CurrentUserId) -> Result[list[dict]]:
    return Result.ok(await _svc(db).tool_details(agent_id))


@router.get("/{agent_id}/mcp-servers", response_model=Result[list[str]], summary="绑定的 MCP ID")
async def get_mcp(agent_id: int, db: DbSession, user_id: CurrentUserId) -> Result[list[str]]:
    return Result.ok(await _svc(db).get_bindings(agent_id, _BINDING_FIELDS["mcp-servers"]))


@router.put("/{agent_id}/mcp-servers", response_model=Result[None], summary="更新 MCP 绑定")
async def put_mcp(
    agent_id: int,
    db: DbSession,
    user_id: CurrentUserId,
    body: Annotated[Any, Body()] = None,
) -> Result[None]:
    await _svc(db).update_bindings(agent_id, _BINDING_FIELDS["mcp-servers"], body)
    return Result.ok(None)


@router.get("/{agent_id}/mcp-servers/detail", response_model=Result[list[dict]],
            summary="绑定的 MCP 详情")
async def get_mcp_detail(agent_id: int, db: DbSession, user_id: CurrentUserId) -> Result[list[dict]]:
    return Result.ok(await _svc(db).mcp_server_details(agent_id))


@router.get("/{agent_id}/subagents", response_model=Result[list[str]], summary="绑定的 SubAgent ID")
async def get_subagents(agent_id: int, db: DbSession, user_id: CurrentUserId) -> Result[list[str]]:
    return Result.ok(await _svc(db).get_bindings(agent_id, _BINDING_FIELDS["subagents"]))


@router.put("/{agent_id}/subagents", response_model=Result[None], summary="更新 SubAgent 绑定")
async def put_subagents(
    agent_id: int,
    db: DbSession,
    user_id: CurrentUserId,
    body: Annotated[Any, Body()] = None,
) -> Result[None]:
    await _svc(db).update_bindings(agent_id, _BINDING_FIELDS["subagents"], body)
    return Result.ok(None)


@router.get("/{agent_id}/skills", response_model=Result[list[str]], summary="绑定的 Skill ID")
async def get_skills(agent_id: int, db: DbSession, user_id: CurrentUserId) -> Result[list[str]]:
    return Result.ok(await _svc(db).get_bindings(agent_id, _BINDING_FIELDS["skills"]))


@router.put("/{agent_id}/skills", response_model=Result[None], summary="更新 Skill 绑定")
async def put_skills(
    agent_id: int,
    db: DbSession,
    user_id: CurrentUserId,
    body: Annotated[Any, Body()] = None,
) -> Result[None]:
    await _svc(db).update_bindings(agent_id, _BINDING_FIELDS["skills"], body)
    return Result.ok(None)


# ======================================================================
# 对话能力 / @ 候选
# ======================================================================


@router.get("/{agent_id}/chat-capabilities", response_model=Result[dict], summary="对话能力")
async def chat_capabilities(
    agent_id: int,
    db: DbSession,
    user_id: CurrentUserId,
    config_version: int | None = Query(None, alias="configVersion"),
) -> Result[dict]:
    return Result.ok(await _svc(db).chat_capabilities(agent_id))


@router.get("/{agent_id}/mention-options", response_model=Result[dict], summary="@ 候选资源")
async def mention_options(
    agent_id: int,
    db: DbSession,
    user_id: CurrentUserId,
    agent_version_id: str | None = Query(None, alias="agentVersionId"),
    types: str | None = Query(None),
) -> Result[dict]:
    return Result.ok(await _svc(db).mention_options(agent_id))


# ======================================================================
# 版本
# ======================================================================


@router.get("/{agent_id}/versions", response_model=Result[dict], summary="已发布版本列表")
async def list_versions(agent_id: int, db: DbSession, user_id: CurrentUserId) -> Result[dict]:
    """返回 `{versions, draftVersionId}`。

    前端 `loadAgentConfigVersions` 读的是 `payload.versions || []`，
    所以列出真实版本后它会自动出现下拉项；没有已发布版本时就是空列表（降级为只有草稿）。
    """
    rows = (
        (
            await db.execute(
                select(AgentVersion)
                .where(AgentVersion.agent_id == agent_id, AgentVersion.deleted == 0)
                .order_by(AgentVersion.version.desc())
            )
        )
        .scalars()
        .all()
    )
    versions = [
        {
            "id": str(v.id),
            "version": v.version,
            "status": v.status,
            "description": v.description,
            "nodeCount": v.node_count,
            "edgeCount": v.edge_count,
            "publishTime": v.publish_time,
            "createTime": v.create_time,
        }
        for v in rows
    ]
    draft = next((v["id"] for v in versions if v["status"] == "draft"), None)
    return Result.ok({"versions": versions, "draftVersionId": draft})


@router.get("/{agent_id}/versions/{version}", response_model=Result[dict], summary="版本配置详情")
async def get_version(
    agent_id: int, version: int, db: DbSession, user_id: CurrentUserId
) -> Result[dict]:
    return Result.ok(await _svc(db).get_version(agent_id, version))


@router.post("/{agent_id}/versions/{version}/restore", response_model=Result[None],
             summary="恢复到某个已发布版本")
async def restore_version(
    agent_id: int, version: int, db: DbSession, user_id: CurrentUserId
) -> Result[None]:
    await _svc(db).restore_version(agent_id, version)
    return Result.ok(None)


@router.delete("/{agent_id}/versions/{version}", response_model=Result[None], summary="删除某个版本")
async def delete_version(
    agent_id: int, version: int, db: DbSession, user_id: CurrentUserId
) -> Result[None]:
    await _svc(db).delete_version(agent_id, version)
    return Result.ok(None)


# ======================================================================
# 以下接口保持原行为（详情接口，聊天页依赖）
# ======================================================================


@router.get(
    "/{agent_id}/detail",
    response_model=Result[dict[str, AgentDetailOut]],
    summary="Agent 详情（外层包一层 agent，对齐前端 res.data.agent）",
)
async def get_agent_detail(agent_id: str, db: DbSession) -> Result[dict[str, AgentDetailOut]]:
    agent = (
        await db.execute(
            select(Agent).where(Agent.id == int(agent_id), Agent.deleted == 0)
        )
    ).scalar_one_or_none()
    if agent is None:
        return Result.fail("Agent 不存在", code=404)
    return Result.ok({"agent": AgentDetailOut.model_validate(agent)})
