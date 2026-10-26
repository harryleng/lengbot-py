"""模型供应商管理：CRUD + 连通性检查 + 拉取模型列表。

对照 Java：`ModelProviderServiceImpl`（纯数据 CRUD）+ `ModelFactory`（凭证解析、
连通性检查、拉模型列表）两个类的合并。Java 拆成两个是为了避开循环依赖
（Service 要调 Factory，Factory 要读 Service），Python 侧用普通函数调用没有这个问题。

**没有缓存层**。Java 有 `modelCache`（本地 + Redis + 多实例广播）和
`providerModels` 的 5 分钟 Redis 缓存，因为 AgentScope 的 Model 实例创建很贵、
而且凭证变更后不失效会持续 401。Python 侧每次调用现建 `LlmConfig`（就是几个
字符串），没有可缓存的对象，所以：
- `refresh-cache` 做成显式 no-op（前端会调，返回成功即可，别 404）
- 不实现 `invalidateCache`，也就没有 Java 那个"改完凭证还用旧 key"的坑
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

import httpx
from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.snowflake import new_id
from app.models.entities import ModelProvider, ModelRecord
from app.schemas.common import PageResult
from app.schemas.model_provider import (
    FetchedModelOut,
    ModelProviderCheckIn,
    ModelProviderIn,
    ModelProviderOut,
    ProviderModelOut,
    ProviderWithModelsOut,
)
from app.services.llm_client import _chat_completions_url
from app.services.model_provider_presets import (
    CONNECTIVITY_PROMPT,
    PRESETS,
    cheapest_model_of,
    config_fields_of,
    infer_model_type,
    model_capabilities_of,
    normalize_type,
)

logger = logging.getLogger(__name__)

# 连通性检查用短超时：前端点"测试连接"在等，不该卡 30 秒
CHECK_TIMEOUT = 25.0
FETCH_TIMEOUT = 20.0


def _json_text(value: Any) -> str:
    if value is None:
        return "{}"
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _parse_json_field(raw: Any) -> Any:
    """把入参的 JSON 文本解析成可写 JSONB 的值。

    解析失败返回空 dict 而不是抛错 —— 前端 `JsonInput` 组件在用户输了一半时
    也会触发保存，这时 JSON 是不完整的。为了半截 JSON 把整个保存拦下来，
    体验很差；Java 侧 `parseConfig` 也是 catch 后返回空 Map（只记 warning）。
    """
    if raw is None:
        return {}
    if isinstance(raw, (dict, list)):
        return raw
    text = str(raw).strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        logger.warning("JSON 字段解析失败，按空对象处理: %s", text[:120])
        return {}


class ModelProviderService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    async def page(self, page_num: int, page_size: int) -> PageResult[ModelProviderOut]:
        total = (
            await self.db.execute(
                select(func.count())
                .select_from(ModelProvider)
                .where(ModelProvider.deleted == 0)
            )
        ).scalar_one()
        rows = (
            (
                await self.db.execute(
                    select(ModelProvider)
                    .where(ModelProvider.deleted == 0)
                    .order_by(ModelProvider.create_time.desc(), ModelProvider.id.desc())
                    .offset((page_num - 1) * page_size)
                    .limit(page_size)
                )
            )
            .scalars()
            .all()
        )
        return PageResult.of(
            records=[ModelProviderOut.model_validate(r) for r in rows],
            total=int(total),
            current=page_num,
            size=page_size,
        )

    async def get(self, provider_id: int) -> ModelProviderOut:
        row = await self._require(provider_id)
        return ModelProviderOut.model_validate(row)

    async def list_with_models(self, model_type: str | None) -> list[ProviderWithModelsOut]:
        """所有**启用**的供应商 + 其下模型，可按模型类型过滤。

        过滤后没模型的供应商直接不返回（Java 同逻辑）——否则前端选择器里会出现
        一堆空分组，看起来像坏了。

        这个接口是 `ModelSelect.vue` 的数据源，被 Agent 编辑页、工作流节点配置、
        Playground 三处复用，改字段名会一起崩。
        """
        providers = (
            (
                await self.db.execute(
                    select(ModelProvider)
                    .where(ModelProvider.deleted == 0, ModelProvider.status == "active")
                    .order_by(ModelProvider.create_time.desc(), ModelProvider.id.desc())
                )
            )
            .scalars()
            .all()
        )

        wanted = (model_type or "").strip().lower()
        out: list[ProviderWithModelsOut] = []
        for p in providers:
            stmt = select(ModelRecord).where(
                ModelRecord.provider_id == p.id, ModelRecord.deleted == 0
            )
            if wanted:
                stmt = stmt.where(ModelRecord.type == wanted)
            models = (await self.db.execute(stmt.order_by(ModelRecord.id))).scalars().all()
            if not models:
                continue
            out.append(
                ProviderWithModelsOut(
                    id=str(p.id),
                    name=p.name,
                    type=p.type,
                    models=[
                        ProviderModelOut(
                            id=str(m.id),
                            model_id=m.model_id,
                            name=m.name,
                            type=m.type,
                            status=m.status,
                            dimension=m.dimension,
                        )
                        for m in models
                    ],
                )
            )
        return out

    # ------------------------------------------------------------------
    # 写操作
    # ------------------------------------------------------------------

    async def create(self, dto: ModelProviderIn) -> ModelProviderOut:
        provider = ModelProvider(
            id=new_id(),
            name=dto.name,
            type=normalize_type(dto.type),
            api_key=dto.api_key,
            base_url=dto.base_url,
            # 注意 models_endpoint 列名是下划线，Python 属性名也是下划线
            models_endpoint=dto.models_endpoint,
            headers_json=_parse_json_field(dto.headers_json),
            extra_json=_parse_json_field(dto.extra_json),
            config=self._build_config(dto),
            status="active",
            create_time=datetime.now(),
            update_time=datetime.now(),
            deleted=0,
        )
        self.db.add(provider)
        await self.db.commit()
        await self.db.refresh(provider)
        logger.info("创建供应商 %s（type=%s）", provider.name, provider.type)
        return ModelProviderOut.model_validate(provider)

    async def update(self, dto: ModelProviderIn) -> ModelProviderOut:
        if not dto.id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="缺少 id")
        provider = await self._require(int(dto.id))

        provider.name = dto.name
        provider.type = normalize_type(dto.type)
        # apiKey 传空串表示"不改"还是"清空"？Java 是直接 set（传 null 就清空）。
        # 但前端编辑弹窗里 apiKey 输入框是空的（因为接口不返回 key），
        # 用户不改就提交空串 —— 直接 set 会把已存的 key 抹掉。
        # 所以这里：传了非空值才覆盖，空值保留原 key。
        if dto.api_key:
            provider.api_key = dto.api_key
        provider.base_url = dto.base_url
        provider.models_endpoint = dto.models_endpoint
        provider.headers_json = _parse_json_field(dto.headers_json)
        provider.extra_json = _parse_json_field(dto.extra_json)
        provider.config = self._build_config(dto)
        provider.update_time = datetime.now()

        await self.db.commit()
        await self.db.refresh(provider)
        logger.info("更新供应商 %s（id=%s）", provider.name, provider.id)
        return ModelProviderOut.model_validate(provider)

    async def delete(self, provider_id: int) -> None:
        """软删供应商，并级联软删其下模型。

        为什么软删：Java 实体上有 `@TableLogic`，`removeById` 实际是
        `UPDATE deleted = 1`。历史会话/Agent 还引用着这个 providerId，
        真删会让那些记录变成悬空引用（打开会话查供应商直接 404）。

        级联删模型同理 —— 不然 `with-models` 会捞出一批没有宿主的模型。
        """
        provider = await self._require(provider_id)
        provider.deleted = 1
        provider.update_time = datetime.now()

        models = (
            (
                await self.db.execute(
                    select(ModelRecord).where(
                        ModelRecord.provider_id == provider_id, ModelRecord.deleted == 0
                    )
                )
            )
            .scalars()
            .all()
        )
        for m in models:
            m.deleted = 1
            m.update_time = datetime.now()

        await self.db.commit()
        logger.info("删除供应商 %s（级联 %d 个模型）", provider_id, len(models))

    async def toggle_status(self, provider_id: int, new_status: str) -> None:
        allowed = {"active", "disabled"}
        code = (new_status or "").strip().lower()
        if code not in allowed:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"status 只能是 {'/'.join(sorted(allowed))}",
            )
        provider = await self._require(provider_id)
        provider.status = code
        provider.update_time = datetime.now()
        await self.db.commit()

    # ------------------------------------------------------------------
    # 连通性检查
    # ------------------------------------------------------------------

    async def check_connectivity(self, provider_id: int) -> str:
        provider = await self._require(provider_id)
        if not provider.api_key:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="该供应商没有配置 API Key"
            )
        model_id = await self._resolve_model_id(provider, None)
        return await self._do_check(
            provider.type or "OPENAI",
            provider.api_key,
            provider.base_url or "",
            model_id,
            self._completions_path(provider.config),
        )

    @staticmethod
    async def check_connectivity_by_form(dto: ModelProviderCheckIn) -> str:
        """表单态检查：不落库，拿用户刚填的值直接试。

        这个接口在用户"还没保存"时就要能用，所以**不能**查库、也不该依赖
        Service 实例（那要求先有一个 AsyncSession，纯属多余）。
        改成 staticmethod 后，API 层直接 `ModelProviderService.check_connectivity_by_form(dto)`。
        """
        if not dto.api_key:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="请先填写 API Key"
            )
        model_id = (dto.model_id or "").strip() or cheapest_model_of(dto.type)
        return await ModelProviderService._do_check(
            normalize_type(dto.type),
            dto.api_key,
            dto.base_url or "",
            model_id,
            (dto.completions_path or "").strip() or None,
        )

    @staticmethod
    async def _do_check(
        provider_type: str,
        api_key: str,
        base_url: str,
        model_id: str,
        completions_path: str | None,
    ) -> str:
        """真发一次最小请求。成功返回"连接成功，API Key 有效"，失败抛 400。

        为什么必须真发：只校验 URL 格式和 key 非空毫无意义 —— 用户最需要知道的
        是"这个 key 到底能不能用"。代价是点一次测试就花一条极短请求的钱
        （`max_tokens=16`，约等于免费）。
        """
        url = ModelProviderService._resolve_completions_url(
            base_url, provider_type, completions_path
        )
        payload = {
            "model": model_id,
            "messages": [{"role": "user", "content": CONNECTIVITY_PROMPT}],
            "max_tokens": 16,
            "stream": False,
        }
        try:
            async with httpx.AsyncClient(timeout=CHECK_TIMEOUT) as client:
                resp = await client.post(
                    url,
                    json=payload,
                    headers={"Authorization": f"Bearer {api_key}"},
                )
        except httpx.TimeoutException as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"连接超时（>{CHECK_TIMEOUT:g}s）：{url}",
            ) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"连接失败：{type(exc).__name__}: {exc}",
            ) from exc

        if resp.status_code >= 400:
            # 把上游的错误原文带回去 —— 用户要靠它区分"key 错"还是"模型名错"
            detail = resp.text[:400]
            logger.warning("连通性检查失败 %s: %s %s", url, resp.status_code, detail)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"HTTP {resp.status_code}: {detail}",
            )
        logger.info("连通性检查通过: type=%s model=%s", provider_type, model_id)
        return "连接成功，API Key 有效"

    # ------------------------------------------------------------------
    # 拉取模型列表
    # ------------------------------------------------------------------

    async def fetch_models(self, provider_id: int) -> list[FetchedModelOut]:
        """联网拉该供应商的模型列表（OpenAI 兼容的 `/models`）。

        只有 OpenAI 兼容协议有标准 `/models`，所以所有 type 都按这个格式请求 ——
        Java 侧也只有 `OpenAIModelHandler` 实现了 `fetchModels`，
        其余 handler 走接口默认实现返回空列表。这里统一实现，比 Java 更宽松一点，
        因为 Ollama / DashScope 其实也有兼容的 /models。
        """
        provider = await self._require(provider_id)
        if not provider.api_key:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="该供应商没有配置 API Key"
            )
        url = (provider.models_endpoint or "").strip() or (
            (provider.base_url or "").rstrip("/") + "/v1/models"
        )

        headers = {"Authorization": f"Bearer {provider.api_key}"}
        extra = provider.headers_json or {}
        if isinstance(extra, dict):
            for k, v in extra.items():
                if v not in (None, ""):
                    headers[str(k)] = str(v)

        try:
            async with httpx.AsyncClient(timeout=FETCH_TIMEOUT) as client:
                resp = await client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"拉取模型列表失败：{type(exc).__name__}: {exc}",
            ) from exc

        if resp.status_code >= 400:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"拉取模型列表失败 HTTP {resp.status_code}: {resp.text[:300]}",
            )

        try:
            data = resp.json().get("data") or []
        except (json.JSONDecodeError, AttributeError):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"返回不是预期的 JSON 结构：{resp.text[:200]}",
            ) from None

        seen: set[str] = set()
        out: list[FetchedModelOut] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            mid = item.get("id")
            if not mid:
                continue
            mid = str(mid)
            if mid in seen:
                continue
            seen.add(mid)
            out.append(FetchedModelOut(model_id=mid, type=infer_model_type(mid)))
        out.sort(key=lambda x: x.model_id)
        logger.info("拉取到 %d 个模型（provider=%s）", len(out), provider_id)
        return out

    # ------------------------------------------------------------------
    # 静态数据（不查库）
    # ------------------------------------------------------------------

    async def config_fields(self, provider_id: int) -> list[dict[str, Any]]:
        provider = await self._require(provider_id)
        return config_fields_of(provider.type or "OPENAI")

    async def model_capabilities(self, provider_id: int) -> list[dict[str, Any]]:
        provider = await self._require(provider_id)
        return model_capabilities_of(provider.type or "OPENAI")

    @staticmethod
    def default_model(provider_type: str | None) -> str:
        return cheapest_model_of(provider_type or "OPENAI")

    @staticmethod
    def presets() -> list[dict[str, Any]]:
        return PRESETS

    @staticmethod
    def refresh_cache() -> None:
        """显式 no-op。

        Java 里要清 AgentScope Model 实例缓存 + Redis + 多实例广播，
        因为不清会沿用旧凭证导致持续 401。Python 侧没有这类缓存
        （每次调用现建 LlmConfig），所以这里什么都不用做。
        但**必须存在**：前端"保存后刷新缓存"是一个必调动作，删了会 404。
        """

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    async def _require(self, provider_id: int) -> ModelProvider:
        row = (
            await self.db.execute(
                select(ModelProvider).where(
                    ModelProvider.id == provider_id, ModelProvider.deleted == 0
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="模型供应商不存在"
            )
        return row

    def _build_config(self, dto: ModelProviderIn) -> dict[str, Any]:
        """把 dto.config + defaultModelId 合成最终落库的 config。

        对齐 Java `buildProviderConfig`：`defaultModelId` 不是独立列，
        它被塞进 `config.modelId`（空串则移除该键，表示"跟随供应商默认"）。
        """
        config = _parse_json_field(dto.config)
        if not isinstance(config, dict):
            config = {}
        if dto.default_model_id is not None:
            if dto.default_model_id.strip():
                config["modelId"] = dto.default_model_id.strip()
            else:
                config.pop("modelId", None)
        return config

    @staticmethod
    def _completions_path(config: Any) -> str | None:
        if isinstance(config, dict):
            path = config.get("completionsPath")
            if isinstance(path, str) and path.strip():
                return path.strip()
        return None

    @staticmethod
    def _resolve_completions_url(
        base_url: str, provider_type: str, completions_path: str | None
    ) -> str:
        """拼 chat/completions 地址。

        优先级：用户配的 `completionsPath` > `baseUrl` + 通用拼接规则。
        后者复用 `llm_client._chat_completions_url`，那里踩过腾讯云
        `/plan/v3` 被拼成 `/plan/v3/v1/...` 导致 404 的坑。
        """
        base = (base_url or "").rstrip("/")
        if completions_path:
            if completions_path.startswith("http"):
                return completions_path
            if not completions_path.startswith("/"):
                completions_path = "/" + completions_path
            return base + completions_path
        if not base:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="请先填写 Base URL"
            )
        return _chat_completions_url(base)

    async def _resolve_model_id(
        self, provider: ModelProvider, explicit: str | None
    ) -> str:
        """连通性检查用哪个模型。四级回退，顺序照抄 Java `resolveModelId`：

        1. 显式传入的 modelId（表单态检查用）
        2. `provider.config.modelId`（用户在配置里选的）
        3. **该供应商下 type=llm 且 active 的第一个模型**
        4. `getCheapestModel()` 兜底

        第 3 步为什么要卡 `type == 'llm'`：SiliconFlow 那种供应商下可能**只有**
        embedding 模型（BAAI/bge-*），拿它去调 chat 接口会被上游拒掉
        （Java 注释里写的是错误码 20012）。所以必须按类型筛。
        """
        if explicit and explicit.strip():
            return explicit.strip()

        config = provider.config or {}
        if isinstance(config, dict):
            configured = config.get("modelId")
            if isinstance(configured, str) and configured.strip():
                return configured.strip()

        row = (
            await self.db.execute(
                select(ModelRecord)
                .where(
                    ModelRecord.provider_id == provider.id,
                    ModelRecord.deleted == 0,
                    ModelRecord.type == "llm",
                    ModelRecord.status == "active",
                )
                .order_by(ModelRecord.id)
                .limit(1)
            )
        ).scalar_one_or_none()
        if row is not None and row.model_id:
            return row.model_id

        return cheapest_model_of(provider.type or "OPENAI")
