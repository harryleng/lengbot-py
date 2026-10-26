"""评测集接口（14 个，对齐前端 `src/api/evalDataset.js`）。

**路由顺序**（第 6 次遇到同一个问题，这次从一开始就排对）：
`/versions`、`/items`、`/examples` 都是静态首段，必须全部排在
`/{dataset_id}` 之前。反了的话 `GET /api/eval/datasets/examples` 会被
`/{dataset_id}` 吃掉 → `int("examples")` 失败 → 422，
前端"从示例创建"永远打不开。

`PUT /datasets/{id}` 的 id 在**路径**里（Java `@PathVariable`），
而 `PUT /prompts?id=` 是在 query 里 —— 两个模块写法不同，别混。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import CurrentUserId, DbSession
from app.schemas.common import PageResult, Result
from app.schemas.eval import (
    EvalDatasetIn,
    EvalDatasetItemIn,
    EvalDatasetItemOut,
    EvalDatasetOut,
    EvalDatasetVersionIn,
    EvalDatasetVersionOut,
)
from app.services.eval_dataset_service import EvalDatasetService
from app.services.eval_examples import (
    create_dataset_from_example,
    list_dataset_examples,
)

router = APIRouter(prefix="/api/eval/datasets", tags=["eval-datasets"])


def _svc(db: DbSession) -> EvalDatasetService:
    return EvalDatasetService(db)


# ======================================================================
# ① 静态路径（必须最前）
# ======================================================================


@router.get("/examples", response_model=Result[list[dict]], summary="内置示例评测集列表")
async def list_examples(user_id: CurrentUserId) -> Result[list[dict]]:
    """4 个内置模板（prompt_quality / qa_accuracy / text_classification / summarization）。

    逐字抄 Java `EvalDatasetExampleTemplates.listExamples()` —— 连 itemCount 都不改，
    前端用它显示"含 5 条数据"。
    """
    return Result.ok(list_dataset_examples())


@router.post("/examples/{key}", response_model=Result[EvalDatasetOut], summary="从示例创建评测集")
async def create_from_example(
    key: str, db: DbSession, user_id: CurrentUserId
) -> Result[EvalDatasetOut]:
    """一步做完四件事：建集 → 批量塞样本 → 建 v1.0 版本 → 返回。

    名称冲突时自动加 ` (2)` 后缀（Java 也是），用户连点两次不会撞名。
    """
    return Result.ok(await create_dataset_from_example(db, key, user_id))


@router.post("/versions", response_model=Result[EvalDatasetVersionOut], summary="创建评测集版本")
async def create_version(
    dto: EvalDatasetVersionIn, db: DbSession, user_id: CurrentUserId
) -> Result[EvalDatasetVersionOut]:
    """冻结当前条目 **id 列表**（不是内容快照）。空数据集会 400。"""
    return Result.ok(await _svc(db).create_version(int(dto.dataset_id), dto.version))


@router.post("/items", response_model=Result[EvalDatasetItemOut], summary="添加一条样本")
async def add_item(
    dto: EvalDatasetItemIn, db: DbSession, user_id: CurrentUserId
) -> Result[EvalDatasetItemOut]:
    if not dto.dataset_id:
        return Result.fail("缺少 datasetId", code=400)
    return Result.ok(
        await _svc(db).add_item(int(dto.dataset_id), dto.data_content)
    )


@router.post("/items/batch", response_model=Result[int], summary="批量添加样本")
async def batch_add_items(
    dto: EvalDatasetItemIn, db: DbSession, user_id: CurrentUserId
) -> Result[int]:
    """`dataContents` 是数组；单条时也可只给 `dataContent`。

    返回**新增条数**（不是数组）。前端读 `res.data` 直接显示"导入 N 条"。
    """
    if not dto.dataset_id:
        return Result.fail("缺少 datasetId", code=400)
    contents = dto.data_contents
    if contents is None and dto.data_content is not None:
        contents = [dto.data_content]
    return Result.ok(await _svc(db).batch_add_items(int(dto.dataset_id), contents))


@router.get("/items", response_model=Result[PageResult[EvalDatasetItemOut]], summary="样本分页")
async def list_items(
    db: DbSession,
    user_id: CurrentUserId,
    dataset_id: Annotated[int, Query(alias="datasetId")],
    page_num: Annotated[int, Query(alias="pageNum")] = 1,
    page_size: Annotated[int, Query(alias="pageSize")] = 20,
) -> Result[PageResult[EvalDatasetItemOut]]:
    records, total = await _svc(db).list_items(dataset_id, page_num, page_size)
    return Result.ok(
        PageResult.of(records=records, total=total, current=page_num, size=page_size)
    )


@router.get(
    "/versions/{version_id}/items",
    response_model=Result[list[EvalDatasetItemOut]],
    summary="某个版本的数据项快照",
)
async def list_version_items(
    version_id: int, db: DbSession, user_id: CurrentUserId
) -> Result[list[EvalDatasetItemOut]]:
    """**顺序按版本里的 id 列表**，不是按创建时间 —— 实验按这个顺序跑。"""
    return Result.ok(await _svc(db).get_version_items(version_id))


@router.delete("/items/{item_id}", response_model=Result[None], summary="删除一条样本")
async def delete_item(item_id: int, db: DbSession, user_id: CurrentUserId) -> Result[None]:
    await _svc(db).delete_item(item_id)
    return Result.ok(None)


# ======================================================================
# ② 主表
# ======================================================================


@router.get("", response_model=Result[PageResult[EvalDatasetOut]], summary="评测集列表")
async def list_datasets(
    db: DbSession,
    user_id: CurrentUserId,
    page_num: Annotated[int, Query(alias="pageNum")] = 1,
    page_size: Annotated[int, Query(alias="pageSize")] = 20,
    keyword: Annotated[str | None, Query()] = None,
) -> Result[PageResult[EvalDatasetOut]]:
    records, total = await _svc(db).list(user_id, page_num, page_size, keyword)
    return Result.ok(
        PageResult.of(records=records, total=total, current=page_num, size=page_size)
    )


@router.post("", response_model=Result[EvalDatasetOut], summary="创建评测集")
async def create_dataset(
    dto: EvalDatasetIn, db: DbSession, user_id: CurrentUserId
) -> Result[EvalDatasetOut]:
    return Result.ok(await _svc(db).create(dto, user_id))


# ======================================================================
# ③ 动态 detail（最后）
# ======================================================================


@router.get("/{dataset_id}", response_model=Result[EvalDatasetOut], summary="评测集详情")
async def get_dataset(
    dataset_id: int, db: DbSession, user_id: CurrentUserId
) -> Result[EvalDatasetOut]:
    return Result.ok(await _svc(db).get(dataset_id))


@router.put("/{dataset_id}", response_model=Result[None], summary="更新评测集")
async def update_dataset(
    dataset_id: int,
    dto: EvalDatasetIn,
    db: DbSession,
    user_id: CurrentUserId,
) -> Result[None]:
    """id 在**路径**里（和 prompt 的 `?id=` 不一样，别混）。"""
    await _svc(db).update(dataset_id, dto)
    return Result.ok(None)


@router.delete("/{dataset_id}", response_model=Result[None], summary="删除评测集")
async def delete_dataset(
    dataset_id: int, db: DbSession, user_id: CurrentUserId
) -> Result[None]:
    await _svc(db).delete(dataset_id)
    return Result.ok(None)


@router.get(
    "/{dataset_id}/versions",
    response_model=Result[list[EvalDatasetVersionOut]],
    summary="评测集版本列表",
)
async def list_versions(
    dataset_id: int, db: DbSession, user_id: CurrentUserId
) -> Result[list[EvalDatasetVersionOut]]:
    return Result.ok(await _svc(db).list_versions(dataset_id))


__all__ = ["router"]
