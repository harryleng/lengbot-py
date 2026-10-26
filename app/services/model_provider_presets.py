"""模型供应商的静态数据：配置字段定义 + 预设模板 + 默认模型。

这些数据在 Java 侧分散在 **6 个类**里：
- `OpenAIModelHandler` / `DashScopeModelHandler` / `DeepSeekModelHandler`
  / `OllamaModelHandler` / `MimoModelHandler` 各自手写 `getConfigFields()`
- `AgentCapabilityConfigFields` 提供 5 种能力字段组合
- `ModelProviderServiceImpl.listPresets()` 里 12 条硬编码预设

Python 侧把它们收进一个纯数据模块。理由：这些是**配置元数据**，没有任何行为，
每个 provider 一个 class 只会得到 5 个只有 getter 的类。用 dict 表更直白，
改的时候也一目了然（对比 5 个 handler 各 250 行 Java）。

对照 Java：`ModelProviderHandler` 接口里和"数据"有关的那几个方法
（`getConfigFields` / `getModelCapabilities` / `getCheapestModel`），
`createModel` / `buildGenerateOptions` 那部分 Python 侧由 `services/llm_client.py` 直接处理，
不需要这层抽象。
"""

from __future__ import annotations

from typing import Any

# ----------------------------------------------------------------------
# 提供商类型
# ----------------------------------------------------------------------

PROVIDER_TYPES: dict[str, str] = {
    "OPENAI": "OpenAI",
    "DASHSCOPE": "通义千问",
    "DEEPSEEK": "DeepSeek",
    "OLLAMA": "Ollama",
    "MIMO": "小米MiMo",
}

# 连通性检查用一个极短的请求，只要模型能回话就说明 key/url 都对
CONNECTIVITY_PROMPT = "你好，请回复OK"


def normalize_type(value: str | None) -> str:
    """把前端传来的 type 归一到枚举 code。

    前端可能传 code（`OPENAI`）、也可能传中文名（`OpenAI` / `通义千问`）——
    Java 的 `ModelProviderType.fromValue` 两种都认，这里保持一致。
    认不出来就退回 OPENAI：宁可让用户改，也别 500。
    """
    if not value:
        return "OPENAI"
    raw = str(value).strip()
    upper = raw.upper()
    if upper in PROVIDER_TYPES:
        return upper
    for code, desc in PROVIDER_TYPES.items():
        if raw == desc or raw.lower() == desc.lower():
            return code
    return "OPENAI"


# ----------------------------------------------------------------------
# ConfigField 构造辅助
# ----------------------------------------------------------------------


