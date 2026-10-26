from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """配置来源：.env（优先）> 环境变量 > 默认值。

    对照 Java：相当于 @ConfigurationProperties + application.yml，
    但没有 profile 机制 —— Python 侧用不同 .env 文件区分环境。
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = "postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/lengbot_py"
    dev_user_id: int = 2090273750973657090
    db_echo: bool = False

    # ---- 认证开关（auth gate）----
    # False（开发态，默认）：与收口前完全一致 —— login 不校验密码、
    #   get_current_user_id 恒返回 dev_user_id，33 项回归脚本无需带 token。
    # True（上线态）：login 真校验 BCrypt、token 必须有效（Redis 有状态），
    #   未认证请求一律 401；API Key（lbkey_）也可作为身份凭据。
    # 两个状态共用同一套登录/登出端点，切换不改前端。
    auth_enabled: bool = False

    # ---- Neo4j（知识图谱；与 Java 同一台实例，label 前缀隔离见 graph_service）----
    neo4j_uri: str = "bolt://127.0.0.1:7687"
    neo4j_username: str = "neo4j"
    neo4j_password: str = ""

    # ---- Redis（token 会话存储；与 Java 同一台实例，独立 key 前缀不冲突）----
    # 密码不要写死默认值（源码进版本库会泄露）。真实值从 .env 的 REDIS_URL 来。
    redis_url: str = "redis://localhost:9736/9"
    # token 有效期（秒），Java Sa-Token 默认 30 天同值
    token_ttl_seconds: int = 30 * 24 * 3600
    # 文件工具的沙箱根目录。所有 local_* 工具的路径都被限制在这下面，
    # 防止模型用 `../../` 读写工作区之外的文件。
    workspace_root: str = "workspace"

    # ---- 工作流 api 节点的网络策略 ----
    # 默认 False：api 节点拒绝访问回环/内网/链路本地地址（防 SSRF）。
    # 自托管部署要调**自家内网服务**时改成 true —— 这是唯一的正规开关，
    # 刻意不做成节点字段，否则画工作流的人就能自己关掉防护。
    workflow_allow_private_network: bool = False

    # ---- Embedding 配置 ----
    # 注意：库里 embedding.model_name 字段写的是 Qwen/Qwen3-VL-Embedding-8B，
    # 但实测反推（scripts/rag_model_probe.py）实际是 BAAI/bge-large-zh-v1.5
    # —— 相似度 1.000000。元数据是错的，以实测为准。
    embedding_provider_id: int = 2094705335903604737
    embedding_model: str = "BAAI/bge-large-zh-v1.5"
    embedding_dimension: int = 1024
    # bge 系列原生就是 1024 维，不需要 dimensions 参数；
    # Qwen3 系列默认 4096，必须传 dimensions=1024 才能和库对上。
    embedding_use_dimensions: bool = False

    # ---- 联网搜索（web_search 工具）----
    # 对齐 Java 的 `lengbot.tavily.api-key`。Java 那边在 yml 里留了一个默认 key，
    # Python 版**不写默认值** —— 密钥只从环境变量来，避免又一次"key 随代码进版本库"
    # （Java 的 application.yml 里正是为这件事留了一段注释）。
    tavily_api_key: str = ""

    # ---- MinIO 对象存储（文档原始文件 / 解析后的 markdown / 文档内图片）----
    # 端口是 9100 不是默认 9000 —— 宿主机的 9000 被 portainer 占了（Java 侧注释有写）。
    # 实测 http://localhost:9100/minio/health/live 返回 200。
    minio_endpoint: str = "http://localhost:9100"
    # 真实 AK/SK 从 .env 来，不要写死默认值
    minio_access_key: str = ""
    minio_secret_key: str = ""
    minio_bucket: str = "lengbot"

    # ---- GitHub 远程 Skill ----
    # 对齐 Java 的 `lengbot.github.token`。不配也能用，但 GitHub API
    # 对匿名请求限流 60 次/小时，稍微点几下列表就 403。
    github_token: str = ""

    # ---- 文档解析 ----
    # 上传后是否同步完成"解析 + 分块 + 向量化"。
    # Java 是丢 Redis 队列异步跑（有进度条）；Python 版同步跑，
    # 因为一本书向量化要 3-10 分钟，异步没有任务表支撑会丢任务。
    # 设 false 时上传只落文件，需要手动调 /ingest 触发入库。
    ingest_inline: bool = True


@lru_cache
def get_settings() -> Settings:
    """lru_cache 让 Settings 只解析一次（相当于 Spring 的单例 Bean）。"""
    return Settings()


settings = get_settings()
