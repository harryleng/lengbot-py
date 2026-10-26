import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.v1 import (
    admin,
    agent_ext,
    agents,
    api_keys,
    auth,
    chat,
    chat_sessions,
    dashboard,
    document_edit,
    enums,
    eval_datasets,
    eval_evaluators,
    eval_experiments,
    graph,
    knowledge,
    knowledge_docs,
    knowledge_eval,
    knowledge_qa,
    landing,
    logs,
    mcp_servers,
    misc,
    model_providers,
    ocr,
    observability,
    prompts,
    session_files,
    skills,
    subagents,
    system_config,
    tasks,
    token_budget,
    tool_calls,
    tools,
    tts_voices,
    user_memories,
    user_preferences,
    workflow,
)
from app.core.db import SessionLocal
from app.services import log_buffer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s")
logger = logging.getLogger("lengbot-py")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """启动时探一次库。

    阶段 0 最容易踩的坑就是「库没恢复 / 端口没起」，如果不在这里炸，
    报错会出现在第一个请求里，还带着一堆 SQLAlchemy 堆栈，很难定位。
    """
    try:
        async with SessionLocal() as session:
            await session.execute(text("SELECT 1"))
        logger.info("数据库连接正常")
    except Exception as exc:  # noqa: BLE001
        logger.error("数据库连不上，先确认：1) 本地 PG 已启动 2) 已执行 db/RESTORE.md 的恢复步骤")
        logger.error("原因：%s", exc)

    # `/api/logs/recent` 的数据源。必须在 logging.basicConfig 之后挂，
    # 否则 root logger 重建时会被洗掉，日志页永远空白。
    log_buffer.install()

    # 内置资源注册器（对照 Java BuiltInSkillRegistrar）：Skill 按 content_hash
    # 同步 + 工具实现漂移报告。内部已兜底（单条失败 log 继续，整体异常不挡启动），
    # await 是为了让启动日志里就能看到同步结果，而不是散落在请求期。
    from app.services.builtin_registrar import run_startup_sync

    await run_startup_sync()

    # 异步任务持久化（对照 Java recoverUnfinishedTasks + TaskZombieScheduler）：
    # 启动重跑遗留任务 + 60s 僵尸收割。reaper 句柄在 shutdown 时取消。
    reaper_task = None
    try:
        from app.services import task_recovery

        await task_recovery.recover_unfinished_tasks()
        reaper_task = await task_recovery.start_zombie_reaper()
    except Exception:  # noqa: BLE001
        logger.exception("[TaskRecovery] 启动恢复失败（不阻断启动）")
    yield
    if reaper_task is not None:
        reaper_task.cancel()


app = FastAPI(
    title="lengbot-py",
    version="0.1.0",
    description="lengbot 的 Python 学习版：message 表为事实来源，LangGraph 仅作执行引擎",
    lifespan=lifespan,
)

# 前端 dev server 是 5173/5174，直连时需要 CORS；
# 走 vite proxy 时用不到，但配上不影响。
app.add_middleware(
    CORSMiddleware,
    # 5175 是 lengbot-py 专用前端（lengbot-py/ui），5173/5174 留给 Java 版主前端
    allow_origins=["http://localhost:5173", "http://localhost:5174", "http://localhost:5175"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """把 FastAPI 默认的 {"detail": "..."} 转成前端的统一格式。

    重要：前端 axios 拦截器读的是 body 里的 `code`，不是 HTTP 状态码。
    Java 侧全局异常处理器也是这么做的，这里必须保持一致，
    否则前端会拿到 undefined.code 然后报「未知错误」。
    """
    return JSONResponse(
        status_code=200,
        content={"code": exc.status_code, "message": str(exc.detail), "data": None},
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Pydantic 参数校验失败（FastAPI 默认返回 422 + `{"detail": [...]}`）。

    **不接管会静默"成功"**：前端拦截器是
        `if (res.code && res.code !== 200) { reject }`
    而默认的 422 body 里**没有 `code` 字段** → `res.code` 是 undefined →
    条件不成立 → 被当成成功返回，调用方拿到 `data === undefined`，
    然后在业务代码里莫名其妙地 `Cannot read property 'xxx' of undefined`。

    这类问题最难查：后端日志只有一行 422，前端报的是业务错误，两边对不上。
    实测触发场景：Agent 名称超过 50 字、绑定接口 body 传了非数组。
    """
    errors = exc.errors()
    # 拼成人能读的一句话：`name: String should have at most 50 characters`
    parts: list[str] = []
    for err in errors[:5]:
        loc = ".".join(str(x) for x in err.get("loc", []) if x != "body")
        msg = str(err.get("msg", "参数不合法"))
        parts.append(f"{loc}: {msg}" if loc else msg)
    detail = "；".join(parts) or "参数校验失败"

    return JSONResponse(
        status_code=200,
        content={"code": 400, "message": detail, "data": None},
    )


app.include_router(auth.router)
app.include_router(admin.router)
app.include_router(user_memories.router)
app.include_router(token_budget.router)
app.include_router(document_edit.router)
app.include_router(graph.router)
app.include_router(graph.kb_router)
app.include_router(misc.router)
app.include_router(session_files.router)
app.include_router(agent_ext.router)
app.include_router(system_config.router)
app.include_router(agents.router)
app.include_router(chat_sessions.router)
app.include_router(eval_datasets.router)
app.include_router(eval_evaluators.router)
app.include_router(eval_experiments.router)
app.include_router(model_providers.router)
app.include_router(tools.router)
app.include_router(mcp_servers.router)
app.include_router(prompts.router)
app.include_router(subagents.router)
app.include_router(chat.router)
app.include_router(workflow.router)

# ⚠️ 知识库路由的注册顺序是硬约束，别随手挪：
#   knowledge_docs / knowledge_qa 里是 /knowledge/documents/...、/knowledge/qa-pairs/...
#   这类**静态前缀**路径，必须先注册；knowledge 里是 /knowledge/{knowledge_id}，
#   后注册才不会把 `documents` / `qa-pairs` 当成 knowledge_id 去转 int 然后 404。
#   这是本项目踩过 7 次的坑（见 agents / tools / model-providers 的同类注释）。
app.include_router(knowledge_qa.router)
app.include_router(knowledge_eval.router)
app.include_router(knowledge_docs.router)
app.include_router(knowledge.router)
app.include_router(ocr.router)
app.include_router(skills.router)
app.include_router(enums.router)
app.include_router(landing.router)
app.include_router(logs.router)
app.include_router(dashboard.router)
app.include_router(observability.router)
app.include_router(api_keys.router)
app.include_router(tts_voices.router)
app.include_router(user_preferences.router)
app.include_router(tasks.router)
app.include_router(tool_calls.router)


@app.get("/health", tags=["ops"])
async def health() -> dict[str, str]:
    return {"status": "ok"}
