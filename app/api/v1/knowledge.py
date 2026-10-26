"""知识库核心接口（对齐前端 `src/api/knowledge.js` 的前 30 个）。

## 路由顺序：这是本模块最容易踩的坑

FastAPI 按**注册顺序**匹配，`/knowledge/{knowledge_id}` 会吃掉
`/knowledge/documents/{doc_id}`、`/knowledge/qa-pairs/{id}` 这类路径中的
一部分（因为 `documents` 也能被解析成 `{knowledge_id}` → 转 int 失败 → 404）。

本项目已在 agents / tools / model-providers 上踩过 7 次同样的坑。
解决办法固定成两条：
1. **所有静态前缀段的路由先注册**（`/documents/...`、`/qa-pairs/...`、`/dify/...`）
2. 带 `{knowledge_id}` 的放最后

本文件只有 `/knowledge/{knowledge_id}` 和 `/knowledge/{knowledge_id}/xxx`，
静态前缀的在另外两个文件（`knowledge_docs.py` / `knowledge_qa.py`），
所以顺序上只要保证 **本文件 router 后 include**。
见 `main.py` 的 include 顺序。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Query

from app.api.deps import CurrentUserId, DbSession
from app.schemas.common import PageResult, Result
from app.schemas.knowledge import (
    AdvisorSummaryOut,
    IngestConfigOut,
    KnowledgeIn,
    KnowledgeMemberOut,
    KnowledgeOut,
    LowRatedChunkOut,
    SleepingChunkOut,
)
from app.services.knowledge_service import KnowledgeService

router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])


def _svc(db: DbSession, user_id: CurrentUserId) -> KnowledgeService:
    return KnowledgeService(db, user_id)


# ----------------------------------------------------------------------
# 静态前缀路由：必须写在 /{knowledge_id} 之前
# ----------------------------------------------------------------------


@router.post(
    "/dify/connection-test",
    response_model=Result[dict[str, Any]],
    summary="测试未保存的 Dify Dataset 连接",
)
async def test_dify_draft_connection(
    payload: Annotated[dict[str, Any], Body(...)],
    db: DbSession,
    user_id: CurrentUserId,
) -> Result[dict[str, Any]]:
    """测未保存的 Dify 配置。

    **注意这里返回的是明确失败而不是假装成功**：Dify 是外部服务，
    Python 版没有接它的 Dataset 客户端。返回 `{success: false, message: ...}`
    比返回 400 更好 —— 前端 `testDifyDraftConnection` 的调用方是按
    `res.data.success` 判断的，抛 400 会让整个表单弹错误提示。
    """
    api_url = str(payload.get("apiUrl") or payload.get("api_url") or "").strip()
    dataset_id = str(payload.get("datasetId") or payload.get("dataset_id") or "").strip()
    token = str(payload.get("token") or "").strip()

    if not api_url or not dataset_id or not token:
        return Result.ok(
            {
                "success": False,
                "message": "apiUrl / datasetId / token 三项都必填",
                "datasetName": None,
                "documentCount": 0,
            }
        )
    return Result.ok(
        {
            "success": False,
            "message": (
                "Python 版尚未接入 Dify Dataset 客户端，无法真实探测连接。"
                "配置可以保存，但检索/入库会走本地 pgvector 链路。"
            ),
            "datasetName": None,
            "documentCount": 0,
        }
    )


# ----------------------------------------------------------------------
# CRUD
# ----------------------------------------------------------------------


@router.get("", response_model=Result[PageResult[KnowledgeOut]], summary="分页查询我加入的知识库")
async def page(
    db: DbSession,
    user_id: CurrentUserId,
    page_num: int = Query(1, alias="pageNum"),
    page_size: int = Query(10, alias="pageSize"),
    name: str | None = Query(None),
) -> Result[PageResult[KnowledgeOut]]:
    return Result.ok(await _svc(db, user_id).page(page_num, page_size, name))


@router.post("", response_model=Result[KnowledgeOut], summary="创建知识库")
async def create(dto: KnowledgeIn, db: DbSession, user_id: CurrentUserId) -> Result[KnowledgeOut]:
    return Result.ok(await _svc(db, user_id).create(dto))


@router.put("", response_model=Result[KnowledgeOut], summary="更新知识库（需 MANAGER 及以上）")
async def update(dto: KnowledgeIn, db: DbSession, user_id: CurrentUserId) -> Result[KnowledgeOut]:
    return Result.ok(await _svc(db, user_id).update(dto))


@router.get("/{knowledge_id}", response_model=Result[KnowledgeOut], summary="获取知识库详情")
async def get_one(knowledge_id: int, db: DbSession, user_id: CurrentUserId) -> Result[KnowledgeOut]:
    return Result.ok(await _svc(db, user_id).get(knowledge_id))


@router.delete("/{knowledge_id}", response_model=Result[None], summary="删除知识库（仅 CREATOR）")
async def delete(knowledge_id: int, db: DbSession, user_id: CurrentUserId) -> Result[None]:
    await _svc(db, user_id).delete(knowledge_id)
    return Result.ok(None)


# ----------------------------------------------------------------------
# 成员
# ----------------------------------------------------------------------


@router.get(
    "/{knowledge_id}/members",
    response_model=Result[list[KnowledgeMemberOut]],
    summary="成员列表",
)
async def list_members(
    knowledge_id: int, db: DbSession, user_id: CurrentUserId
) -> Result[list[KnowledgeMemberOut]]:
    return Result.ok(await _svc(db, user_id).list_members(knowledge_id))


@router.post("/{knowledge_id}/members", response_model=Result[None], summary="添加成员")
async def add_member(
    knowledge_id: int,
    db: DbSession,
    user_id: CurrentUserId,
    target_user_id: int = Query(..., alias="userId"),
    role: str = Query("viewer"),
) -> Result[None]:
    """`userId` 是 query 参数（不是 body）—— 对齐 Java `@RequestParam`，
    前端 `addKnowledgeMember` 也是 `params: { userId, role }`。"""
    await _svc(db, user_id).add_member(knowledge_id, target_user_id, role)
    return Result.ok(None)


@router.put("/{knowledge_id}/members/{target_user_id}", response_model=Result[None], summary="修改成员角色")
async def update_member_role(
    knowledge_id: int,
    target_user_id: int,
    db: DbSession,
    user_id: CurrentUserId,
    role: str = Query(...),
) -> Result[None]:
    await _svc(db, user_id).update_member_role(knowledge_id, target_user_id, role)
    return Result.ok(None)


@router.delete("/{knowledge_id}/members/{target_user_id}", response_model=Result[None], summary="移除成员")
async def remove_member(
    knowledge_id: int, target_user_id: int, db: DbSession, user_id: CurrentUserId
) -> Result[None]:
    await _svc(db, user_id).remove_member(knowledge_id, target_user_id)
    return Result.ok(None)


# ----------------------------------------------------------------------
# 配置 / 统计
# ----------------------------------------------------------------------


@router.get(
    "/{knowledge_id}/default-ingest-config",
    response_model=Result[IngestConfigOut],
    summary="默认入库配置",
)
async def get_default_ingest_config(
    knowledge_id: int, db: DbSession, user_id: CurrentUserId
) -> Result[IngestConfigOut]:
    return Result.ok(await _svc(db, user_id).get_ingest_config(knowledge_id))


@router.get("/{knowledge_id}/query-params", response_model=Result[dict[str, Any]], summary="读检索配置")
async def get_query_params(
    knowledge_id: int, db: DbSession, user_id: CurrentUserId
) -> Result[dict[str, Any]]:
    return Result.ok(await _svc(db, user_id).get_query_params(knowledge_id))


@router.put("/{knowledge_id}/query-params", response_model=Result[dict[str, Any]], summary="保存检索配置")
async def update_query_params(
    knowledge_id: int,
    payload: Annotated[dict[str, Any], Body(...)],
    db: DbSession,
    user_id: CurrentUserId,
) -> Result[dict[str, Any]]:
    """返回归一化后的配置（Java 返回 `Result<Void>`）。

    这里返回值更实用：前端保存后能立刻看到"哪些字段被抠掉了"，
    不用再发一次 GET 才知道。
    """
    return Result.ok(await _svc(db, user_id).update_query_params(knowledge_id, payload))


@router.get("/{knowledge_id}/milvus-health", response_model=Result[dict[str, Any]], summary="Milvus 连接状态")
async def milvus_health(
    knowledge_id: int, db: DbSession, user_id: CurrentUserId
) -> Result[dict[str, Any]]:
    """Python 版没有 Milvus 客户端，如实返回不可用。

    **不能返回 `available: true` 图省事** —— 前端 `KnowledgeDetail.vue` 拿到
    true 后会把 `milvusConnected` 置为已连接，用户就以为能检索了，
    实际检索走 pgvector，两套数据不一致时很难查。
    """
    await _svc(db, user_id).ensure_member(knowledge_id)
    return Result.ok(
        {
            "available": False,
            "message": "Python 版只接了 pgvector，未接入 Milvus 客户端",
        }
    )


@router.post(
    "/{knowledge_id}/connection-test", response_model=Result[dict[str, Any]], summary="测试已保存的 Dify 连接"
)
async def test_dify_connection(
    knowledge_id: int, db: DbSession, user_id: CurrentUserId
) -> Result[dict[str, Any]]:
    await _svc(db, user_id).ensure_member(knowledge_id)
    return Result.ok(
        {
            "success": False,
            "message": "Python 版尚未接入 Dify Dataset 客户端，无法探测已保存的连接",
            "datasetName": None,
            "documentCount": 0,
        }
    )


@router.post("/{knowledge_id}/stats/refresh", response_model=Result[None], summary="全量重算统计")
async def refresh_stats(knowledge_id: int, db: DbSession, user_id: CurrentUserId) -> Result[None]:
    await _svc(db, user_id).refresh_stats(knowledge_id)
    return Result.ok(None)


# ----------------------------------------------------------------------
# 示例问题
# ----------------------------------------------------------------------


@router.get(
    "/{knowledge_id}/example-questions", response_model=Result[list[str]], summary="读示例问题"
)
async def get_example_questions(
    knowledge_id: int, db: DbSession, user_id: CurrentUserId
) -> Result[list[str]]:
    return Result.ok(await _svc(db, user_id).get_example_questions(knowledge_id))


@router.put("/{knowledge_id}/example-questions", response_model=Result[None], summary="保存示例问题")
async def update_example_questions(
    knowledge_id: int,
    questions: Annotated[list[str], Body(...)],
    db: DbSession,
    user_id: CurrentUserId,
) -> Result[None]:
    """body 是**裸数组** `["q1","q2"]`，不是 `{questions: [...]}`。

    前端 `updateExampleQuestions(knowledgeId, questions)` 直接把数组当 body。
    写成对象包一层 → 422 → 前端拦截器当成功 → 用户以为保存了。
    """
    await _svc(db, user_id).update_example_questions(knowledge_id, questions)
    return Result.ok(None)


@router.post(
    "/{knowledge_id}/example-questions/generate",
    response_model=Result[str],
    summary="AI 生成一条示例问题",
)
async def generate_one_example_question(
    knowledge_id: int, db: DbSession, user_id: CurrentUserId
) -> Result[str]:
    """需要 LLM。走 `example_question_service`，失败时返回空串而不是抛错。

    前端拿到空串会提示"生成失败，请重试"，比 500 更友好。
    """
    from app.services.example_question_service import generate_one

    await _svc(db, user_id).ensure_role(knowledge_id, "developer")
    question = await generate_one(db, knowledge_id)
    if question:
        existing = await _svc(db, user_id).get_example_questions(knowledge_id)
        if question not in existing:
            await _svc(db, user_id).update_example_questions(knowledge_id, [*existing, question])
    return Result.ok(question)


@router.post(
    "/{knowledge_id}/generate-questions",
    response_model=Result[None],
    summary="为所有已完成文档批量生成示例问题",
)
async def generate_questions(
    knowledge_id: int, db: DbSession, user_id: CurrentUserId
) -> Result[None]:
    """**同步执行**（Java 是异步丢线程池）。

    Python 版不搞后台线程：`asyncio.create_task` 在进程退出时会丢，
    而这批接口的调用频率极低（建库后手工点一次），同步等 10-30s 可接受，
    且错误能直接返回给用户。真要异步应该走 `task` 表 + 状态轮询，不是现在。
    """
    from app.services.example_question_service import generate_for_documents

    await _svc(db, user_id).ensure_role(knowledge_id, "developer")
    await generate_for_documents(db, knowledge_id)
    return Result.ok(None)


# ----------------------------------------------------------------------
# 思维导图
# ----------------------------------------------------------------------


@router.get("/{knowledge_id}/mindmap", response_model=Result[Any], summary="读思维导图")
async def get_mindmap(knowledge_id: int, db: DbSession, user_id: CurrentUserId) -> Result[Any]:
    return Result.ok(await _svc(db, user_id).get_mindmap(knowledge_id))


@router.post("/{knowledge_id}/mindmap", response_model=Result[Any], summary="AI 生成思维导图")
async def generate_mindmap(
    knowledge_id: int,
    db: DbSession,
    user_id: CurrentUserId,
    provider_id: int | None = Query(None, alias="providerId"),
) -> Result[Any]:
    from app.services.example_question_service import generate_mindmap as gen

    await _svc(db, user_id).ensure_role(knowledge_id, "developer")
    return Result.ok(await gen(db, knowledge_id, provider_id))


# ----------------------------------------------------------------------
# Advisor
# ----------------------------------------------------------------------


@router.get(
    "/{knowledge_id}/advisor/summary",
    response_model=Result[AdvisorSummaryOut],
    summary="反馈聚合概览",
)
async def advisor_summary(
    knowledge_id: int,
    db: DbSession,
    user_id: CurrentUserId,
    window_days: int = Query(14, alias="windowDays"),
) -> Result[AdvisorSummaryOut]:
    return Result.ok(await _svc(db, user_id).advisor_summary(knowledge_id, window_days))


@router.get(
    "/{knowledge_id}/advisor/low-rated-chunks",
    response_model=Result[list[LowRatedChunkOut]],
    summary="低分分块（按点踩数倒序）",
)
async def advisor_low_rated(
    knowledge_id: int,
    db: DbSession,
    user_id: CurrentUserId,
    limit: int = Query(10),
) -> Result[list[LowRatedChunkOut]]:
    return Result.ok(await _svc(db, user_id).advisor_low_rated(knowledge_id, limit))


@router.get(
    "/{knowledge_id}/advisor/sleeping-chunks",
    response_model=Result[list[SleepingChunkOut]],
    summary="休眠分块（最近 N 天未被引用）",
)
async def advisor_sleeping(
    knowledge_id: int,
    db: DbSession,
    user_id: CurrentUserId,
    days: int = Query(14),
    limit: int = Query(10),
) -> Result[list[SleepingChunkOut]]:
    return Result.ok(await _svc(db, user_id).advisor_sleeping(knowledge_id, days, limit))