def _field(
    key: str,
    label: str,
    type_: str = "text",
    *,
    default: Any = None,
    hint: str | None = None,
    min_: float | None = None,
    max_: float | None = None,
    step: float | None = None,
    options: list[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """造一个 ConfigField。字段名用 camelCase —— 前端按 JS 习惯取。

    Java 侧是 `@Data` + Lombok 生成的 getter，Jackson 输出 camelCase；
    这里直接写 camelCase，省掉一层转换。
    """
    out: dict[str, Any] = {"key": key, "label": label, "type": type_}
    if min_ is not None:
        out["min"] = min_
    if max_ is not None:
        out["max"] = max_
    if step is not None:
        out["step"] = step
    if options:
        out["options"] = [{"value": v, "label": lb} for v, lb in options]
    if default is not None:
        out["defaultValue"] = default
    if hint:
        out["hint"] = hint
    return out


# ----------------------------------------------------------------------
# 各 provider 的调参字段（对应 Java 各 Handler.getConfigFields）
# ----------------------------------------------------------------------
# ⚠️ 不要把滑块的默认值抽成"公共常量"。第一版我这么干过，结果 5 处全错：
#    provider    temperature  topP  maxTokens(max)
#    OPENAI      0.7          1.0   2048 (8192)
#    DASHSCOPE   0.7          0.8   2048 (8192)
#    DEEPSEEK    0.7          1.0   4096 (8192)
#    OLLAMA      0.7          0.9   2048 (4096)
#    MIMO        1.0          0.95  4096 (32768)
# 这些默认值是各家厂商自己的推荐值，不是笔误。抽公共常量看着"干净"，
# 实际是把 5 家厂商的调参经验拍平成一个，用户点开就拿到错的起始值。


def _model_select(options: list[tuple[str, str]], default: str, hint: str) -> dict[str, Any]:
    return _field("modelId", "模型", "select", options=options, default=default, hint=hint)


def _slider(key: str, label: str, min_: float, max_: float, step: float,
            default: float, hint: str | None = None) -> dict[str, Any]:
    return _field(key, label, "slider", min_=min_, max_=max_, step=step,
                  default=default, hint=hint)


def _max_tokens(max_: float, default: int) -> dict[str, Any]:
    return _field("maxTokens", "最大 Token", "number", min_=256.0, max_=max_,
                  step=256.0, default=default, hint="单次回答的最大长度")


def _penalties() -> list[dict[str, Any]]:
    return [
        _field("presencePenalty", "存在惩罚", "slider", min_=-2.0, max_=2.0,
               step=0.1, default=0.0, hint="正值降低重复话题的概率"),
        _field("frequencyPenalty", "频率惩罚", "slider", min_=-2.0, max_=2.0,
               step=0.1, default=0.0, hint="正值降低重复用词的概率"),
    ]


CONFIG_FIELDS: dict[str, list[dict[str, Any]]] = {
    "OPENAI": [
        _model_select(
            [
                ("gpt-4o-mini", "GPT-4o Mini（视觉）"),
                ("gpt-4o", "GPT-4o（视觉）"),
                ("gpt-4-turbo", "GPT-4 Turbo（视觉）"),
                ("gpt-4", "GPT-4"),
                ("gpt-3.5-turbo", "GPT-3.5 Turbo"),
            ],
            "gpt-4o-mini",
            "多模态请选用 gpt-4o / gpt-4o-mini / gpt-4-turbo 等视觉模型",
        ),
        _slider("temperature", "温度", 0.0, 2.0, 0.1, 0.7, "值越高回答越随机，值越低回答越确定"),
        _slider("topP", "核采样", 0.0, 1.0, 0.05, 1.0, "控制词汇选择的多样性"),
        _max_tokens(8192.0, 2048),
        *_penalties(),
    ],
    "DASHSCOPE": [
        _model_select(
            [
                ("qwen-plus", "Qwen-Plus（推荐）"),
                ("qwen-max", "Qwen-Max（最强）"),
                ("qwen-turbo", "Qwen-Turbo（快速）"),
                ("qwen-vl-plus", "Qwen-VL-Plus（视觉）"),
                ("qwen-vl-max", "Qwen-VL-Max（视觉增强）"),
            ],
            "qwen-plus",
            "视觉模型请选用 qwen-vl 系列",
        ),
        _slider("temperature", "温度", 0.0, 2.0, 0.1, 0.7, "值越高回答越随机"),
        _slider("topP", "核采样", 0.0, 1.0, 0.05, 0.8, None),
        _max_tokens(8192.0, 2048),
    ],
    "DEEPSEEK": [
        _model_select(
            [
                ("deepseek-chat", "DeepSeek Chat"),
                ("deepseek-reasoner", "DeepSeek Reasoner"),
            ],
            "deepseek-chat",
            "reasoner 会输出思考过程，速度较慢、成本较高",
        ),
        _slider("temperature", "温度", 0.0, 2.0, 0.1, 0.7, "值越高回答越随机"),
        _slider("topP", "核采样", 0.0, 1.0, 0.05, 1.0, None),
        _max_tokens(8192.0, 4096),
    ],
    "OLLAMA": [
        _field(
            "modelId",
            "模型名称",
            "text",
            default="qwen2.5:latest",
            hint="填 `ollama list` 里看到的模型名，如 qwen2.5:latest",
        ),
        _slider("temperature", "温度", 0.0, 2.0, 0.1, 0.7, "值越高回答越随机"),
        _slider("topP", "核采样", 0.0, 1.0, 0.05, 0.9, None),
        # 本地部署显存有限，上限只有 4096
        _max_tokens(4096.0, 2048),
    ],
    "MIMO": [
        _model_select(
            [
                ("mimo-v2.5-pro", "MiMo v2.5 Pro"),
                ("mimo-v2.5", "MiMo v2.5"),
                ("mimo-v2-omni", "MiMo v2 Omni（多模态）"),
                ("MiMo-7B-RL", "MiMo-7B-RL"),
                ("MiMo-7B", "MiMo-7B"),
            ],
            "mimo-v2.5-pro",
            "多模态建议选用 mimo-v2.5 或 mimo-v2-omni",
        ),
        _slider("temperature", "温度", 0.0, 2.0, 0.1, 1.0, "值越高回答越随机创造性"),
        _slider("topP", "核采样", 0.0, 1.0, 0.05, 0.95, None),
        _max_tokens(32768.0, 4096),
        *_penalties(),
    ],
}


# ----------------------------------------------------------------------
# 模型能力字段（对应 Java AgentCapabilityConfigFields）
# ----------------------------------------------------------------------


def _vision_audio_fields(vision_hint: str, image_hint: str, video_hint: str | None) -> list[dict]:
    """多模态能力开关。三个 hint 文案按 provider 不同。

    Java 侧是同一个私有方法被 5 个 `xxxFields()` 复用，只是文案不同 ——
    说明"能力项本身"是通用的，差异只在提示语。这里保留同样的结构。
    """
    fields = [
        _field("multimodalEnabled", "多模态", "switch", default=False, hint=vision_hint),
        _field("enableImageInput", "图片输入", "switch", default=False, hint=image_hint),
    ]
    if video_hint:
        fields.append(
            _field("enableVideoInput", "视频输入", "switch", default=False, hint=video_hint)
        )
    fields.append(
        _field(
            "enableAudioInput",
            "语音输入",
            "switch",
            default=False,
            hint="浏览器端语音识别（STT），不消耗模型额度",
        )
    )
    return fields


def _web_search_fields() -> list[dict]:
    return [
        _field(
            "webSearchEnabled",
            "联网搜索",
            "switch",
            default=False,
            hint="开启后模型可调用搜索工具获取实时信息",
        )
    ]


MODEL_CAPABILITIES: dict[str, list[dict[str, Any]]] = {
    "OPENAI": _vision_audio_fields(
        "开启后对话页可上传图片；请选用 gpt-4o / gpt-4o-mini / gpt-4-turbo 等视觉模型",
        "允许用户上传 JPEG/PNG/WebP/GIF，通过 OpenAI 视觉接口（image_url）发送",
        None,
    ),
    "DASHSCOPE": _vision_audio_fields(
        "开启后对话页可上传图片；建议选用 qwen-vl-max / qwen-vl-plus / qwen2-vl 等视觉模型",
        "允许用户上传 JPEG/PNG/WebP/GIF，通过通义千问多模态接口发送",
        None,
    ),
    "DEEPSEEK": [],  # Java 侧就是空列表：DeepSeek 没有多模态
    "OLLAMA": _vision_audio_fields(
        "开启后对话页可上传图片；请选用 llava / minicpm-v 等本地视觉模型",
        "允许用户上传 JPEG/PNG/WebP，通过本地 Ollama 多模态接口发送",
        None,
    ),
    "MIMO": [
        *_vision_audio_fields(
            "开启后对话页可上传图片/视频；需同时开启下方对应输入能力",
            "允许用户上传 JPEG/PNG/WebP 等图片，以 Base64 形式调用 MiMo 视觉理解",
            "允许用户上传 MP4/WebM 等短视频（≤20MB），由 MiMo 视频理解接口处理",
        ),
        *_web_search_fields(),
        _field(
            "enableTts",
            "语音合成",
            "switch",
            default=False,
            hint="预留：将 AI 回复合成为语音（需配合 MiMo TTS 接口）",
        ),
        _field(
            "enableReasoning",
            "深度思考",
            "switch",
            default=False,
            hint="开启后输出 reasoning 思考过程（MiMo thinking 参数）",
        ),
    ],
}


def config_fields_of(provider_type: str) -> list[dict[str, Any]]:
    return CONFIG_FIELDS.get(normalize_type(provider_type), CONFIG_FIELDS["OPENAI"])


def model_capabilities_of(provider_type: str) -> list[dict[str, Any]]:
    return MODEL_CAPABILITIES.get(normalize_type(provider_type), [])


# ----------------------------------------------------------------------
# 默认（最便宜）模型 —— 用于连通性检查、标题生成这类低成本场景
# ----------------------------------------------------------------------

CHEAPEST_MODEL: dict[str, str] = {
    "OPENAI": "gpt-4o-mini",
    "DASHSCOPE": "qwen-plus",
    "DEEPSEEK": "deepseek-chat",
    "OLLAMA": "qwen2.5:latest",
    "MIMO": "mimo-v2.5",
}


def cheapest_model_of(provider_type: str) -> str:
    return CHEAPEST_MODEL.get(normalize_type(provider_type), "gpt-4o-mini")


# ----------------------------------------------------------------------
# 预设模板（对应 Java ModelProviderServiceImpl.listPresets）
# ----------------------------------------------------------------------

_ICON = "https://registry.npmmirror.com/@lobehub/icons-static-svg/latest/files/icons"

# 注意：Java 侧「火山 AgentPlan」的 logo 是一段 **几 KB 的 base64 SVG 内联**。
# 这里刻意不抄 —— 前端 `ModelProviderManage.vue` 有 `v-else` 兜底（显示名字首字母），
# 少了 logo 只是少个图标，不影响任何功能。把几 KB 噪音塞进源码不值得。
PRESETS: list[dict[str, Any]] = [
    {
        "code": "openai",
        "name": "OpenAI",
        "description": "标准 OpenAI Chat Completions 接口预设",
        "type": "OPENAI",
        "logo": f"{_ICON}/openai.svg",
        "baseUrl": "https://api.openai.com/v1",
        "defaultModelId": "gpt-4o-mini",
        "completionsPath": None,
        "modelsEndpoint": "https://api.openai.com/v1/models",
        "headersJson": "{}",
        "extraJson": "{}",
    },
    {
        "code": "deepseek",
        "name": "DeepSeek",
        "description": "DeepSeek OpenAI 兼容接口预设",
        "type": "OPENAI",
        "logo": f"{_ICON}/deepseek-color.svg",
        "baseUrl": "https://api.deepseek.com",
        "defaultModelId": "deepseek-chat",
        "completionsPath": None,
        "modelsEndpoint": "https://api.deepseek.com/v1/models",
        "headersJson": "{}",
        "extraJson": "{}",
    },
    {
        "code": "zhipuai",
        "name": "智谱 BigModel",
        "description": "GLM OpenAI 兼容接口预设",
        "type": "OPENAI",
        "logo": f"{_ICON}/zhipu-color.svg",
        "baseUrl": "https://open.bigmodel.cn/api/paas/v4",
        "defaultModelId": "glm-4-flash",
        "completionsPath": None,
        "modelsEndpoint": "https://open.bigmodel.cn/api/paas/v4/models",
        "headersJson": "{}",
        "extraJson": "{}",
    },
    {
        "code": "moonshotai-cn",
        "name": "Moonshot CN",
        "description": "Kimi 国内 OpenAI 兼容接口预设",
        "type": "OPENAI",
        "logo": f"{_ICON}/moonshot.svg",
        "baseUrl": "https://api.moonshot.cn/v1",
        "defaultModelId": "moonshot-v1-8k",
        "completionsPath": None,
        "modelsEndpoint": "https://api.moonshot.cn/v1/models",
        "headersJson": "{}",
        "extraJson": "{}",
    },
    {
        "code": "minimax-cn",
        "name": "MiniMax CN",
        "description": "MiniMax 国内 OpenAI 兼容接口预设",
        "type": "OPENAI",
        "logo": f"{_ICON}/minimax-color.svg",
        "baseUrl": "https://api.minimaxi.com/v1",
        "defaultModelId": "",
        "completionsPath": None,
        "modelsEndpoint": "https://api.minimaxi.com/v1/models",
        "headersJson": "{}",
        "extraJson": "{}",
    },
    {
        "code": "dashscope-compatible",
        "name": "DashScope 兼容模式",
        "description": "阿里百炼 OpenAI 兼容模式，原生能力仍建议使用 DashScope 类型",
        "type": "DASHSCOPE",
        "logo": f"{_ICON}/bailian-color.svg",
        "baseUrl": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "defaultModelId": "qwen-plus",
        "completionsPath": None,
        "modelsEndpoint": "https://dashscope.aliyuncs.com/compatible-mode/v1/models",
        "headersJson": "{}",
        "extraJson": "{}",
    },
    {
        "code": "siliconflow-cn",
        "name": "SiliconFlow CN",
        "description": "硅基流动国内 OpenAI 兼容接口预设",
        "type": "OPENAI",
        "logo": f"{_ICON}/siliconcloud-color.svg",
        "baseUrl": "https://api.siliconflow.cn/v1",
        "defaultModelId": "",
        "completionsPath": None,
        "modelsEndpoint": "https://api.siliconflow.cn/v1/models?sub_type=chat",
        "headersJson": "{}",
        "extraJson": "{}",
    },
    {
        "code": "openrouter",
        "name": "OpenRouter",
        "description": "OpenRouter 聚合平台 OpenAI 兼容接口预设，可按需补充 HTTP-Referer 与 X-Title",
        "type": "OPENAI",
        "logo": f"{_ICON}/openrouter.svg",
        "baseUrl": "https://openrouter.ai/api/v1",
        "defaultModelId": "",
        "completionsPath": None,
        "modelsEndpoint": "https://openrouter.ai/api/v1/models",
        "headersJson": '{"HTTP-Referer":"","X-Title":"LengBot"}',
        "extraJson": "{}",
    },
    {
        "code": "modelscope",
        "name": "ModelScope",
        "description": "魔搭社区推理聚合 OpenAI 兼容接口预设",
        "type": "OPENAI",
        "logo": f"{_ICON}/modelscope-color.svg",
        "baseUrl": "https://api-inference.modelscope.cn/v1",
        "defaultModelId": "",
        "completionsPath": None,
        "modelsEndpoint": "https://api-inference.modelscope.cn/v1/models",
        "headersJson": "{}",
        "extraJson": "{}",
    },
    {
        "code": "zai",
        "name": "Z.AI",
        "description": "Z.AI OpenAI 兼容接口预设",
        "type": "OPENAI",
        "logo": f"{_ICON}/zai.svg",
        "baseUrl": "https://api.z.ai/api/paas/v4",
        "defaultModelId": "",
        "completionsPath": None,
        "modelsEndpoint": "https://api.z.ai/api/paas/v4/models",
        "headersJson": "{}",
        "extraJson": "{}",
    },
    {
        "code": "volcengine-agent-plan",
        "name": "火山 AgentPlan",
        "description": "火山引擎 Ark Coding OpenAI 兼容接口预设",
        "type": "OPENAI",
        "logo": None,
        "baseUrl": "https://ark.cn-beijing.volces.com/api/coding/v3",
        "defaultModelId": "ark-code-latest",
        "completionsPath": "/chat/completions",
        "modelsEndpoint": "https://ark.cn-beijing.volces.com/api/coding/v3/models",
        "headersJson": "{}",
        "extraJson": "{}",
    },
]


# ----------------------------------------------------------------------
# 模型类型推断（fetch-models 用）
# ----------------------------------------------------------------------

# 靠模型名里的关键词猜类型。**照抄 Java `FetchedModel.inferType`**，别自己加规则：
# OpenAI 兼容的 /models 接口只回一个 id，没有类型字段，只能靠名字猜。
#
# 第一版我自作聪明加了 vision 判断（vl / 4o → multimodal），但 `ModelType` 枚举
# **根本没有 multimodal 这个值**（只有 llm/embedding/rerank/tts/stt），
# 前端拿到未知类型会渲染成空白标签。这类地方宁可少猜，也不要编。
_TYPE_HINTS: list[tuple[tuple[str, ...], str]] = [
    (("embedding", "embed"), "embedding"),
    (("rerank",), "rerank"),
    (("tts",), "tts"),
    (("stt", "whisper"), "stt"),
]

# 只认这 5 种，和 Java ModelType 枚举一一对应
MODEL_TYPES: dict[str, str] = {
    "llm": "对话模型",
    "embedding": "嵌入模型",
    "rerank": "重排模型",
    "tts": "语音合成",
    "stt": "语音识别",
}


def infer_model_type(model_id: str) -> str:
    mid = (model_id or "").lower()
    for hints, code in _TYPE_HINTS:
        if any(h in mid for h in hints):
            return code
    return "llm"


# 反直觉但**刻意保持**的行为：`BAAI/bge-large-zh-v1.5` 会被猜成 `llm`，
# 因为名字里既没有 "embedding" 也没有 "embed"（只有 bge 这个缩写）。
# 库里那条记录的 type=embedding 是当初**人工选的**，不是推断出来的。
# 别"顺手优化"成按 bge/m3e 等缩写匹配 —— 那样 Python 版和 Java 版的
# 拉取结果就不一致了，同一个模型在两边显示不同类型的标签。
