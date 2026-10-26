"""问答对 8 接口 + 检索测试 1 接口。

路由（prefix `/api/knowledge`）：

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/{knowledge_id}/qa-pairs` | 新建（自动向量化） |
| GET | `/{knowledge_id}/qa-pairs` | 分页 + 关键字过滤 |
| POST | `/{knowledge_id}/qa-pairs/batch-import` | 批量导入 |
| POST | `/{knowledge_id}/qa-pairs/ai-generate` | AI 生成 |
| POST | `/{knowledge_id}/search` | 检索测试（chunk + QA 两路） |
| PUT | `/qa-pairs/{qa_pair_id}` | 更新 |
| DELETE | `/qa-pairs/{qa_pair_id}` | 删除（级联向量） |
| POST | `/qa-pairs/{qa_pair_id}/vectorize` | 手动向量化 |
| POST | `/qa-pairs/batch-vectorize` | 批量向量化 |

## ⚠️ 注册顺序是硬约束

这个 router 里有 `/qa-pairs/{qa_pair_id}` 这类**静态前缀**，必须在
`knowledge.py` 的 `/{knowledge_id}` **之前**注册到 app，
否则 `qa-pairs` 会被当成 knowledge_id 去 `int()` 转换然后 404
（本项目已经踩过 7 次同类问题）。看 `app/main.py` 里的注册顺序注释。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request

from app.api.deps import CurrentUserId, DbSession
from app.schemas.common import PageResult, Result
from app.schemas.knowledge_qa import (
    KnowledgeSearchIn,
    QaPairIn,
    QaPairOut,
    QaPairUpdateIn,
    RagSearchResultOut,
)
from app.services.knowledge_search_service import KnowledgeSearchService
from app.services.qa_pair_service import QaPairService

router = APIRouter(prefix="/api/knowledge", tags=["knowledge-qa-pairs"])


def _svc(db: DbSession, user_id: CurrentUserId) -> QaPairService:
    return QaPairService(db, user_id)


@router.post("/{knowledge_id}/qa-pairs", response_model=Result[QaPairOut], summary="新建问答对")
async def create_qa_pair(
    knowledge_id: int, payload: QaPairIn, db: DbSession, user_id: CurrentUserId
) -> Result[QaPairOut]:
    """创建后立刻向量化（Java 是异步，这里同步做，见服务的注释）。"""
    return Result.ok(await _svc(db, user_id).create(knowledge_id, payload))


@router.get(
    "/{knowledge_id}/qa-pairs",
    response_model=Result[PageResult[QaPairOut]],
    summary="分页查询问答对",
)
async def list_qa_pairs(
    knowledge_id: int,
    db: DbSession,
    user_id: CurrentUserId,
    page_num: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    keyword: str = "",
) -> Result[PageResult[QaPairOut]]:
    records, total = await _svc(db, user_id).page(knowledge_id, page_num, page_size, keyword)
    return Result.ok(
        PageResult(
            records=records,
            total=total,
            size=page_size,
            current=page_num,
            pages=(total + page_size - 1) // page_size,
        )
    )


@router.post(
    "/{knowledge_id}/qa-pairs/batch-import",
    response_model=Result[int],
    summary="批量导入问答对",
)
async def batch_import(
    knowledge_id: int, items: list[QaPairIn], db: DbSession, user_id: CurrentUserId
) -> Result[int]:
    return Result.ok(await _svc(db, user_id).batch_import(knowledge_id, items))


@router.post(
    "/{knowledge_id}/qa-pairs/ai-generate",
    response_model=Result[dict[str, Any]],
    summary="AI 生成问答对",
)
async def ai_generate(
    knowledge_id: int,
    db: DbSession,
    user_id: CurrentUserId,
    count: int = Query(10, ge=1, le=20),
    provider_id: int | None = Query(None),
    model_id: str | None = Query(None),
) -> Result[dict[str, Any]]:
    """返回 `task` 对象（Java `Task` 序列化结果）。

    `count` 上限 20（Java `Math.min(count, 20)`）：再多一次 LLM 调用
    JSON 会开始变形，"生成了 50 条但全是同一个问题"比"只给 20 条"更糟。
    """
    return Result.ok(
        await _svc(db, user_id).ai_generate(knowledge_id, count, provider_id, model_id)
    )


@router.post(
    "/{knowledge_id}/search",
    response_model=Result[list[RagSearchResultOut]],
    summary="检索测试（chunk + 问答对，不调 LLM）",
)
async def search_knowledge(
    knowledge_id: int, payload: KnowledgeSearchIn, db: DbSession, user_id: CurrentUserId
) -> Result[list[RagSearchResultOut]]:
    """body 支持 `overrides` 临时覆盖检索参数（不落库）。"""
    return Result.ok(await KnowledgeSearchService(db, user_id).search(knowledge_id, payload))


@router.put("/qa-pairs/{qa_pair_id}", response_model=Result[QaPairOut], summary="更新问答对")
async def update_qa_pair(
    qa_pair_id: int, payload: QaPairUpdateIn, db: DbSession, user_id: CurrentUserId
) -> Result[QaPairOut]:
    return Result.ok(await _svc(db, user_id).update(qa_pair_id, payload))


@router.delete("/qa-pairs/{qa_pair_id}", response_model=Result[None], summary="删除问答对")
async def delete_qa_pair(
    qa_pair_id: int, db: DbSession, user_id: CurrentUserId
) -> Result[None]:
    await _svc(db, user_id).delete(qa_pair_id)
    return Result.ok(None)


@router.post(
    "/qa-pairs/{qa_pair_id}/vectorize",
    response_model=Result[None],
    summary="手动触发向量化（仅 pending / failed）",
)
async def vectorize_one(
    qa_pair_id: int, db: DbSession, user_id: CurrentUserId
) -> Result[None]:
    await _svc(db, user_id).vectorize(qa_pair_id)
    return Result.ok(None)


@router.post(
    "/qa-pairs/batch-vectorize",
    response_model=Result[int],
    summary="批量向量化，返回触发成功的条数",
)
async def batch_vectorize(
    request: Request,
    db: DbSession,
    user_id: CurrentUserId,
) -> Result[int]:
    """body 是**裸数组** `[123,456]`（Java `@RequestBody List<Long>`）。

    为什么不直接用 `ids: list[int] = Body(...)`：FastAPI 对**单值** body
    参数会自动包一层 key（期待 `{"ids": [...]}`），而 Java 契约是裸数组，
    前端 `request.post('/knowledge/qa-pairs/batch-vectorize', qaPairIds)`
    发出去的就是裸数组。这里直接读 Request 并**两种形态都收**，
    省得再去赌 FastAPI 的版本行为。
    """
    try:
        raw = await request.json()
    except Exception:  # noqa: BLE001
        raw = []
    ids: list[int]
    if isinstance(raw, list):
        ids = raw
    elif isinstance(raw, dict):
        ids = raw.get("qaPairIds") or raw.get("qa_pair_ids") or []
    else:
        ids = []

    valid = [int(x) for x in ids]
    return Result.ok(await _svc(db, user_id).batch_vectorize(valid))
