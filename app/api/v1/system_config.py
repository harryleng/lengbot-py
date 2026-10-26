"""系统配置接口。

目前只实现前端启动时**必经**的那一个：`GET /api/system-config/health`。

前端 `views/Landing.vue` 和 `views/Login.vue` 挂载时就调它，
失败会直接显示「后端连接失败」横幅 —— 所以这个接口 404，页面第一眼就像挂了。
（Landing 的配置接口另有 try/catch 兜底，不影响。）

对照 Java：`SystemConfigController.health()` → `HealthService.aggregate()`，
返回 `{status: UP|DEGRADED, components: {postgresql/redis/minio: {...}}}`。

**Python 版的差异要说清楚**：这里只依赖 PostgreSQL。Redis 和 MinIO 在 Python 版
一个都没接（RAG 走 pgvector，对象存储没用到），所以它们标 `NOT_CONFIGURED`
而不是伪装成 UP —— 假报 UP 比报错更坏，将来真接上了你会以为它一直在跑。
"""

import logging
import time

from fastapi import APIRouter
from sqlalchemy import text

from app.api.deps import CurrentUserId, DbSession
from app.schemas.common import Result
from app.services.system_config_service import SystemConfigService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/system-config", tags=["system-config"])


@router.get("/health", response_model=Result[dict], summary="基础设施健康检查（公开）")
async def health(db: DbSession) -> Result[dict]:
    components: dict[str, dict] = {}
    all_up = True

    # 1. PostgreSQL —— 唯一真实依赖，跑一条 SELECT 1 计时
    started = time.perf_counter()
    try:
        await db.execute(text("SELECT 1"))
        components["postgresql"] = {
            "status": "UP",
            "responseTime": f"{int((time.perf_counter() - started) * 1000)}ms",
        }
    except Exception as exc:  # noqa: BLE001
        components["postgresql"] = {"status": "DOWN", "error": str(exc)[:300]}
        all_up = False
        logger.warning("健康检查：PostgreSQL 不通 —— %s", exc)

    # 2/3. Redis、MinIO —— Python 版没接，如实标注
    for name in ("redis", "minio"):
        components[name] = {
            "status": "NOT_CONFIGURED",
            "note": "Python 版未接入该组件（不影响聊天 / RAG / 工作流）",
        }

    return Result.ok({"status": "UP" if all_up else "DEGRADED", "components": components})


# ======================================================================
# 默认模型配置（11 个接口）
# ======================================================================
# 前端 `SettingsView.vue` 的「模型设置」标签页：进页面调 `default-models`
# 一次拿 4 个，每个卡片单独 PUT。`default-ai` 是老接口，等价于 chat。
#
# **注意返回的 providerId 是字符串**，不是数字 —— 19 位雪花 ID 超过 JS 安全整数，
# 用数字传过去前端 `JSON.parse` 就会失真，下拉框选不中已配置的模型。
# 详见 `services/system_config_service.py` 的模块 docstring。


def _svc(db: DbSession) -> SystemConfigService:
    return SystemConfigService(db)


@router.get("/default-models", response_model=Result[dict], summary="所有默认模型配置（合并）")
async def get_all_default_models(db: DbSession) -> Result[dict]:
    return Result.ok(await _svc(db).get_all())


@router.get("/default-ai", response_model=Result[dict], summary="默认 AI 配置（等价于对话模型）")
async def get_default_ai(db: DbSession) -> Result[dict]:
    return Result.ok(await _svc(db).get_chat())


@router.put("/default-ai", response_model=Result[None], summary="更新默认 AI 配置")
async def update_default_ai(payload: dict, db: DbSession, user_id: CurrentUserId) -> Result[None]:
    await _svc(db).update_ai(payload)
    return Result.ok(None)


@router.get("/default-chat-model", response_model=Result[dict], summary="默认对话模型")
async def get_default_chat(db: DbSession) -> Result[dict]:
    return Result.ok(await _svc(db).get_chat())


@router.put("/default-chat-model", response_model=Result[None], summary="更新默认对话模型")
async def update_default_chat(payload: dict, db: DbSession, user_id: CurrentUserId) -> Result[None]:
    await _svc(db).update_chat(payload)
    return Result.ok(None)


@router.get("/default-embedding-model", response_model=Result[dict], summary="默认向量模型")
async def get_default_embedding(db: DbSession) -> Result[dict]:
    return Result.ok(await _svc(db).get_embedding())


@router.put("/default-embedding-model", response_model=Result[None], summary="更新默认向量模型")
async def update_default_embedding(
    payload: dict, db: DbSession, user_id: CurrentUserId
) -> Result[None]:
    await _svc(db).update_embedding(payload)
    return Result.ok(None)


@router.get("/default-tts-model", response_model=Result[dict], summary="默认 TTS 模型")
async def get_default_tts(db: DbSession) -> Result[dict]:
    return Result.ok(await _svc(db).get_tts())


@router.put("/default-tts-model", response_model=Result[None], summary="更新默认 TTS 模型")
async def update_default_tts(payload: dict, db: DbSession, user_id: CurrentUserId) -> Result[None]:
    await _svc(db).update_tts(payload)
    return Result.ok(None)


@router.get("/default-rerank-model", response_model=Result[dict], summary="默认重排模型")
async def get_default_rerank(db: DbSession) -> Result[dict]:
    return Result.ok(await _svc(db).get_rerank())


@router.put("/default-rerank-model", response_model=Result[None], summary="更新默认重排模型")
async def update_default_rerank(
    payload: dict, db: DbSession, user_id: CurrentUserId
) -> Result[None]:
    await _svc(db).update_rerank(payload)
    return Result.ok(None)
