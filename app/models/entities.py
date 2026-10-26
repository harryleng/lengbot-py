from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Date,
    Boolean,
    DateTime,
    Double,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """所有 ORM 模型的基类（对照 Java：MyBatis-Plus 的实体约定）。

    注意：这里刻意**只映射需要的表**，不追求 100% 覆盖。
    表结构以 dump 恢复出来的真库为准，不要凭想象加字段。
    """


class ChatSession(Base):
    """会话表。事实来源之一：会话元数据都在这里。

    与 Java 侧 `com.lengbot.entity.ChatSession` 一一对应。
    """

    __tablename__ = "chat_session"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    agent_id: Mapped[int | None] = mapped_column(BigInteger)
    agent_version_id: Mapped[int | None] = mapped_column(BigInteger)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    title: Mapped[str | None] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, nullable=False, default="active")
    context: Mapped[dict | None] = mapped_column(JSONB)
    message_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime)
    pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # 实际存的是 jsonb 数组（[{id,type,...}]），不是 dict。
    # 之前误标成 dict，导致 model_validate 遇到真实 list 数据直接 ValidationError。
    attachments: Mapped[list | None] = mapped_column(JSONB)
    create_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    update_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    deleted: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)


class Message(Base):
    """消息表。**这是整套系统的事实来源** —— 决策已定：

    LangGraph 只当执行引擎，聊天历史永远以这张表为准，
    不走 checkpoint 的 blob。代价是每个 node 结束时要落库，
    换来的是前端分页/搜索/token 统计全都能用 SQL 做。
    """

    __tablename__ = "message"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    session_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    role: Mapped[str] = mapped_column(String, nullable=False)
    content: Mapped[str | None] = mapped_column(Text)
    # 注意：这几列在库里是 NOT NULL + 有 DEFAULT，但 SQLAlchemy 会**显式插入 NULL**
    # （它把每个映射列都写进 INSERT 语句），数据库默认值不会生效。
    # 所以必须在模型层给 default，否则 INSERT 直接报 not-null violation。
    content_type: Mapped[str] = mapped_column(String, nullable=False, default="text")
    token_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 注意：属性名不能叫 metadata —— 那是 SQLAlchemy DeclarativeBase 的保留属性
    msg_metadata: Mapped[dict | None] = mapped_column("metadata", JSONB)
    tool_events: Mapped[dict | None] = mapped_column(JSONB)
    parent_id: Mapped[int | None] = mapped_column(BigInteger)
    create_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    message_type: Mapped[str] = mapped_column(String, nullable=False, default="text")
    reply_to_message_id: Mapped[int | None] = mapped_column(BigInteger)
    starred: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    @property
    def metadata_for_api(self) -> dict | None:
        """给 Pydantic 用的出口。

        `metadata` 是 SQLAlchemy DeclarativeBase 的保留属性（存表定义），
        不能直接暴露给 API 层，这里换个名字让 schema 取。
        """
        return self.msg_metadata


class ToolCall(Base):
    """工具调用记录表（**不是** message.tool_calls 那一列，那列 2026-06-24 已被删）。

    这张表存在的唯一理由：**完整工具结果太大，不能全塞进 message**。
    两处存储各司其职：

    - `message.tool_events`（JSONB）—— 给前端渲染工具卡片用的事件流，
      历史列表里还会被「瘦身」（>500 字的结果正文删掉，只留 `resultTotalLength`）。
    - `tool_calls.tool_output`（TEXT）—— 完整原文，前端点「查看结果」时按 id 回查。

    主键 `id` 是**业务预生成**的雪花 ID，不是自增 —— 工具一开跑就得把这个 id
    塞进 SSE 的 `tool_call` / `tool_result` 事件，前端才能拿它去回查。
    对照 Java：`appendToolCallStart` 里的 `IdWorker.getId()`，注释原文
    「tool_call/tool_result 事件 + tool_calls 表主键共用同一 id」。

    注意 `tool_input` 在 Java 侧是 String + JsonbTypeHandler（写进 JSONB 列后被 PG
    解析成对象），这里直接存解析后的 dict，落库形态一致。
    """

    __tablename__ = "tool_calls"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # 收尾时才回填：执行工具那一刻 assistant 消息还没 insert
    message_id: Mapped[int | None] = mapped_column(BigInteger)
    tool_name: Mapped[str] = mapped_column(String(100), nullable=False)
    tool_input: Mapped[dict | list | str | None] = mapped_column(JSONB)
    tool_output: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class Agent(Base):
    """Agent 配置（阶段 1 只读：拿 system_prompt + 模型参数）。

    刻意只映射需要的列 —— 表里还有几十个字段，全映射过来只会增加心智负担。
    SQLAlchemy 允许映射子集，只要你不 INSERT（缺列会走数据库默认值）。
    """

    __tablename__ = "agent"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(BigInteger)
    name: Mapped[str | None] = mapped_column(String)
    description: Mapped[str | None] = mapped_column(Text)
    avatar: Mapped[str | None] = mapped_column(String)
    # chat / workflow / digital_human —— 决定走 LLM 中间件链还是工作流 DAG
    agent_type: Mapped[str | None] = mapped_column(String)
    system_prompt: Mapped[str | None] = mapped_column(Text)
    config: Mapped[dict | None] = mapped_column(JSONB)
    status: Mapped[str | None] = mapped_column(String)
    is_default: Mapped[bool | None] = mapped_column(Boolean)
    # 欢迎语 / 推荐问题：聊天页首屏直接渲染这两项
    welcome_message: Mapped[str | None] = mapped_column(Text)
    recommended_questions: Mapped[dict | None] = mapped_column(JSONB)
    # 做「Agent 管理」时补的 4 列。`deleted` 尤其重要 ——
    # 原来没映射它，列表只能靠 `status != 'archived'` 过滤，
    # **软删的 Agent 会照样出现在列表里**（真 bug，见 agent_service.py）。
    icon: Mapped[str | None] = mapped_column(String)
    publish_time: Mapped[datetime | None] = mapped_column(DateTime)
    version: Mapped[int | None] = mapped_column(Integer)
    create_time: Mapped[datetime | None] = mapped_column(DateTime)
    update_time: Mapped[datetime | None] = mapped_column(DateTime)
    deleted: Mapped[int] = mapped_column(SmallInteger, default=0)


class AgentVersion(Base):
    """Agent 版本快照（工作流草稿/发布版、Chat 型的历史版本）。

    两处存格式不同，读的时候要注意：
    - workflow 型：`config = {"graph": {...}}`
    - chat 型：`config = {"kind": "chat", "payload": {...}}`（三层嵌套）
    详情接口要原样返回，不要在服务端"统一格式" —— 前端两套解析逻辑都在。
    """

    __tablename__ = "agent_version"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    agent_id: Mapped[int | None] = mapped_column(BigInteger)
    user_id: Mapped[int | None] = mapped_column(BigInteger)
    version: Mapped[int | None] = mapped_column(Integer)
    config: Mapped[dict | None] = mapped_column(JSONB)
    node_count: Mapped[int | None] = mapped_column(Integer)
    edge_count: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str | None] = mapped_column(String)  # draft / published
    description: Mapped[str | None] = mapped_column(Text)
    publish_time: Mapped[datetime | None] = mapped_column(DateTime)
    create_time: Mapped[datetime | None] = mapped_column(DateTime)
    update_time: Mapped[datetime | None] = mapped_column(DateTime)
    deleted: Mapped[int] = mapped_column(SmallInteger, default=0)


class User(Base):
    """用户表（表名 `users`，不是 `user` —— PG 里 user 是保留字）。

    `password` 是 **BCrypt 哈希**（Java Sa-Token 的 BCrypt，`$2a$10$...` 前缀）。
    bcrypt 库对 `$2a$` / `$2b$` 前缀都能验 —— Java 写入的存量哈希 Python 可直接校验。
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str | None] = mapped_column(String)
    email: Mapped[str | None] = mapped_column(String)
    nickname: Mapped[str | None] = mapped_column(String)
    avatar: Mapped[str | None] = mapped_column(String)
    phone: Mapped[str | None] = mapped_column(String)
    role: Mapped[str | None] = mapped_column(String)
    status: Mapped[str | None] = mapped_column(String)
    password: Mapped[str | None] = mapped_column(String(256))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime)
    create_time: Mapped[datetime | None] = mapped_column(DateTime)
    update_time: Mapped[datetime | None] = mapped_column(DateTime)
    deleted: Mapped[int] = mapped_column(SmallInteger, default=0)
    # 个人资料 + 个人偏好混在一起：头像框/等级 与 longMemory* 共用这一列。
    # 写回时必须整列覆盖，所以「先读全量 → 改目标 key → 整列写回」是硬要求。
    config: Mapped[dict | None] = mapped_column(JSONB)


class ModelProvider(Base):
    """模型供应商。base_url + api_key + type，全部走 OpenAI 兼容协议调用。

    注意：这张表里的 api_key 在 Java 侧是明文落库（2026-09-14 撤销了 AES-GCM 加密层），
    所以这里直接读明文 —— 与 lengbot 现状保持一致，不要自己加解密。
    """

    __tablename__ = "model_provider"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str | None] = mapped_column(String)
    type: Mapped[str | None] = mapped_column(String)
    api_key: Mapped[str | None] = mapped_column(String)
    base_url: Mapped[str | None] = mapped_column(String)
    config: Mapped[dict | None] = mapped_column(JSONB)
    status: Mapped[str | None] = mapped_column(String)
    # 下面三列原来没映射，做「模型供应商管理」时才补上 —— 少了它们，
    # 列表页拿不到用户自己配的 models_endpoint / headers，前端会显示成空。
    # 注意库里列名是下划线、接口要出 camelCase，靠 SQLAlchemy 属性名 + Pydantic 别名处理。
    models_endpoint: Mapped[str | None] = mapped_column(String)
    headers_json: Mapped[dict | None] = mapped_column(JSONB)
    extra_json: Mapped[dict | None] = mapped_column(JSONB)
    create_time: Mapped[datetime | None] = mapped_column(DateTime)
    update_time: Mapped[datetime | None] = mapped_column(DateTime)
    deleted: Mapped[int] = mapped_column(SmallInteger, default=0)


class ModelRecord(Base):
    """模型表（provider 下挂的具体模型）。

    叫什么：表名是 `model`，但 `Model` 在代码里太容易和"模型实例"混淆
    （`llm_client.LlmConfig` 才是真正用来发请求的东西），所以实体叫 `ModelRecord`，
    表名照旧 `model`。

    这张表**不是**从模型列表接口自动同步的，是用户在「模型管理」里手工维护的
    （或者 `fetch-models` 拉下来后手工勾选）。所以 `type` 是人工确认过的，
    比 `infer_model_type` 靠名字猜的准 —— 检索时选 embedding 模型要看这一列。
    """

    __tablename__ = "model"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    provider_id: Mapped[int | None] = mapped_column(BigInteger)
    model_id: Mapped[str | None] = mapped_column(String)
    name: Mapped[str | None] = mapped_column(String)
    type: Mapped[str | None] = mapped_column(String)
    status: Mapped[str | None] = mapped_column(String)
    dimension: Mapped[int | None] = mapped_column(Integer)
    create_time: Mapped[datetime | None] = mapped_column(DateTime)
    update_time: Mapped[datetime | None] = mapped_column(DateTime)
    deleted: Mapped[int] = mapped_column(SmallInteger, default=0)


class McpServer(Base):
    """MCP 服务器（外部工具来源）。

    没有 `mcp_tool` 表 —— 工具列表是**运行时从 server 拉的**，不落库
    （Java 侧 `McpClientService.getToolsWithCache`，走 Redis 缓存）。所以只要
    不接 MCP 客户端，`/mcp-servers/{id}/tools` 就无从取数。

    `disabled_tools` 是个 jsonb **数组**，存被用户关掉的工具名。
    `is_builtin` 是 **smallint**（1/0），不是 bool —— 前端判断是
    `Number(server.isBuiltin) === 1`，给字符串 "1" 也能过（Number 会转）。
    """

    __tablename__ = "mcp_server"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str | None] = mapped_column(String)
    description: Mapped[str | None] = mapped_column(String)
    install_type: Mapped[str | None] = mapped_column(String)   # npx / uvx / sse / builtin
    deploy_config: Mapped[dict | None] = mapped_column(JSONB)  # {packageName, args, env}
    detail_config: Mapped[dict | None] = mapped_column(JSONB)
    host: Mapped[str | None] = mapped_column(String)           # sse/http 的地址
    status: Mapped[str | None] = mapped_column(String)
    user_id: Mapped[int | None] = mapped_column(BigInteger)
    transport: Mapped[str | None] = mapped_column(String)      # stdio / sse / streamable_http
    headers: Mapped[dict | None] = mapped_column(JSONB)
    disabled_tools: Mapped[list | None] = mapped_column(JSONB)
    icon: Mapped[str | None] = mapped_column(String)
    create_time: Mapped[datetime | None] = mapped_column(DateTime)
    update_time: Mapped[datetime | None] = mapped_column(DateTime)
    deleted: Mapped[int] = mapped_column(SmallInteger, default=0)
    last_sync_time: Mapped[datetime | None] = mapped_column(DateTime)
    is_builtin: Mapped[int] = mapped_column(SmallInteger, default=0)


class SystemConfig(Base):
    """系统配置表（key-value）。

    两个和别处不一样的地方：
    1. **主键是 `config_key` 字符串，不是雪花 id** —— 这张表没有 id 列，
       键就是业务标识（`default_chat_model` 之类）。
    2. `config_value` 是 **text**，里面存的是 **JSON 文本**，不是 jsonb。
       （实测：`jsonb_typeof(config_value)` 直接报 "does not exist"，
       说明这列从来就不是 jsonb。）所以读写都要自己 `json.loads` / `dumps`。
    """

    __tablename__ = "system_config"

    config_key: Mapped[str] = mapped_column(String, primary_key=True)
    config_value: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(String)
    create_time: Mapped[datetime | None] = mapped_column(DateTime)
    update_time: Mapped[datetime | None] = mapped_column(DateTime)


class WorkflowTestRun(Base):
    """工作流编排页的「调试运行」记录（对齐 `workflow_test_run` DDL）。

    为什么要落库而不是只留在内存：用户调完一遍流程，关掉抽屉再打开还想看
    上一次每个节点吐了什么。Java 侧保留**每个 agent 最近 50 条**，超了删最旧的
    （`MAX_RETAIN_PER_AGENT`），这里照抄。

    `run_id` 是 32 位无横线 UUID 且**全局唯一**（`uk_workflow_test_run_run_id`）——
    和 `suspended_runs` 里的 runId 同源，HITL 恢复时靠它找回这条记录。
    """

    __tablename__ = "workflow_test_run"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[str] = mapped_column(String)
    agent_id: Mapped[int] = mapped_column(BigInteger)
    user_id: Mapped[int] = mapped_column(BigInteger)
    # generation（文本生成）/ conversation（多轮对话）
    test_mode: Mapped[str] = mapped_column(String, default="generation")
    used_draft: Mapped[int] = mapped_column(SmallInteger, default=1)
    status: Mapped[str] = mapped_column(String, default="running")
    user_input: Mapped[str | None] = mapped_column(Text)
    output: Mapped[str | None] = mapped_column(Text)
    node_events: Mapped[list | None] = mapped_column(JSONB)
    variables: Mapped[dict | None] = mapped_column(JSONB)
    workflow_graph: Mapped[dict | None] = mapped_column(JSONB)
    error_info: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(BigInteger)
    start_time: Mapped[datetime | None] = mapped_column(DateTime)
    end_time: Mapped[datetime | None] = mapped_column(DateTime)
    create_time: Mapped[datetime | None] = mapped_column(DateTime)
    update_time: Mapped[datetime | None] = mapped_column(DateTime)
    deleted: Mapped[int] = mapped_column(SmallInteger, default=0)


class Tool(Base):
    """工具定义表。

    目前 26 个 builtin + 6 个 knowledge 工具。这张表是**前端工具管理页面的产物**，
    所以 `input_schema` 里存的是完整 JSON Schema。

    这里有个真实工程问题：Java 侧的工具 schema 存在**两处**
    （代码里的 `@ToolParam` 注解、数据库里的 `input_schema`），
    靠 `BuiltinToolRegistrar` 同步，很容易漂移。
    Python 侧只有一处（函数类型注解），但为了和前端配置对齐，
    仍然读库里的记录来"决定这个 Agent 能用哪些工具"，
    读到的 `input_schema` 只用于**校验**（见 tools/registry.py 的 verify_schema）。
    """

    __tablename__ = "tool"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(BigInteger)
    name: Mapped[str | None] = mapped_column(String)
    display_name: Mapped[str | None] = mapped_column(String)
    description: Mapped[str | None] = mapped_column(Text)
    tool_type: Mapped[str | None] = mapped_column(String)
    input_schema: Mapped[dict | None] = mapped_column(JSONB)
    output_schema: Mapped[dict | None] = mapped_column(JSONB)
    config: Mapped[dict | None] = mapped_column(JSONB)
    status: Mapped[str | None] = mapped_column(String)
    icon: Mapped[str | None] = mapped_column(String)
    # 做「工具管理」时补的 8 列（原来只映射 13 列，表里是 21 列）。
    # 缺这些列做列表页会显示成空，而且 SQLAlchemy **不报错**，很难发现。
    endpoint_url: Mapped[str | None] = mapped_column(String)   # 自定义 HTTP 工具的地址
    auth_type: Mapped[str | None] = mapped_column(String)      # none / bearer / api_key
    auth_config: Mapped[dict | None] = mapped_column(JSONB)
    tags: Mapped[list | None] = mapped_column(JSONB)           # 前端按 tag 过滤（JSONB @>）
    output_example: Mapped[dict | None] = mapped_column(JSONB)
    rate_limit_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    rate_limit_config: Mapped[dict | None] = mapped_column(JSONB)
    create_time: Mapped[datetime | None] = mapped_column(DateTime)
    update_time: Mapped[datetime | None] = mapped_column(DateTime)
    deleted: Mapped[int] = mapped_column(SmallInteger, default=0)


class Prompt(Base):
    """Prompt 主表（评测 / Playground 用）。

    注意 **`latest_version` 是 varchar 不是 int** —— 版本号允许 `"v1"` / `"1.0"`
    这种写法（库里实际两种都有：`v2` 和 `1.0`）。所以不能当数字排序，
    列表 `ORDER BY create_time` 才是对的。
    """

    __tablename__ = "prompt"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    prompt_key: Mapped[str] = mapped_column(String)
    description: Mapped[str | None] = mapped_column(String)
    latest_version: Mapped[str | None] = mapped_column(String)
    tags: Mapped[str | None] = mapped_column(String)
    user_id: Mapped[int | None] = mapped_column(BigInteger)
    create_time: Mapped[datetime | None] = mapped_column(DateTime)
    update_time: Mapped[datetime | None] = mapped_column(DateTime)
    deleted: Mapped[int] = mapped_column(SmallInteger, default=0)


class PromptVersion(Base):
    """Prompt 版本快照。

    `variables` / `model_config` / `tool_config` 在库里是 **jsonb**，
    但 Java 实体用 `JsonbTypeHandler` 把它们当 **String** 读写，
    所以 API 的入参和出参都是 **JSON 字符串**（不是对象）。
    前端两种都兼容（`typeof x === 'string' ? JSON.parse(x) : x`），
    这里跟 Java 保持一致给字符串 —— 出参形态和 Java 版一模一样，
    切换后端时前端不用改。
    """

    __tablename__ = "prompt_version"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    prompt_key: Mapped[str] = mapped_column(String)
    version: Mapped[str] = mapped_column(String)
    version_desc: Mapped[str | None] = mapped_column(String)
    template: Mapped[str] = mapped_column(Text)
    variables: Mapped[dict | list | None] = mapped_column(JSONB)
    model_config: Mapped[dict | None] = mapped_column(JSONB)
    tool_config: Mapped[dict | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String, default="pre")  # pre / release
    user_id: Mapped[int | None] = mapped_column(BigInteger)
    create_time: Mapped[datetime | None] = mapped_column(DateTime)
    update_time: Mapped[datetime | None] = mapped_column(DateTime)
    deleted: Mapped[int] = mapped_column(SmallInteger, default=0)


class PromptBuildTemplate(Base):
    """Prompt 构建模板（Playground 里的「套模板」）。

    一个不一致点：`variables` 是 **varchar**（字符串），
    而 `model_config` / `tool_config` 是 **jsonb**。
    同一张表里混着两种存储，照 Java 实体的声明抄的，别"统一"成一种。
    """

    __tablename__ = "prompt_build_template"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    prompt_template_key: Mapped[str] = mapped_column(String)
    tags: Mapped[str | None] = mapped_column(String)
    template_desc: Mapped[str | None] = mapped_column(String)
    template: Mapped[str] = mapped_column(Text)
    variables: Mapped[str | None] = mapped_column(String)
    model_config: Mapped[dict | None] = mapped_column(JSONB)
    tool_config: Mapped[dict | None] = mapped_column(JSONB)
    create_time: Mapped[datetime | None] = mapped_column(DateTime)
    update_time: Mapped[datetime | None] = mapped_column(DateTime)
    deleted: Mapped[int] = mapped_column(SmallInteger, default=0)


class SubAgent(Base):
    """子智能体定义（可被主 Agent 通过 delegate 工具调用）。

    `tool_ids` 是 jsonb 数组，但 Java 实体当 String 读写（JsonbTypeHandler），
    所以 API 层同样是**字符串**。
    `enabled` / `is_builtin` 是 **smallint(0/1)**，不是 bool —— 前端用
    `Number(x) === 1` 判断，给 true/false 会全部判成 false。
    """

    __tablename__ = "subagent"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String)
    display_name: Mapped[str] = mapped_column(String)
    description: Mapped[str] = mapped_column(Text)
    system_prompt: Mapped[str] = mapped_column(Text)
    tool_ids: Mapped[list | None] = mapped_column(JSONB)
    model_id: Mapped[int | None] = mapped_column(BigInteger)
    llm_model: Mapped[str | None] = mapped_column(String)
    connect_timeout_seconds: Mapped[int] = mapped_column(Integer, default=10)
    read_timeout_seconds: Mapped[int] = mapped_column(Integer, default=60)
    model_retry_times: Mapped[int] = mapped_column(Integer, default=1)
    enabled: Mapped[int] = mapped_column(SmallInteger, default=1)
    is_builtin: Mapped[int] = mapped_column(SmallInteger, default=0)
    icon: Mapped[str | None] = mapped_column(String)
    user_id: Mapped[int | None] = mapped_column(BigInteger)
    create_time: Mapped[datetime | None] = mapped_column(DateTime)
    update_time: Mapped[datetime | None] = mapped_column(DateTime)
    deleted: Mapped[int] = mapped_column(SmallInteger, default=0)


class SubAgentRun(Base):
    """子代理任务运行记录（Java 侧 `delegate` 工具写，Python 版只读）。

    `request_id` 才是业务 id —— 前端的 `taskId` 指的就是它，不是主键 `id`。
    拿 `id` 去查会 404，这个很容易搞错。
    """

    __tablename__ = "subagent_run"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    thread_id: Mapped[str | None] = mapped_column(String)
    parent_thread_id: Mapped[str | None] = mapped_column(String)
    subagent_name: Mapped[str | None] = mapped_column(String)
    task: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str | None] = mapped_column(String)
    request_id: Mapped[str | None] = mapped_column(String)
    batch_id: Mapped[str | None] = mapped_column(String)
    parent_request_id: Mapped[str | None] = mapped_column(String)
    parent_session_id: Mapped[int | None] = mapped_column(BigInteger)
    mode: Mapped[str | None] = mapped_column(String)
    cancel_requested: Mapped[int] = mapped_column(SmallInteger, default=0)
    reply: Mapped[str | None] = mapped_column(Text)
    tool_call_count: Mapped[int | None] = mapped_column(Integer)
    start_time: Mapped[datetime | None] = mapped_column(DateTime)
    end_time: Mapped[datetime | None] = mapped_column(DateTime)
    error_message: Mapped[str | None] = mapped_column(Text)
    create_time: Mapped[datetime | None] = mapped_column(DateTime)
    update_time: Mapped[datetime | None] = mapped_column(DateTime)


class SubAgentTaskBatch(Base):
    """子代理任务批次（一次 delegate 调用产生一批）。"""

    __tablename__ = "subagent_task_batch"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    batch_id: Mapped[str] = mapped_column(String)
    parent_request_id: Mapped[str | None] = mapped_column(String)
    parent_thread_id: Mapped[str | None] = mapped_column(String)
    parent_session_id: Mapped[int | None] = mapped_column(BigInteger)
    mode: Mapped[str | None] = mapped_column(String)
    aggregation: Mapped[str | None] = mapped_column(String)
    status: Mapped[str | None] = mapped_column(String)
    total_count: Mapped[int] = mapped_column(Integer, default=0)
    completed_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    cancelled_count: Mapped[int] = mapped_column(Integer, default=0)
    cancel_requested: Mapped[int] = mapped_column(SmallInteger, default=0)
    create_time: Mapped[datetime | None] = mapped_column(DateTime)
    update_time: Mapped[datetime | None] = mapped_column(DateTime)


class SubAgentTaskEvent(Base):
    """子代理任务的运行事件（游标就是自增主键 `id`）。"""

    __tablename__ = "subagent_task_event"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    task_id: Mapped[str | None] = mapped_column(String)
    batch_id: Mapped[str | None] = mapped_column(String)
    event_type: Mapped[str | None] = mapped_column(String)
    payload: Mapped[dict | None] = mapped_column(JSONB)
    create_time: Mapped[datetime | None] = mapped_column(DateTime)


class MessageFeedback(Base):
    """消息反馈（👍 like / 👎 dislike）。

    **一人一条**：`(user_id, message_id)` 唯一约束在业务层保证
    （DB 没有唯一索引，Java 靠先查后写）。重复提交是 toggle 语义 ——
    同类型删除、不同类型切换，详见 `MessageFeedbackService.submit`。

    `agent_id` / `agent_version` 是**提交时的快照**，不是实时关联：
    后面 Agent 改名或删掉，统计报表仍然要能指向当时是哪个版本给的回答。
    """

    __tablename__ = "message_feedback"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    message_id: Mapped[int] = mapped_column(BigInteger)
    user_id: Mapped[int] = mapped_column(BigInteger)
    # like / dislike
    rating: Mapped[str] = mapped_column(String)
    reason: Mapped[str | None] = mapped_column(Text)
    agent_id: Mapped[int | None] = mapped_column(BigInteger)
    agent_version: Mapped[int | None] = mapped_column(Integer)
    create_time: Mapped[datetime | None] = mapped_column(DateTime)


# ======================================================================
# 评测（阶段 14）
# ======================================================================
# 七张表对应三组接口：
#   评测集  eval_dataset / eval_dataset_item / eval_dataset_version
#   评估器  eval_evaluator / eval_evaluator_version（+ eval_evaluator_template）
#   实验    eval_experiment / eval_experiment_result
#
# **所有 jsonb 字段的 API 形态都是 JSON 字符串**（Java 用 JsonbTypeHandler
# 把 jsonb 列当 String 读写）。前端两种都兼容，但 `ExperimentDetail.vue`
# 里是直接 `JSON.parse(exp.evaluationObjectConfig)` —— 给对象会 TypeError。


class EvalDataset(Base):
    """评测集（一组测试样本的容器）。"""

    __tablename__ = "eval_dataset"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String)
    description: Mapped[str | None] = mapped_column(String)
    # [{key,label,type}] —— 列定义，决定数据项表单长什么样
    columns_config: Mapped[list | None] = mapped_column(JSONB)
    user_id: Mapped[int | None] = mapped_column(BigInteger)
    create_time: Mapped[datetime | None] = mapped_column(DateTime)
    update_time: Mapped[datetime | None] = mapped_column(DateTime)
    deleted: Mapped[int] = mapped_column(SmallInteger, default=0)


class EvalDatasetItem(Base):
    """评测集的一条样本。`data_content` 是 `{列key: 值}` 的对象。"""

    __tablename__ = "eval_dataset_item"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    dataset_id: Mapped[int] = mapped_column(BigInteger)
    data_content: Mapped[dict | None] = mapped_column(JSONB)
    create_time: Mapped[datetime | None] = mapped_column(DateTime)
    update_time: Mapped[datetime | None] = mapped_column(DateTime)
    deleted: Mapped[int] = mapped_column(SmallInteger, default=0)


class EvalDatasetVersion(Base):
    """评测集版本 = **数据项 id 的快照**（不是内容快照）。

    这一点很重要：版本冻结的是"这一刻包含哪些条目"，
    条目内容改了，历史版本看到的内容也会跟着变。
    Java 就是这么设计的（`dataset_items` 存 id 数组）。
    """

    __tablename__ = "eval_dataset_version"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    dataset_id: Mapped[int] = mapped_column(BigInteger)
    version: Mapped[str] = mapped_column(String)
    data_count: Mapped[int] = mapped_column(Integer, default=0)
    # draft / published
    status: Mapped[str] = mapped_column(String, default="draft")
    # id 数组：[2104...]
    dataset_items: Mapped[list | None] = mapped_column(JSONB)
    create_time: Mapped[datetime | None] = mapped_column(DateTime)
    update_time: Mapped[datetime | None] = mapped_column(DateTime)
    deleted: Mapped[int] = mapped_column(SmallInteger, default=0)


class EvalEvaluator(Base):
    """评估器（LLM-as-judge 的容器，真正的提示词在版本里）。"""

    __tablename__ = "eval_evaluator"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String)
    description: Mapped[str | None] = mapped_column(String)
    # 逗号分隔的字符串（不是数组，Java 侧就是 String）
    tags: Mapped[str | None] = mapped_column(String)
    user_id: Mapped[int | None] = mapped_column(BigInteger)
    create_time: Mapped[datetime | None] = mapped_column(DateTime)
    update_time: Mapped[datetime | None] = mapped_column(DateTime)
    deleted: Mapped[int] = mapped_column(SmallInteger, default=0)


class EvalEvaluatorVersion(Base):
    """评估器版本 = 提示词 + 模型配置 + 变量清单。

    `variables` 是**字符串数组**（`["input","actual_output"]`），
    不是对象 —— 它只是告诉前端"这个评估器需要哪些参数"，
    真正的映射关系在实验的 `evaluatorConfig` 里。
    """

    __tablename__ = "eval_evaluator_version"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    evaluator_id: Mapped[int] = mapped_column(BigInteger)
    version: Mapped[str] = mapped_column(String)
    # ⚠️ `model_config` 是 Pydantic 保留名，schema 层要用 alias 绕过（见 schemas/eval.py）
    model_config: Mapped[dict | None] = mapped_column(JSONB)
    prompt: Mapped[str | None] = mapped_column(Text)
    variables: Mapped[list | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String, default="draft")
    create_time: Mapped[datetime | None] = mapped_column(DateTime)
    update_time: Mapped[datetime | None] = mapped_column(DateTime)
    deleted: Mapped[int] = mapped_column(SmallInteger, default=0)


class EvalEvaluatorTemplate(Base):
    """评估器构建模板（预设提示词骨架，给用户套用）。"""

    __tablename__ = "eval_evaluator_template"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    evaluator_template_key: Mapped[str] = mapped_column(String)
    template_desc: Mapped[str | None] = mapped_column(String)
    template: Mapped[str | None] = mapped_column(Text)
    variables: Mapped[str | None] = mapped_column(String)
    model_config: Mapped[dict | None] = mapped_column(JSONB)
    create_time: Mapped[datetime | None] = mapped_column(DateTime)
    update_time: Mapped[datetime | None] = mapped_column(DateTime)
    deleted: Mapped[int] = mapped_column(SmallInteger, default=0)


class EvalExperiment(Base):
    """评测实验：数据集版本 × Prompt 版本 × N 个评估器。

    `evaluation_object_config` / `evaluator_config` 库里是对象，
    但 API 出参是 **JSON 字符串**（前端 `JSON.parse`）。

    `progress` 是 0~100 的整数；`status` 五态：
    draft / running / completed / failed / stopped。
    """

    __tablename__ = "eval_experiment"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String)
    description: Mapped[str | None] = mapped_column(String)
    dataset_id: Mapped[int | None] = mapped_column(BigInteger)
    dataset_version_id: Mapped[int | None] = mapped_column(BigInteger)
    dataset_version: Mapped[str | None] = mapped_column(String)
    evaluation_object_config: Mapped[dict | None] = mapped_column(JSONB)
    evaluator_config: Mapped[list | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String, default="draft")
    progress: Mapped[int] = mapped_column(Integer, default=0)
    complete_time: Mapped[datetime | None] = mapped_column(DateTime)
    user_id: Mapped[int | None] = mapped_column(BigInteger)
    task_id: Mapped[int | None] = mapped_column(BigInteger)
    create_time: Mapped[datetime | None] = mapped_column(DateTime)
    update_time: Mapped[datetime | None] = mapped_column(DateTime)
    deleted: Mapped[int] = mapped_column(SmallInteger, default=0)


class EvalExperimentResult(Base):
    """一条评测结果 = 一个样本 × 一个评估器。

    `score` 是 numeric（PG 的 Decimal），Python 侧要转 float 给前端 ——
    直接把 Decimal 丢给 JSON 会抛 `not JSON serializable`。
    """

    __tablename__ = "eval_experiment_result"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    experiment_id: Mapped[int] = mapped_column(BigInteger)
    input: Mapped[str | None] = mapped_column(Text)
    actual_output: Mapped[str | None] = mapped_column(Text)
    reference_output: Mapped[str | None] = mapped_column(Text)
    score: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    reason: Mapped[str | None] = mapped_column(Text)
    evaluator_version_id: Mapped[int | None] = mapped_column(BigInteger)
    evaluator_name: Mapped[str | None] = mapped_column(String)
    evaluation_time: Mapped[datetime | None] = mapped_column(DateTime)
    create_time: Mapped[datetime | None] = mapped_column(DateTime)
    update_time: Mapped[datetime | None] = mapped_column(DateTime)
    deleted: Mapped[int] = mapped_column(SmallInteger, default=0)


class Knowledge(Base):
    """知识库表。

    对照 Java `com.lengbot.entity.Knowledge`。

    **序列化契约（错了前端白屏）**：
    - `id` / `user_id` 必须输出字符串 —— 19 位雪花 ID 超过 JS 安全整数
    - `config` / `query_params` / `mindmap_data` / `example_questions` 都是 **jsonb**，
      但 Java 侧用 `JsonbTypeHandler` 映射成 `String` 字段再输出，
      所以前端 `KnowledgeDetail.vue` 拿到的是**字符串**，自己 `JSON.parse`。
      Python 侧拿到的已经是 dict/list —— 出参必须 `json.dumps` 回去，
      否则前端 `typeof x === 'string'` 判断失败，走 `|| {}` 分支静默丢配置。
    """

    __tablename__ = "knowledge"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    embedding_model: Mapped[str] = mapped_column(String, nullable=False)
    type: Mapped[str] = mapped_column(String, nullable=False, default="pg")
    config: Mapped[dict | None] = mapped_column(JSONB)
    query_params: Mapped[dict | None] = mapped_column(JSONB)
    document_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String, nullable=False, default="active")
    mindmap_data: Mapped[dict | list | None] = mapped_column(JSONB)
    example_questions: Mapped[list | None] = mapped_column(JSONB)
    graph_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    node_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    edge_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    create_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    update_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    deleted: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)


class KnowledgeMember(Base):
    """知识库成员。权限等级 concept：creator > manager > developer > viewer。

    注意表里**没有 deleted 列** —— 移除成员是真删。
    """

    __tablename__ = "knowledge_member"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    knowledge_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    role: Mapped[str] = mapped_column(String, nullable=False, default="viewer")
    create_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    update_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class Document(Base):
    """知识库文档。

    `status` 状态机：uploading → uploaded → pending → processing → completed / failed。
    前端 `KnowledgeDetail.vue` 用 `PROCESSING_STATUSES = {uploading, pending, processing}`
    决定是否启动 1s 轮询 —— 状态卡在这三个值上会永久轮询，务必保证终态能落地。
    """

    __tablename__ = "document"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    knowledge_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    file_path: Mapped[str | None] = mapped_column(String)
    file_type: Mapped[str | None] = mapped_column(String)
    file_size: Mapped[int | None] = mapped_column(BigInteger)
    file_hash: Mapped[str | None] = mapped_column(String)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    token_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String, nullable=False, default="uploaded")
    error_message: Mapped[str | None] = mapped_column(Text)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSONB)
    markdown_path: Mapped[str | None] = mapped_column(String)
    embedding_json: Mapped[dict | None] = mapped_column(JSONB)
    duplicate_rate: Mapped[float | None] = mapped_column(Numeric)
    duplicate_details: Mapped[dict | list | None] = mapped_column(JSONB)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    last_edit_time: Mapped[datetime | None] = mapped_column(DateTime)
    create_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    update_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    deleted: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)


class Chunk(Base):
    """文档分块。

    `content_tsv` 是 PG 触发器维护的 tsvector（`trg_chunk_content_tsv`），
    **不映射**：写库时由触发器自动算，映射成字段反而会尝试写只读列。

    `status` 只有 chunked / vectorizing / vectorized / failed 四种，
    Advisor 的休眠分块 SQL 却按 `status='completed'` 过滤 —— Java 侧就是这样，
    结果是休眠分块永远查不到数据。Python 版按真实枚举值 `vectorized` 修掉。
    """

    __tablename__ = "chunk"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    document_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    knowledge_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSONB)
    status: Mapped[str] = mapped_column(String, nullable=False, default="chunked")
    parent_content: Mapped[str | None] = mapped_column(Text)
    create_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    update_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class QaPair(Base):
    """知识库问答对。

    `source`：manual / import / ai；`status`：pending / vectorizing / active / failed。
    """

    __tablename__ = "qa_pair"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    knowledge_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False, default="manual")
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    token_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    create_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    update_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    deleted: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)


class KnowledgeGraph(Base):
    """知识图谱抽取任务记录（PG 侧的元数据，真正的点/边在 Neo4j）。"""

    __tablename__ = "knowledge_graph"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    knowledge_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    node_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    edge_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    task_id: Mapped[int | None] = mapped_column(BigInteger)
    error_message: Mapped[str | None] = mapped_column(Text)
    create_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    update_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    deleted: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)


class GraphDocument(Base):
    """「哪个文档已抽过图谱」的台账，供 `graph/existing-docs` 用。"""

    __tablename__ = "graph_document"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    graph_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    document_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    entity_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    relation_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[str | None] = mapped_column(Text)
    create_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    update_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    deleted: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)


class TaskRecord(Base):
    """通用异步任务表（Java 侧 `TaskService` 用）。

    Python 版只用来给评测实验记状态，让前端的"停止"按钮有地方写
    `cancel_requested`，也让失败原因有地方存（前端 `ExperimentDetail`
    在 `status=failed` 时会读 `taskId` 去查错误信息）。

    **不做真正的任务调度**：Python 侧用 `asyncio.create_task` 在进程内跑，
    重启即丢。这是已知限制，写在这里防止以后误以为是持久队列。
    """

    __tablename__ = "task"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str | None] = mapped_column(String)
    type: Mapped[str | None] = mapped_column(String)
    status: Mapped[str | None] = mapped_column(String)
    progress: Mapped[int | None] = mapped_column(Integer)
    message: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict | None] = mapped_column(JSONB)
    result: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    cancel_requested: Mapped[int] = mapped_column(SmallInteger, default=0)
    user_id: Mapped[int | None] = mapped_column(BigInteger)
    ref_id: Mapped[int | None] = mapped_column(BigInteger)
    create_time: Mapped[datetime | None] = mapped_column(DateTime)
    update_time: Mapped[datetime | None] = mapped_column(DateTime)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    deleted: Mapped[int] = mapped_column(SmallInteger, default=0)


class Skill(Base):
    """Skill 表（对齐 Java `com.lengbot.entity.Skill`）。

    Skill 是「编排指令包」：绑定若干 Tool / MCP，再用 `promptTemplate` 告诉主
    Agent 什么时候用、怎么用。真正的文件（SKILL.md 等）不在库里，在 MinIO 的
    `skills/{slug}/` 下，库里只存 `objectPrefix` 和 `contentHash`。

    两个容易踩的点：

    1. **`slug` 有部分唯一索引** `uk_skill_slug ... WHERE slug IS NOT NULL AND deleted=0`。
       所以"删掉的 Skill 的 slug 可以复用"，但活着的不行 —— 判重 SQL 必须带 `deleted=0`，
       否则软删记录会把新建挡住。
    2. **`toolIds` / `mcpServerIds` / `skillDependencies` 是 JSONB 且是字符串数组**
       （19 位雪花 ID 存 JSON number 会精度丢失，所以存字符串）。
       前端 `parseIdArray` 两种形态都吃，出参直接给数组。
    """

    __tablename__ = "skill"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    slug: Mapped[str | None] = mapped_column(String)
    agent_id: Mapped[int | None] = mapped_column(BigInteger)
    tool_ids: Mapped[list | None] = mapped_column(JSONB)
    mcp_server_ids: Mapped[list | None] = mapped_column(JSONB)
    model_id: Mapped[int | None] = mapped_column(BigInteger)
    name: Mapped[str | None] = mapped_column(String)
    display_name: Mapped[str | None] = mapped_column(String)
    icon: Mapped[str | None] = mapped_column(String)
    description: Mapped[str | None] = mapped_column(String)
    prompt_template: Mapped[str | None] = mapped_column(Text)
    config: Mapped[dict | None] = mapped_column(JSONB)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str | None] = mapped_column(String)
    scope: Mapped[str | None] = mapped_column(String)
    is_builtin: Mapped[int] = mapped_column(SmallInteger, default=0)
    content_hash: Mapped[str | None] = mapped_column(String)
    object_prefix: Mapped[str | None] = mapped_column(String)
    version: Mapped[str | None] = mapped_column(String)
    skill_dependencies: Mapped[list | None] = mapped_column(JSONB)
    source_type: Mapped[str | None] = mapped_column(String)
    user_id: Mapped[int | None] = mapped_column(BigInteger)
    create_time: Mapped[datetime | None] = mapped_column(DateTime)
    update_time: Mapped[datetime | None] = mapped_column(DateTime)
    deleted: Mapped[int] = mapped_column(SmallInteger, default=0)


class ApiKey(Base):
    """开放 API 密钥表。

    两个长度不同的"同一个 key"要分清：

    - `key_hash` = sha256(rawKey)，64 位，落库 + 鉴权时反查（唯一索引）
    - `key_prefix` = rawKey 前 12 位 + `****`，只是给列表页"这条是哪把钥匙"用的

    明文 rawKey 只在 create / regenerate 的响应里出现一次，之后不可恢复。
    对照 Java `ApiKeyServiceImpl`：rawKey = `lbkey_` + 32 位 uuid + 16 位 uuid 片段。
    """

    __tablename__ = "api_key"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    key_prefix: Mapped[str] = mapped_column(String(20), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # 出参走 ApiKeyPermission 的 desc（"仅对话"/"完全访问"），Java 用 @JsonValue 标在 desc 上
    permissions: Mapped[str] = mapped_column(String(32), nullable=False, default="chat")
    is_enabled: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    create_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    update_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    deleted: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    agent_ids: Mapped[list | None] = mapped_column(JSONB)
    rate_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    daily_quota: Mapped[int] = mapped_column(Integer, nullable=False, default=100000)
    used_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    # 配额跨天重置标记（Java mapper 原子 SQL 的 CASE WHEN 依赖此列）
    quota_reset_at: Mapped[date | None] = mapped_column(Date)


class TtsVoice(Base):
    """TTS 音色缓存表。

    Java 侧这张表的定位是「Provider 音色清单的本地缓存 + 用户自定义管理字段叠加」：
    Provider 同步只刷新 friendly_name/locale/gender 三个派生字段，
    favorite / voice_group / remark 是用户自己的，upsert 时必须保留。
    """

    __tablename__ = "tts_voice"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    voice_name: Mapped[str] = mapped_column(String(128), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="edge-tts")
    friendly_name: Mapped[str | None] = mapped_column(String(256))
    locale: Mapped[str | None] = mapped_column(String(32))
    gender: Mapped[str | None] = mapped_column(String(16))
    favorite: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    voice_group: Mapped[str | None] = mapped_column(String(64), default="")
    remark: Mapped[str | None] = mapped_column(String(512), default="")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    extra_json: Mapped[dict | None] = mapped_column(JSONB, default=dict)
    create_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    update_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    deleted: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)


class LlmTrace(Base):
    """LLM 调用链追踪表（`/api/observability/*` 的数据源）。

    `trace_source` 决定这条记录能不能在默认列表里露面：
    Java `pageList` 在未指定 source 时只取 `chat` / `workflow` / NULL 三类，
    把 subagent 等辅助调用的 trace 排除掉 —— Python 必须照做，
    否则「调用链列表」会被子 agent 的内部调用刷屏。
    """

    __tablename__ = "llm_trace"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    session_id: Mapped[int | None] = mapped_column(BigInteger)
    user_id: Mapped[int | None] = mapped_column(BigInteger)
    agent_id: Mapped[int | None] = mapped_column(BigInteger)
    agent_name: Mapped[str | None] = mapped_column(String(128))
    model: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="running")
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tool_call_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_duration_ms: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    spans: Mapped[list | dict | None] = mapped_column(JSONB, default=list)
    error_message: Mapped[str | None] = mapped_column(Text)
    reply_content: Mapped[str | None] = mapped_column(Text)
    display_content: Mapped[str | None] = mapped_column(Text)
    trace_source: Mapped[str | None] = mapped_column(String(32))
    create_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    prompt_cache_hit_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    first_token_ms: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)


class EvalRagBenchmark(Base):
    """知识库级 RAG 评估的**考题库**（对照 Java `EvalRagBenchmark`）。

    注意和全局评测三件套（`/api/eval/*`）完全是两套体系：
    这套绑定具体知识库，测的是「这个库的检索+回答链路质量」。

    `status` 生命周期：generating（AI 生成中 / 异步导入中）→ ready / failed。
    """

    __tablename__ = "eval_rag_benchmark"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    knowledge_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(String(512))
    question_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="ready")
    create_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    update_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    deleted: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)


class EvalRagBenchmarkItem(Base):
    """单道考题：query + gold_chunk_ids（JSON 字符串数组）+ gold_answer。

    `gold_chunk_ids` 在库里是 **TEXT 存的 JSON 数组字符串**（如 `["1","2"]`），
    不是 jsonb —— 因为 Java 实体就是 String，Python 照抄存储形态，
    读的时候 json.loads 一次即可。
    """

    __tablename__ = "eval_rag_benchmark_item"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    benchmark_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    query: Mapped[str] = mapped_column(String(2000), nullable=False)
    gold_chunk_ids: Mapped[str | None] = mapped_column(String(2000))
    gold_answer: Mapped[str | None] = mapped_column(Text)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class EvalRagResult(Base):
    """一次评估运行的成绩单。

    `status` 用**大写** RUNNING / COMPLETED / FAILED（Java 侧就这口径，
    和全局评测的 completed 小写不是一个风格，前端按这个匹配，别统一）。
    `retrieval_json` / `answer_json` 是聚合指标的 JSON 文本；
    `overall_score` 只由检索指标均值构成，不含答案准确率（设计如此）。
    """

    __tablename__ = "eval_rag_result"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    knowledge_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    benchmark_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    benchmark_name: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="RUNNING")
    overall_score: Mapped[float | None] = mapped_column(Double)
    retrieval_json: Mapped[str | None] = mapped_column(Text)
    answer_json: Mapped[str | None] = mapped_column(Text)
    config_json: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(BigInteger)
    analysis: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    create_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    update_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    deleted: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)


class EvalRagResultDetail(Base):
    """单道考题的评估明细。

    `retrieval_scores` 是 `{"precision@1": .., "recall@1": .., ...}` 的 JSON 文本；
    `answer_score` 为 NULL 表示「没配评判模型 / 没生成答案」，
    0.0 才是「判了但不合格」—— 前端 errorOnly 过滤靠这个区分。
    """

    __tablename__ = "eval_rag_result_detail"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    result_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    query: Mapped[str] = mapped_column(String(2000), nullable=False)
    gold_chunk_ids: Mapped[str | None] = mapped_column(String(2000))
    gold_answer: Mapped[str | None] = mapped_column(Text)
    generated_answer: Mapped[str | None] = mapped_column(Text)
    retrieved_chunk_ids: Mapped[str | None] = mapped_column(String(2000))
    retrieval_scores: Mapped[str | None] = mapped_column(Text)
    answer_score: Mapped[float | None] = mapped_column(Double)
    answer_reasoning: Mapped[str | None] = mapped_column(Text)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class UserMemory(Base):
    """用户长期记忆（对照 Java `UserMemory`）。

    `embedding_vector` 列（vector(1024)）**故意不映射**：
    实体层带向量列会拖累每次 ORM 查询的序列化，Java 侧也是由 mapper
    原生 SQL 单独读写（`updateEmbeddingVector`）。Python 同样走原生 SQL。
    """

    __tablename__ = "user_memory"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    agent_id: Mapped[int | None] = mapped_column(BigInteger)
    session_id: Mapped[int | None] = mapped_column(BigInteger)
    memory_type: Mapped[str] = mapped_column(String(32), nullable=False, default="preference")
    content: Mapped[str] = mapped_column(Text, nullable=False)
    keywords: Mapped[list | None] = mapped_column(JSONB, default=list)
    source_message_id: Mapped[int | None] = mapped_column(BigInteger)
    confidence: Mapped[float] = mapped_column(Numeric(5, 4), nullable=False, default=1.0)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime)
    create_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    update_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    deleted: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)


class DocumentVersion(Base):
    """文档版本快照（在线编辑/回滚用）。

    内容本体不在库里，在 MinIO `doc-versions/{docId}/v{N}.txt`；
    这张表只记"第几版、哈希多少、存在哪、谁存的"。
    """

    __tablename__ = "document_version"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    document_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str | None] = mapped_column(String(64))
    storage_path: Mapped[str] = mapped_column(String(512), nullable=False)
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    create_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class GraphExtractionTask(Base):
    """抽取流水记录（对照 Java `GraphExtractionTask`），source=auto/import。"""

    __tablename__ = "graph_extraction_task"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    knowledge_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    document_id: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="auto")
    entity_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    relation_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[str | None] = mapped_column(Text)
    create_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    update_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    deleted: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
