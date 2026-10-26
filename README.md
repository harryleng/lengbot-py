# lengbot-py

lengbot 的 Python 学习版重写。**不是生产系统**，目标是练 Python 工程能力；lengbot 本身不动。

前端直接复用 `C:\home\lengbot\lengbot\lengbot-ui`，一行不改，靠接口契约对齐。

---

## 两条铁律

**1. `message` 表是唯一事实来源**
LangGraph 只当执行引擎（负责跑 Agent 循环、HITL 挂起恢复），聊天历史永远落这张表。
❌ 不要把 messages 塞进 LangGraph 的 checkpoint blob —— 那样前端分页、搜索、token 统计全部失效。
✅ 每个 node 结束时落库；checkpoint 只用于 `interrupt` / `resume`。

**2. 接口契约一个字段都不能变**
393 个接口、381 个前端调用点。路径、参数名（`pageNum` 不是 `page_num`）、
响应结构（`{code, message, data}`）、**bigint 主键序列化成字符串** —— 全是硬约束。
改一个，前端就等着报错。

---

## 目录

```
app/
  core/        config.py（≈ application.yml）  db.py（≈ DataSource）
  models/      SQLAlchemy ORM（≈ MyBatis 实体）
  schemas/     Pydantic（≈ DTO + Bean Validation）
  repositories/ 数据访问（≈ Mapper）
  services/    业务逻辑（≈ ServiceImpl）
  api/         FastAPI 路由（≈ Controller）
db/
  lengbot_seed.dump   远程库快照
  RESTORE.md          恢复步骤（先读这个）
scripts/
  export_api_todo.py  从 lengbot-ui 提取接口清单，生成学习看板
```

## 怎么跑

```bash
# 0. 先按 db/RESTORE.md 启动本地 PG + 恢复数据（这一步只能你做）
cp .env.example .env      # 然后改密码
uv sync
uv run uvicorn app.main:app --reload --port 18080
```

打开 http://127.0.0.1:18080/docs 看自动生成的 OpenAPI 文档。

> 端口统一用 **18080**（`start.bat` 默认值）。别用 8080 / 8083 ——
> 这两个落在 Windows 的系统保留段里（Hyper-V/winnat），会报 `WinError 10013`，
> 看起来像权限问题，实际是端口根本申请不到。查法：
> `netsh int ipv4 show excludedportrange protocol=tcp`

验证：

```bash
curl "http://127.0.0.1:18080/api/chat/sessions?pageNum=1&pageSize=5"
```

应该返回真实会话数据。

### 让前端连过来

**lengbot-py 有自己的前端副本，在 `ui/`（从主仓 `lengbot-ui` 复制），端口 5175。**

```bash
# 1. 起后端（18080）
start.bat

# 2. 起前端（5175）—— 双击 start-ui.bat，或：
cd ui && pnpm install && pnpm dev
```

页面：**http://localhost:5175**，登录用 `admin`，密码随便填
（见下方「已实现的接口」里关于开发态登录的说明）。

| 前端 | 端口 | 后端 | 用途 |
|---|---|---|---|
| `lengbot/lengbot-ui` | 5174 | Java 8082 | 主仓，**指向 Java 版，未做任何改动** |
| `lengbot-py/ui` | **5175** | **Python 18080** | Python 版专用，proxy 写死 18080 |

两份互不影响，可以同时开。`ui/vite.config.js` 相比主仓只改了三处：
`port 5175`、`proxy target → 18080`、proxy 里改写 Origin/Referer 头的端口号。
（其余配置一字未动，所以主仓升级前端时，把 `src/` 覆盖过来即可。）

副本里 `ui/node_modules` 用 `pnpm install` 装（走全局 store 硬链接，约 26 秒、不额外占盘）。
主仓用 pnpm，所以这里也统一 pnpm，别混 npm 生成 `package-lock.json`。

---

## 阶段路线

| 阶段 | 内容 | 状态 |
|---|---|---|
| 0 | 地基 + 会话列表/详情/消息三个读接口 | ✅ 已完成并验证 |
| 1 | SSE 流式聊天闭环（真实模型 deepseek-flash） | ✅ 含 stop / reconnect / 标题生成 |
| 2 | 工具调用（手写循环） | ✅ 已通：5 个文件工具 + 工具循环 + SSE 事件 |
| 2.5 | 用 LangGraph 重写一遍做对比 | ✅ 两版都跑通，结论见 `docs/手写循环vsLangGraph.md` |
| 2.6 | LangGraph checkpointer + interrupt（HITL） | ✅ 挂起/批准/拒绝三场景实测通过 |
| 3 | 知识库 RAG | ✅ 检索链路打通（自命中测试 3/3），发现 embedding 元数据错误 |
| 4 | 工作流引擎 | ✅ DAG 执行器 + 9 种节点 + **CONFIRM 挂起恢复**（三校验通过） |
| 4.5 | workflow 型 agent 接入 chat 流式链路 | ✅ 已通（6 节点实际跑完并落库） |
| 5 | 补齐剩余 builtin 工具（5 类） | ✅ execute_code / web_search / ocr_parse_file / 二进制文件读写，端到端实测通过 |
| 6 | 工具调用持久化（tool_events + tool_calls + 详情接口） | ✅ 三处落库端到端验证通过，与 Java 历史数据兼容 |
| 7 | 工作流外部节点（api / script / tool）+ 未知节点必报错 | ✅ 真实工作流跑通 api→script→condition，mcp 明确报错 |
| 8 | 模型供应商管理（15 接口） | ✅ 覆盖 0/15 → 15/15，含真连通性检查 |
| 9 | 配置面批量补齐：工具 9 + MCP 10 + 系统配置 11 | ✅ 30 个接口，覆盖率 5% → **18%（66/371）** |
| 10 | Agent 管理（27 接口） | ✅ CRUD + 5 组绑定 + 克隆/默认 + 版本，覆盖率 → **25%（91/371）**；顺带修了全局 422 静默问题 |
| 11 | 工作流编排（16 接口，含 2 个 SSE） | ✅ 草稿/发布/校验/版本/调试/记录，覆盖率 → **28%（105/371）**；修了"跑发布版"取图 bug |
| 12 | Prompt 管理 13 + SubAgent 16 | ✅ 版本四步规则 + 双命名风格（驼峰/snake_case 并存），覆盖率 → **36%（134/371）** |
| 13 | 消息反馈 5 + 会话扩展 6 + RAG 引用 | ✅ toggle 三态 + 收藏/归档/搜索/附件，覆盖率 → **40%（148/371）** |
| 14 | 评测体系：评测集 14 + 评估器 12 + 实验 9 | ✅ **含真实执行引擎**（生成→多评估器打分→落库），覆盖率 → **49%（183/371）** |
| 14+ | 记忆、trace、剩余接口 | 长期 |

## 工作流（阶段 4）

```
app/workflow/
  graph.py           图解析 + next_node(node_id, handle)
  state.py           ExecState + {{var}} 渲染
  nodes.py           10 种内置节点 + 未知节点必报错
  nodes_external.py  api / script / tool（要碰外部世界的三种）
  script_runner.py   子进程跑脚本（main(params) 约定）
  executor.py        执行循环 + 事件流 + 步数上限
```

**节点覆盖度**：Java 有 23 种，Python 目前 13 种。

| 已实现 | 说明 |
|---|---|
| `start` `end` `input` `output` | 骨架 |
| `variable` `variable_handle` | 变量赋值 / 拼接 |
| `llm` `retrieval` `condition` | 核心三件 |
| `confirm` | HITL 挂起 |
| `api` | HTTP 调用（带 SSRF 防护） |
| `script` | python / javascript / java 沙盒 |
| `tool` | 复用 chat 链路的工具注册表 |

| 未实现（遇到会**明确报错中止**） | 阻塞原因 |
|---|---|
| `mcp` | 要 MCP 客户端 + server；库里两个 mcp_server 都是 disabled |
| `loop` / `loop_start` / `loop_end` | 子图执行，需要迭代上下文 |
| `batch` / `batch_start` / `batch_end` | 同上（并行版） |
| `classifier` | 意图分类分支 |
| `parameter_extractor` | LLM 抽参 |
| `app_component` | 应用组件嵌套 |

**三个反直觉的地方（读真实库才知道）：**

1. **图在 `agent_version.config.graph`**，不在 `agent.config`。
   workflow 型 Agent 的 `config` 是空 `{}`。

2. **模型配置在 LLM 节点自己身上**：
   `{"providerId": "...", "modelId": "deepseek-flash", "sysPrompt": "...", "temperature": 0.7}`
   —— 和 chat 型 Agent 完全相反。

3. **变量名差一个字就静默失败**：
   `retrieval` 必须输出 `retrievalResult`（下游 `variable_handle` 的
   `templateContent` 写的是 `{{retrievalResult}}`）；
   `variable_handle` 渲染结果写入 `output`（LLM 节点的 promptTemplate 是
   `用户问题：{{query}}\n\n{{output}}`）。
   写错成 `retrievalContext` 不会报错，只会让模型说"知识库中没有相关内容"。

**分支靠 `sourceHandle`**：普通节点走 `out`，condition 走 `out_a`/`out_b`。
只看节点不看 handle，条件分支会被走成直线。

**未实现的节点跳过而不报错**：画了含 SCRIPT/API/MCP 的流程，不该因为
Python 版没实现就整个跑不起来。日志警告 + 原样通过。

自检：`uv run python -m scripts.workflow_check`

### workflow 型 agent 走 chat 链路

`POST /api/chat/stream` 里按 `agent_type` 分流（对照 Java `WorkflowMiddleware`）：
`workflow` → 工作流引擎；其他 → LLM 工具循环。

**事件名要映射**（前端只认特定名字）：

| 内部 | 前端 | 差异 |
|---|---|---|
| `workflow_node_end` | `workflow_node_complete` | 补 `success` 字段 |
| `workflow_done` | `workflow_complete` | — |

LLM 节点目前是"收集完一次性返回"，没有 token 级流式（`workflow_llm_chunk` 未做）。

### 已实现 / 未实现节点

| 已实现 | 未实现 |
|---|---|
| start / end / input / output | api / script / mcp |
| llm / retrieval / variable / variable_handle | loop / batch / classifier / parameter_extractor |
| condition（含 12 种运算符） | |
| **confirm（HITL 挂起恢复）** | |

## HITL（人工确认）

执行到 `confirm` 节点时挂起，等人填表再继续。

```
POST /api/agents/{agentId}/workflow/resume
{"runId": "...", "formData": {"approved": "通过", "remark": "..."}, "messageId": "..."}
```

挂起时会推两个事件：

| 事件 | 用途 |
|---|---|
| `workflow_confirm_required` | 带 `runId` + `confirmForm{message, formFields}`，前端渲表单 |
| `workflow_suspended` | 前端收尾 UI（loading/streaming 置 false） |

**三个关键设计：**

1. **快照里存整张图**（`graph_raw`），恢复时不用重新查库——
   等待确认期间用户可能改了工作流，用新图续跑会变成"前半段旧逻辑、后半段新逻辑"
2. **快照一次性消费**（`pop` 不是 `get`），防止双击"确认"把后续节点跑两遍
3. **表单值直接进变量池**，下游节点写 `{{approved}}` / `{{remark}}` 就能取到

自检：`uv run python -m scripts.hitl_check`

**和 LangGraph `interrupt` 的差别**：这里只存"挂起那一刻"，只能前向续跑；
LangGraph 的 checkpointer 是每个 super-step 持久化，能回放到任意历史步骤。

## 工具循环（阶段 2 核心）

一轮完整交互：

```
问模型 ──▶ 模型说"我要调 local_read_file" ──▶ 执行 ──▶ 结果作为 role=tool 回灌
   ▲                                                        │
   └────────────────────────────────────────────────────────┘
              直到模型不再要工具（或达到 maxExecutionSteps）
```

三个协议细节，缺一不可：

1. **必须把 assistant 的 `tool_calls` 消息原样回灌** —— 否则下一轮模型不知道"我请求过什么"，会重复调同一个工具
2. 每个工具结果作为 `role=tool` 消息，带 `tool_call_id` 对应回去
3. **达到步数上限后不再传 `tools`** —— 模型只能输出文本，天然避免死循环

**流式 tool_calls 是分片的**（最易踩）：

```
chunk1: {"tool_calls":[{"index":0,"id":"call_x","function":{"name":"calculator","arguments":"{\"a\":"}}]}
chunk2: {"tool_calls":[{"index":0,"function":{"arguments":"10,\"b\":5"}}]}
```

必须按 `index` 累积拼接 `arguments`，否则拿到残缺 JSON。见 `llm_client.py`。

**工具失败不抛异常**：返回 `{"ok": false, "error": "..."}` 让模型自己纠正。
抛异常会中断整个对话，返回错误则模型重试一次就能补上——Agent 的健壮性主要来自这个选择。

**两级截断**：进上下文 2000 字符（省 token），进 SSE 事件 4000 字符（前端要展示细节）。

## 工具层（阶段 2 前半，已完成）

```
app/tools/
  base.py        ToolSpec + @tool 装饰器：类型注解 → JSON Schema
  builtin.py     calculator / get_current_time / text_stats
  builtin_fs.py  local_{list_dir,read_file,write_file,append_file,delete_file}
                 + local_{read,write}_file_binary（base64）
  builtin_exec.py  execute_code（python / javascript / java）
  builtin_web.py   web_search（Tavily）
  builtin_ocr.py   ocr_parse_file（图片 / PDF）
  builtin_kb.py  query_knowledge / list_knowledge_bases / ...
  registry.py    注册表 / resolve_for_agent / verify_against_db
```

**一个函数就是全部定义**：

```python
@tool("calculator", "执行基本数学运算")
async def calculator(
    a: Annotated[float, field("第一个操作数")],
    b: Annotated[float, field("第二个操作数")],
    operation: Annotated[Literal["add","subtract","multiply","divide"], field("运算类型")],
) -> str: ...
```

参数名 / 类型 / 必填 / enum / 描述全部从签名推出来，不存在"改了实现忘改 schema"。
Java 侧则要同时维护 `@ToolParam` 注解和数据库里的 `input_schema` 两份。

**必须 async**：同步函数会阻塞 asyncio 事件循环（在 Spring 里阻塞一个线程无所谓，
在 asyncio 里一个阻塞调用会卡住所有并发请求）。装饰器会强制检查。

**schema 漂移检测**：`registry.verify_against_db()` 会在自检时比对
数据库 `input_schema` 和代码生成的 schema。首次运行就抓到 `filePath` vs `path` 不一致——
模型严格按库里的 schema 传参，参数名对不上就是 TypeError，而且报错信息完全看不出根因。

**文件工具的安全底线**：所有路径经 `_safe_path()` 处理（`resolve()` + `is_relative_to`），
`../../windows/win.ini`、`/etc/passwd`、`C:/Windows/win.ini` 等 4 种穿透实测全部拒绝。
根目录由 `settings.workspace_root` 控制（默认 `workspace/`）。

**一键回归**（改完代码跑这个，约 40s）：

```bash
uv run python -m scripts.verify_all           # 不花钱的 6 项
uv run python -m scripts.verify_all --llm     # 再加端到端对话（真调模型）
```

单个跑：

```bash
uv run python -m scripts.smoke_test         # ORM → schema → JSON 三层对齐
uv run python -m scripts.tool_events_check  # 工具事件截断/瘦身规则（离线，不需要库）
uv run python -m scripts.tool_schema_check  # ToolCall 实体 vs DDL 列一致性（离线）
uv run python -m scripts.tools_check        # 基础工具：schema 校验 + 执行 + 路径穿越防护
uv run python -m scripts.tools_ext_check    # 扩展工具：execute_code / web_search / ocr（真跑）
uv run python -m scripts.rag_check          # RAG 检索链路 + 未向量化诊断
uv run python -m scripts.model_provider_check   # 模型供应商 15 接口（--live 才碰外网）
uv run python -m scripts.tools_api_check        # 工具管理 9 接口
uv run python -m scripts.mcp_api_check          # MCP 服务器 10 接口
uv run python -m scripts.system_config_check    # 系统配置 11 接口（会备份恢复）
uv run python -m scripts.agents_api_check       # Agent 管理 27 接口（会清理测试数据）
uv run python -m scripts.workflow_editor_check  # 工作流编排 16 接口（校验规则 + SSE 调试）
uv run python -m scripts.prompt_subagent_check  # Prompt 13 + SubAgent 16 接口（版本四步 + 双命名）
uv run python -m scripts.chat_ext_check         # 反馈 5 + 会话扩展 6 + RAG 引用（toggle 三态）
uv run python -m scripts.eval_check             # 评测 35 接口（--live 才跑真实实验，花钱）
uv run python -m scripts.workflow_check     # 工作流 DAG 执行
uv run python -m scripts.nodes_ext_check    # 工作流 api/script/tool + 未知节点必报错
uv run python -m scripts.hitl_check         # 工作流 CONFIRM 挂起 / 恢复
uv run python -m scripts.tool_persist_check # 工具调用三处落库（真跑一轮对话）
uv run python -m scripts.e2e_chat_check "问题"   # 端到端对话（真调模型）
uv run python -m scripts.llm_probe          # 逐个供应商实测（排查 401 / 404 / 429）
```

### 阶段 5 补的 5 个工具

| 工具 | 说明 | 依赖 |
|---|---|---|
| `execute_code` | 临时目录里跑 python / javascript / java，超时 5s 强杀；**脚本产生的文件自动收进 `artifacts` 并落到 `workspace/outputs/`** | node / javac（可选，缺则明确报错） |
| `web_search` | Tavily，返回 `answer`（模型归纳）+ `results[]` | `TAVILY_API_KEY`，没配就返回 `success:false` 而不是崩 |
| `ocr_parse_file` | 图片走 RapidOCR；**PDF 先抽文本层，抽得到就不 OCR**；结果写 `ocr/<name>.md` | pymupdf + rapidocr-onnxruntime |
| `local_read_file_binary` | 读二进制 → base64 | - |
| `local_write_file_binary` | base64 → 写文件 | - |

三个踩过的坑：

1. **产物快照必须在写脚本之后拍**。先拍后写会把上一次运行的文件当成这次的产物。
2. **Java 的"缺 return"报错要认中英文两种**（`缺少返回语句` / `missing return statement`），
   否则"只打印不返回"的代码会被误判成编译失败而反复重试。
3. **pymupdf 要 `import pymupdf`**，旧的 `import fitz` 已被废弃。

### 端到端验证

429 限流解除后实测（agent「未来已来」，绑了这 10 个工具）：

```
python -m scripts.e2e_chat_check "请用 execute_code 计算 1 到 100 的和"
  >> 调用 execute_code  → {"sum": 5050, "formula_check": 5050}   ✅

python -m scripts.e2e_chat_check "搜一下 Python 3.14 正式版的发布时间"
  >> 调用 web_search    → 2025-10-07                              ✅

python -m scripts.e2e_chat_check "识别 ocr_src.png 上写了什么"
  >> 调用 ocr_parse_file → "OCR 测试: lengbot 2026"                ✅
  >> 自动接 local_read_file 读回 ocr/ocr_src.md 确认               ✅
```

模型会**自己串工具**（OCR 完主动读结果文件核对），说明工具描述写得够清楚。

### 供应商 URL 拼接（实测修正）

`_chat_completions_url()` 的判据不是"带不带 v1"，而是**路径末尾有没有 `/v<数字>`**：

| base_url | 正确端点 | 说明 |
|---|---|---|
| `https://api.deepseek.com` | + `/v1/chat/completions` | 路径为空，补 v1 |
| `https://api.siliconflow.cn/v1` | + `/chat/completions` | 已带 v1 |
| `https://api.lkeap.cloud.tencent.com/plan/v3` | + `/chat/completions` | **已带 v3** |

腾讯云原先被拼成 `/plan/v3/v1/chat/completions` → 404。而 `/v1` 虽然存在但返回
`401 not authorized`（key 只在 `/plan/v3` 下授权），所以拼错不只是拿不到结果，
是**根本调不通**。可用 `python -m scripts.llm_probe` 逐个供应商实测。

## 已实现的接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/chat/sessions` | 分页列表 |
| POST | `/api/chat/sessions?agentId=` | 创建会话（前端「新对话」先调这个） |
| GET | `/api/chat/sessions/{id}` | 详情 |
| GET | `/api/chat/sessions/{id}/title` | 标题（首轮问答后自动生成） |
| GET | `/api/chat/sessions/{id}/messages` | 消息历史 |
| PUT | `/api/chat/sessions/{id}/title?title=` | 重命名 |
| PUT | `/api/chat/sessions/{id}/pin` | 置顶 / 取消（取反） |
| DELETE | `/api/chat/sessions/{id}` | 删除会话（软删 `deleted=1`） |
| DELETE | `/api/chat/sessions/{id}/messages/{mid}` | 删消息（物理删 + 原子重算统计） |
| GET | `/api/chat/sessions/{id}/export?format=` | 导出 md / json（**裸文本，不包 Result**） |
| GET | `/api/chat/sessions/tool-calls/{toolCallId}/result` | 按 toolCallId 拉工具完整输出（**历史列表瘦身后回查**） |
| GET/POST/PUT/DELETE | `/api/model-providers/...` | 模型供应商管理 **15 个接口全实现**（见下节） |
| GET/POST/PUT/DELETE | `/api/tools/...` | 工具管理 **9 个接口全实现**（含 builtin/knowledge 只读保护） |
| GET/POST/PUT/DELETE | `/api/mcp-servers/...` | MCP 服务器 **10 个（7 实现 + 3 明确报错）** |
| GET/PUT | `/api/system-config/default-*` | 默认模型配置 **11 个接口**（providerId 用字符串） |
| GET/POST/DELETE | `/api/agents/...` | Agent 管理 **27 个接口**（CRUD + 5 组绑定 + 克隆/默认 + 版本） |
| GET/POST/DELETE | `/api/agents/{id}/workflow/...` | 工作流编排 **16 个接口**（含 `test/stream`、`resume/stream` 两个 SSE） |
| GET/POST/PUT/DELETE | `/api/prompts/...` | Prompt 管理 **13 个**（主表 + 版本 + 构建模板） |
| GET/POST/PUT/DELETE | `/api/subagents/...` | SubAgent **16 个**（CRUD 7 + 运行记录 9） |
| GET/POST | `/api/chat/messages/{id}/feedback`、`/api/chat/feedbacks*` | 消息反馈 **5 个**（👍/👎 toggle） |
| PUT/DELETE/GET | `/api/chat/sessions/{search,batch,messages/starred,*/archive,*/attachments}` | 会话扩展 **6 个** |
| GET | `/api/chat/rag-references` | 按问题重新检索知识库引用 |
| GET/POST/PUT/DELETE | `/api/eval/datasets/...` | 评测集 **14 个**（主表 + 样本 + 版本 + 示例） |
| GET/POST/PUT/DELETE | `/api/eval/evaluators/...` | 评估器 **12 个**（含 `/test` 真调模型） |
| GET/POST/PUT/DELETE | `/api/eval/experiments/...` | 评测实验 **9 个**（含异步执行 + 汇总/明细） |
| GET | `/api/system-config/health` | 健康检查（前端登录页 / 落地页挂载即调） |
| POST | `/api/chat/stream` | **SSE 流式对话** |
| POST | `/api/chat/stream/stop?requestId=` | 停止生成（置位取消信号） |
| POST | `/api/chat/stream/reconnect` | 断线重连，body `{requestId, lastEventId}` |
| POST | `/api/auth/login` | **开发态登录：不校验密码**（见下） |
| GET | `/api/auth/me` | 当前用户 |
| POST | `/api/auth/logout` | 登出（无服务端会话，前端删 localStorage 即可） |
| GET | `/api/agents` | Agent 列表（聊天页侧栏 / 选择器） |
| GET | `/api/agents/{id}/detail` | 详情，外层包一层 `{"agent": {...}}` |
| GET | `/api/agents/{id}/versions` | 版本列表（暂返回空，前端会降级成只有「暂存草稿」） |

**登录是替身，不是认证。** `users.password` 存的是 BCrypt 哈希，本仓库没装 bcrypt，
所以 `POST /api/auth/login` **只校验用户名是否存在 + status=active，密码随便填**，
发出去的 token 是占位串 `dev-<userId>`，后端根本不解析它
（`get_current_user_id()` 固定返回 `settings.dev_user_id`）。

写这个假接口的唯一原因：前端 `stores/user.js` 登录后要读 `res.data.token` 塞
localStorage，`router/index.js` 的守卫靠「有没有 token」决定放不放行 ——
没有它连聊天页都进不去。真认证要改的只有 `app/api/deps.py` 一个函数。

**前端还要多少接口？** 跑覆盖率扫描就知道：

```bash
uv run python -m scripts.api_coverage         # 前端在用、后端没有的清单
uv run python -m scripts.api_coverage --full  # 连已实现的一起看
```

它会扫 `lengbot-ui/src/api/*.js` 里所有 `request.xxx('/path')`，和 FastAPI
实际注册的路由做对比。当前覆盖 **183/371（49%）** —— 剩下的大头是
`knowledge`(70)、`knowledge`(70) + `skills`(20) + `graph`(14) + `tts`(4) +
`user memories`(7) + `api-keys`/`logs`/`dashboard` 若干。
`knowledge` 那 70 个涉及文档解析 / 分块 / 向量化 / 预览，
不是"补接口"能解决的，是重写一整个 Java 模块；
`skills` 的 20 个里 13 个依赖 MinIO + zip + GitHub 远程仓库。

## 评测体系（阶段 14）—— 35 个接口 + 真实执行引擎

覆盖率 **148/371 → 183/371（49%）**。

| 模块 | 接口 |
|---|---|
| 评测集 | 主表 CRUD 5 + 样本 4 + 版本 3 + 示例 2 |
| 评估器 | 主表 CRUD 5 + 版本 2 + 模板 2 + 示例 2 + `/test` |
| 实验 | CRUD 5 + stop / restart / results / detail |

### 执行链路（这是本轮唯一"真干活"的部分）

```
数据集版本.dataset_items（id 列表）
  → 逐条取 data_content（如 {"input": "...", "reference_output": "..."}）
  → 按 evaluationObjectConfig.config.variableMap 映射成 prompt 变量
      {"input": "什么是 RAG"} → {"question": "什么是 RAG"}
  → 调被测 Prompt → actual_output
  → 对每个 evaluatorConfig[i] 组装评估器变量（含 actual_output）→ 打分
  → 落 eval_experiment_result（每个 样本×评估器 一行）
```

实测：5 条样本 × 1 个评估器，10 次 LLM 调用，约 25 秒跑完，`progress` 从 0 → 100。

### 分数必须归一化，否则平均分是假的

模型对"给个 0-1 分"的理解不一致，实测会返回 `0.85` / `85` / `8.5` / `"0.7分"`。
Java 只用 `BigDecimal` 接，所以 `85` 会被原样存成 85 —— 在 0~1 的量纲里
一条 85 分就能把平均分从 0.9 拉到 40+，而且**不报错**。

这里做了归一：`1<x<=10` 除以 10、`10<x<=100` 除以 100、边界夹到 `[0,1]`。
无法解析的返回 0 分但**保留原文到 reason** —— 用户能看到模型到底吐了什么。

### 解析必须能从 markdown 围栏里抠 JSON

模型很爱返回 ```` ```json {...} ``` ````。直接 `json.loads` 会炸，
所以先去围栏、再取第一个 `{` 到最后一个 `}`。

**解析失败不抛异常**：20 条样本跑一半、中间一条解析失败就整批崩掉，
用户白等。兜底成 `score=0, reason="评分结果解析失败: <原文>"`（Java 同）。

### 评估 prompt 要追加固定的输出指令

评估器的 prompt 只写"请评估相似度…"，没告诉模型怎么**输出**。
Java 在末尾硬拼一段 `按照Json格式返回…只返回Json字符串`。
少了这段，模型会返回"我认为大概是 0.8 分，因为…"，抓取成功率不稳定。

但**被测对象不能追加** —— 那会让被测模型也变成评估器，拿到的就不是
"用户看到的回答"了。

### 版本冻结的是 id 列表，不是内容

`eval_dataset_version.dataset_items` 存 `[id1, id2, ...]`。
所以条目**内容**改了，历史版本读出来也是新内容。
Java 就这么设计的，但这一点在复现历史实验时会有影响，值得记住。

### 三处和 Java 的刻意差异

| 差异 | 原因 |
|---|---|
| 不建 `task` 排队，只写一条状态记录 | Java 有独立 worker 扫表执行、支持重启恢复；Python 用 `asyncio.create_task`，**重启即丢**。所以 task 表只用来存 `cancel_requested` 和错误信息 |
| 只支持 `type=prompt` 的评测对象 | Java 还支持 `type=agent`。做那个要复用 `ChatStreamService`，跨模块耦合大；遇到其它 type **明确 400**，不静默跳过（跳过会产出 0 条结果，用户以为跑完了） |
| 并发度 3 | 20 条 × 2 评估器 = 60 次调用，全并发会撞 429（踩过）。串行又太慢 |

### `stop` 只发信号，不改状态

执行器每完成一批会检查 `cancel_requested`，自己退出并置 `stopped`。
接口里直接改 status 的话，执行器下一轮写 `progress` 又会改回 `running`。

### 顺手修的一个分页 bug

`list()` 返回的 total 一开始用了 `_enrich_many` 的第二个返回值（本页条数），
导致前端 `total == pageSize`，**永远翻不到第二页**。
这类 bug 在"只测第一页"的验证里完全看不出来。

### 路由顺序（第 6、7 次）

`/datasets/examples`、`/versions`、`/items`、`/evaluators/templates`、`/test`
全部必须排在 `/{id}` 之前。验证脚本专门断言"不 422"。

## 消息反馈 + 会话扩展 + RAG 引用（阶段 13）—— 12 个接口

覆盖率 **134/371 → 148/371（40%）**。

| 模块 | 接口 |
|---|---|
| 消息反馈 | `POST/GET /messages/{id}/feedback`、`POST /messages/feedbacks/batch`、`GET /feedbacks`、`/feedbacks/stats` |
| 会话扩展 | `GET /sessions/search`、`DELETE /sessions/batch`、`GET /sessions/messages/starred`、`PUT /sessions/messages/{id}/star`、`PUT /sessions/{id}/archive`、`GET/DELETE /sessions/{id}/attachments` |
| RAG 引用 | `GET /rag-references?sessionId=&question=&agentId=` |

### 反馈的核心是 toggle 三态，不是 upsert

| 已有 | 新提交 | 结果 |
|---|---|---|
| 无 | like | 新建 |
| like | like | **删除**（返回 `data: null`） |
| like | dislike | 切换 |

只实现"新增或覆盖"的话，用户**永远取消不掉自己点错的赞**。
`data: null` 是前端的判据（`if (res.data)` 走选中分支），
别改成返回 `{rating: null}`。

`agentId` / `agentVersion` 在**提交时快照**（message → session 反查），
不是实时关联 —— Agent 后面改名或删掉，统计报表仍要能回答"这条差评是哪个版本给的"。

### `batch` 返回对象不是数组

```json
{"372562615617982464": {"rating": "like", ...}, "372562615617982465": {...}}
```

前端写法是 `feedbacks[String(msg.id)]`。返回数组的话取不到，
**整个消息列表的点赞状态全部显示成"未点"**，而且不报错。

### 归档 ≠ 删除

| 操作 | 效果 | 能搜到 | 列表可见 |
|---|---|---|---|
| 归档 | `status='archived'`，`deleted=0` | ✅ | ❌ |
| 删除 | `deleted=1` | ❌ | ❌ |
| 批量删除 | **物理删**（会话 + 消息 + 工具记录） | ❌ | ❌ |

单条删除是软删（"从列表移走"），批量删除是物理删（"我不要了"）——
Java 侧就是这样，两种语义确实不同。

### 搜索 snippet 要关键字居中

`content[:200]` 是错的：用户搜的词可能在正文第 3000 字，
截前面 200 字的话结果里完全看不到关键词，看起来像"搜错了"。
现在是**关键字前留 60 字**再截到 200，前后加 `…`。

三个归一化动作不能省：trim 后为空直接返回（避免 `ILIKE '%%'` 全表扫）、
关键词截 64 字、`limit` 夹到 1~50。

### 附件不走 MinIO

附件存在 `chat_session.attachments` 这个 **jsonb 数组**里，没有独立表 ——
所以这批接口能完整工作（不像 skills 的文件接口依赖对象存储）。

- 解析兼容两种形态：`[...]` 和 `{"items": [...]}`
- 单条脏数据跳过，不让整个列表 500
- 删除只从数组摘掉，**不删存储对象**（同一 objectKey 可能被别的消息引用）

### 顺带修了 Java 的一个越权

Java `archiveSession` 只 `getById` 就改状态，**没校验归属** ——
任何人传别人的 sessionId 就能把对方会话归档掉。Python 版补了 `_ensure_owned`。

另外 5 处越权也都测过了（归档 / 搜消息 / 看附件 / 收藏消息 / 给消息点赞别人的会话），
全部返回 404（不是 403 —— 403 会泄露"这个 id 确实存在"）。

### `rag-references` 失败一律空数组

Agent 没绑库 / 库没向量化 / 向量模型不通 → 都返回 `[]`，不抛错。
用户点"引用"是想看有没有额外资料，不是想处理异常弹窗。

Python 版只支持 `sourceType=chunk`（库里没有 qa_pair 数据），`qaPairId` 恒为 null。

## Prompt 管理 + SubAgent（阶段 12）—— 29 个接口，两种命名风格并存

覆盖率 **105/371 → 134/371（36%）**。

| 模块 | 接口 |
|---|---|
| Prompt 主表 | 列表 / 创建 / 详情 / 更新（**id 在 query**）/ 删除（级联版本） |
| Prompt 版本 | `POST /versions` / `GET /versions/detail` / `GET /{key}/versions` |
| 构建模板 | 列表 / 创建 / 更新（id 在 query）/ 按 key 取 / 删除 |
| SubAgent | CRUD 5 个 + `/enabled` + `/{id}/enabled` |
| SubAgent 运行记录 | `/runs`、`/runs/summary`、`/runs/projection`、`/runs/{id}`、`/runs/{id}/thread`、`/runs/{id}/events`、两个 cancel、`/batches/{id}` |

### 同一个模块里两套命名风格

这是 Java 侧的现状，**必须照抄，不能统一**：

| 接口 | 风格 | 原因 |
|---|---|---|
| `GET /api/subagents/runs` | **驼峰** `requestId` | 返回 `Page<SubAgentRun>` 实体分页 |
| `GET /api/subagents/runs/{taskId}` | **snake_case** `task_id` | Java 手拼 `Map.of("task_id", ...)` |

`Map` 不经过 Jackson 的命名策略，所以原样输出。统一成一种，前端对应的那块就全空。

### Prompt 版本的四步规则（顺序不能变）

1. `promptKey` 必须已存在
2. 要发 `release` 且同 key+version 已有 release → **拒绝**（否则同版本号两个"正式版"，评测取到哪个是随机的）
3. 创建前**先软删同 key+version 的 `pre`**（预发布允许覆盖重来）
4. 建完更新 `prompt.latest_version`

第 3 步少了，用户改一次预发布版本就会看到两条同版本号的记录，不知道哪条生效。

### 踩到的坑：Pydantic 的 `model_config` 是保留名

`modelConfig` 这个字段名在 Pydantic v2 里**不能直接用作字段名**（`model_config` 是模型配置属性）。
第一版直接写 `model_config: str` 就抛 `PydanticUserError`。

改成 `model_config_json` + 显式 `alias="modelConfig"` 后又踩了第二个坑：
**`from_attributes` 时 Pydantic 按 alias 去 ORM 对象上找属性**，而 ORM 属性名是
`model_config` → 找不到 → **静默用默认值 `"{}"`**（不报错、不告警）。

正解是 `validation_alias=AliasChoices("modelConfig", "model_config")`，两个都认。
这个问题是验证脚本断言"modelConfig 里应该有 temperature"才抓到的。

### SubAgent 的三条业务规则

1. **内置的只能改模型配置**（名称/提示词由代码注册器同步，改了会被覆盖）
2. 内置**不可删除**，但**可以启停**（Java 也没拦）
3. `toolIds` 提交时要**剔除已删除的工具**，否则前端一直提示"有悬空引用"

还有一处不对称：`modelId` 为 null 表示"继承主 Agent"，必须**清空** `model_id` 和
`llm_model` 两列。按"null 不改"处理，用户永远切不回继承模式。

### 运行记录：只读 + 诚实的 `available=false`

Python 版**没有子代理执行器**（`delegate` 工具没实现），这批接口读的是 Java 留下的历史数据。

- `GET /runs/{id}/thread` 返回 `available: false` + `messages: []`
  —— 那些线程不存在，返回假消息比空数组难查得多
- `cancel` 只写 `cancel_requested=1` 标记并**把 `affected` 原样返回**
  —— Python 侧没有消费者，任务不会真停；写"已取消"但任务还在跑是最坏的结果
- `/runs/projection` 的 `todos` / `attachments` / `artifacts` / `usage` **恒为空**
  （由 Java 的研究协作模块维护），但 `subagents` 是真实数据

## 工作流编排（阶段 11）—— 16 个接口，含 2 个 SSE

覆盖率 **91/371 → 105/371（28%）**。

| 分组 | 接口 |
|---|---|
| 编辑 | `GET`（编辑器状态）/ `POST /draft`（草稿）/ `POST /publish` / `POST /validate` / `GET /io-schema` |
| 版本 | `GET /versions` / `GET /versions/{v}` / `POST /versions/{v}/restore` |
| 调试 | `POST /test`、**`POST /test/stream`**、`POST /test-node` |
| 记录 | `GET/DELETE /test-runs`、`GET/DELETE /test-runs/{runId}` |
| 确认 | `POST /resume`、**`POST /resume/stream`**、`POST /abandon` |

### 三件必须照抄的事

**1. 帧必须是 `data:` 前缀 + 空行**

前端 `processSseLines` 是 `text.split('\n')` 然后只认 `data:` 开头的行。
第一版直接 yield `[STATUS]{json}`（没有前缀、没有换行），结果**一个事件都收不到**
—— 页面不报错，只是点了"运行"什么都没发生，节点也不亮。

**2. 草稿绝对不校验**

前端每 2.5 秒自动保存一次。用户改到一半（LLM 节点还没选模型）时图必然"不合格"，
一旦校验就永远存不上，编辑全丢。校验只在 `publish` 里做。

**3. `validate` 通过时返回 `[]` 而不是 null**

前端 `const errors = res.data; errors.length` —— null 直接 TypeError。

### 校验规则：12 条，其中 3 条是踩过坑的

| 规则 | 不做的后果 |
|---|---|
| condition 必须有 `out_c` 默认边 | 所有条件都不成立时**卡在条件节点不动**，不报错不结束，用户看到一直转圈 |
| 非分支节点不允许多条出边 | 引擎只走第一条，第二条上的节点**永远不执行**，画布上却连着 |
| 环路检测（返回环路径） | 只能靠 `MAX_STEPS=50` 兜底，烧完 token 报"步数超限"，看不出是哪条边回指 |

环路错误会带路径：`工作流存在环路: var_1 → start_1 → var_1`。
只报"存在环"用户根本找不出来。

**合法但 Python 未实现的节点类型（`mcp` / `loop` / `batch` / `classifier` 等）
算错误，不算警告**。前端只有一个 `errors` 通道，混警告等于允许发布；
发布出去之后，用户聊天时才会在那个节点上撞到"尚未实现"，那时再回头查是哪一步要贵得多。

### 顺带修的 bug：聊天跑的一直是草稿，不是发布版

`chat_stream_service._run_workflow` 原来自己拼 SQL 取图：

```sql
WHERE agent_id=:aid AND node_count > 0 ORDER BY version DESC LIMIT 1
```

用 `node_count` 当"有没有图"的判据是错的 —— 那是缓存字段，库里 published 版本的
`node_count` 普遍是 0（历史数据没维护），于是**已发布版被筛掉、实际一直在跑草稿**。
用户在编排页改了没发布，聊天里就已经生效，还以为"发布没生效"。

现在统一走 `WorkflowEditorService.load_definition(use_draft=False)`：
**优先最新发布版，没有发布版才回退草稿**（对应 Java `loadWorkflowDefinition`）。

### 调试记录落 `workflow_test_run`（不是内存）

每个 agent 只保留最近 **50 条**，超出物理删最旧的。存的是**前端格式**事件
（`workflow_node_complete` / `workflow_complete`），所以"实时看到的"和
"事后翻历史"结构完全一样，前端一套渲染逻辑够用。

`runId` 与挂起快照同源 —— HITL 确认后续跑时，靠这个 id 把同一条记录标成 completed，
否则记录永远挂在 `running`，前端一直显示"运行中"。


## Agent 管理（阶段 10）—— 27 个接口 + 一个全局 bug

覆盖率 **66/371 → 91/371（25%）**。

| 分组 | 接口 |
|---|---|
| CRUD | 列表 / 创建 / 更新 / 详情 / 删除 |
| 生命周期 | 克隆 / 设为默认 / 版本列表 / 版本详情 / 版本恢复 / 版本删除 |
| 关联绑定（5 组 × GET/PUT） | knowledge / tools / mcp-servers / subagents / skills |
| 详情辅助 | tools/detail / mcp-servers/detail / chat-capabilities / mention-options |
| 示例 | workflow-examples 列表 / 从示例创建 |

未做（4 个）：`publish` / `avatar`（multipart）/ `generate-prompt` / `generate-questions`，
以及工作流编辑器的 14 个 `/workflow/*`。

### 顺手修了一个全局 bug：422 被前端当成"成功"

FastAPI 的 `RequestValidationError` 默认返回 **HTTP 422 + `{"detail": [...]}`**，
body 里**没有 `code` 字段**。而前端 axios 拦截器是：

```js
if (res.code && res.code !== 200) { reject }   // ← res.code 是 undefined，条件不成立
return res
```

结果是**参数校验失败被当成成功**，调用方拿到 `data === undefined`，然后在业务代码里
报 `Cannot read property 'xxx' of undefined`。排查时后端日志只有一行 422，
前端报的是业务错误，两边对不上。

现在加了 `RequestValidationError` 处理器，统一转成
`{code: 400, message: "name: String should have at most 50 characters"}`。
**这个改动影响所有接口**，所以回归跑了全套 14 项。

### 四条业务规则（都是踩过坑才有的）

**1. 列表必须过滤 `deleted == 0`**

原来的 `list_agents` 只过滤 `status != 'archived'`。但软删是置 `deleted=1`、
`status` 不动 —— **删掉的 Agent 会照样出现在列表里**，点进去详情 404。
根因是 `Agent` 实体当初没映射 `deleted` 列（SQLAlchemy 不映射的列既不报错也不返回）。

**2. 绑定 ID 存字符串，且有数量上限**

| 绑定 | 上限 | 为什么 |
|---|---|---|
| tools | 10 | 每轮对话要把 tools schema 发给模型，绑 50 个能吃几千 token |
| skills | 10 | 同上 |
| knowledges | 10 | 检索要遍历，太多会拖慢首 token |
| subagents | 5 | 每个子 Agent 都会占用上下文 |
| mcpServers | 5 | 同 subagents |

存字符串是因为雪花 ID 19 位超 JS 安全整数（同 model-provider 那个坑）。

**3. `update` 时 `config` 是合并不是替换**

前端表单只提交它管的那些键。整体替换会把 `knowledges`/`tools` 这些
由绑定接口维护的键抹掉 —— 用户改个 `topP`，绑定的 10 个工具全没了。
（Java 侧是整体 `set`，但它每次提交完整 config；Python 侧不假设这一点。）

**4. `set_default` 要先清掉其它 Agent 的 `is_default`**

不清会同时存在多个默认，前端"默认排序"和"新对话用哪个"就随机了。

**5. 删除级联版本 + 会话**

只删 Agent 不删会话，用户点进历史对话会看到"Agent 不存在"，比直接看不到更困惑。

### 测试数据必须清理

`scripts/agents_api_check.py` 会创建 Agent、绑定、克隆、软删，
跑完用 `DELETE FROM agent WHERE name LIKE 'zz-自检Agent%'` 物理清掉
（连 agent_version / chat_session / message 一起），否则跑十次库里就多一堆垃圾。

自检：`uv run python -m scripts.agents_api_check`（50+ 项，约 5 秒）

## 配置面批量补齐（阶段 9）—— 工具 / MCP / 系统配置

沿阶段 8 的节奏继续，一次补 30 个接口：**覆盖率 36/371 → 66/371（18%）**。

| 模块 | 接口 | 状态 |
|---|---|---|
| `tools` | 9 | ✅ 全实现（含 builtin/knowledge 只读保护） |
| `mcp-servers` | 10 | 7 实现 + **3 明确报错** |
| `system-config` | 11 | ✅ 全实现（default-chat/embedding/tts/rerank/ai/models） |

### 三条容易做错的业务规则（错了会静默丢修改）

**1. `tool` 表的两类工具不能随便改**

- `knowledge` 类型：**不可编辑、不可删除、不可禁用** —— 由知识库注册器生成，
  改了下一次同步就被覆盖
- `builtin` 类型：**只能改限流字段** —— 名称/描述/schema 由 `BuiltinToolRegistrar`
  从代码注解同步，用户改了同样会被覆盖

不做这两条的话，用户改完保存"成功了"，重启同步一跑又变回去，数据看着没坏、
修改却静默丢失 —— 这类问题最难查。验证脚本专门测了"改内置工具名"这个动作。

**2. JSONB 数组过滤要用 `.contains()`，不要手写 `op("@>") + cast`**

第一版写的是：

```python
Tool.tags.op("@>")(func.cast(json.dumps([tag]), JSONB))   # 命中 0 条 ❌
```

同一个查询用原生 SQL `tags @> $1`（参数传 JSON 字符串）能命中 1 条，
说明是 ORM 那层的参数绑定问题。改成 `Tool.tags.contains([tag])` 才对 ——
SQLAlchemy 会按 jsonb 正确绑定，不用自己 cast。

**3. `system_config.config_value` 是 text 不是 jsonb**

`jsonb_typeof(config_value)` 直接报 "function does not exist"。这张表没有 id 列，
主键是 `config_key` 字符串。所以读写都要自己 `json.loads` / `dumps`。

### 两处刻意偏离 Java（都是修 Java 的坑）

**1. `providerId` 出参用字符串**

Java 的 `DefaultAiConfigDTO.providerId` 是 `Long`，序列化成 JSON 数字。
但 providerId 是 19 位雪花 ID（约 2e18），**超过 JS 的 `Number.MAX_SAFE_INTEGER`
（9e15）** —— 前端 `JSON.parse` 之后末几位就被改写，再拿去和 `with-models`
返回的 provider.id（字符串）比对时匹配不上。

表现是：设置页的"默认对话模型"明明配置过，打开却显示空。前端代码是
`String(cfg.providerId)`，所以给字符串完全兼容。

**2. 配置键不存在时 upsert，不再静默失败**

Java 的 `updateConfigValue` 在配置项不存在时只 `log.warn` 然后返回 ——
前端看到"保存成功"，实际什么都没写。这里改成 upsert。

### MCP 为什么只做 7 个（诚实说明）

| 接口 | 状态 | 原因 |
|---|---|---|
| CRUD 5 个 + `enabled` + `tools/{name}/toggle` | ✅ | `toggle` 只维护 `disabled_tools` 数组，不需要连接 |
| `test` / `tools` / `tools/refresh` | ❌ 明确 400 | **要真连 MCP server** |

关键事实：**没有 `mcp_tool` 表**，工具列表是运行时从 server 拉的（Java 走
`McpClientService` + Redis 缓存）。而库里两个 server 都是 `disabled`，
其中一个（`mcp-server-chart`）靠 `npx @antv/mcp-server-chart` 起子进程 ——
要先装 node 包才能连。

所以这三个接口**明确抛 400 并说明原因**，而不是返回 `[]` 装作没事：

> 返回空数组的后果：前端显示"该服务器下暂无工具"，用户以为是 server 自己没工具，
> 去改 server 配置 —— 排查方向完全跑偏。

要真做，前置条件是：装 `mcp` SDK → 至少有一个能连通的 server → 才谈得上验证。

自检：
```bash
uv run python -m scripts.tools_api_check      # 39 项
uv run python -m scripts.mcp_api_check        # 28 项
uv run python -m scripts.system_config_check  # 18 项（会备份并恢复真实配置）
```

## 模型供应商管理（阶段 8）—— 横向铺接口的探路

这一轮是为了回答一个问题：**横向补接口的成本到底有多高？**
选 `model-providers` 试，因为它 15 个接口全是 CRUD 类，是最"标准"的一批。

结果：**覆盖率 21/371 (5%) → 36/371 (10%)**，用时约 40 分钟（含端到端验证）。

```
app/services/model_provider_presets.py   5 种类型的字段定义 + 11 条预设（纯数据）
app/services/model_provider_service.py   CRUD + 连通性检查 + 拉模型列表
app/schemas/model_provider.py            DTO / Out
app/api/v1/model_providers.py            15 个端点
scripts/model_provider_check.py          38 项验证
```

### 三个必须照抄 Java 的序列化细节

这三个错了**前端不会报错，只是显示异常**，最难查：

| 契约 | 为什么 | 错了会怎样 |
|---|---|---|
| `apiKey` 只进不出 | Java `@JsonProperty(WRITE_ONLY)` | 把凭证敞开给任何能看列表的人 |
| `config` 返回 **JSON 字符串** | 前端 `parseJsonObject(str)` 直接 `str.trim()` | 传对象 → `TypeError` → 管理页白屏 |
| `id` 返回**字符串** | 19 位雪花 ID 超 JS 安全整数 | 末几位被静默改写 → 改错记录 |

第三条那个 `str.trim()` 在 try 外面，所以不是"解析失败降级"，是**直接抛异常**。
实测踩到：`field_serializer` 只管输出，`model_validate(orm_obj)` 校验时 dict 就炸了
（`Input should be a valid string`），必须改用 `field_validator(mode="before")`。

### 路由顺序（FastAPI 的坑）

```
/presets → /default-model → /with-models → /check → /refresh-cache
→ "" (GET/POST/PUT) → /{provider_id} → /{provider_id}/xxx
```

FastAPI 按**注册顺序**匹配，不像 Spring 优先精确路径。`/{provider_id}` 先注册的话，
`GET /model-providers/presets` 会被它吃掉 → `int("presets")` → 422，
前端看到"参数错误"，完全联想不到是路由顺序。

### 两处刻意偏离 Java

1. **更新时 apiKey 传空 → 保留原 key**。Java 是直接 `set`（传空串就清空）。
   但接口不返回 apiKey，前端编辑弹窗的这个框**必然是空的**，用户改个名字保存
   就把 key 抹了。所以这里改成"非空才覆盖"。验证脚本专门查库确认了这条。

2. **refresh-cache 是 no-op**。Java 要清 AgentScope Model 实例缓存 + Redis +
   多实例广播（不清会沿用旧凭证持续 401）。Python 侧每次调用现建 `LlmConfig`，
   没有可缓存的对象。但接口**必须存在** —— 前端保存后必调，删了就 404。

### 数据侧的两个真实发现

1. **`model_provider` 表 13 列，`entities.py` 原来只映射了 8 列**。少了
   `models_endpoint` / `headers_json` / `extra_json` / `create_time` / `update_time`，
   列表页会显示成空。SQLAlchemy 只 SELECT 映射的列，**不报错**，所以一直没被发现。

2. **库里 5 条原始供应商记录全是 `disabled` 且 `api_key` 为空** ——
   通义千问 / OpenAI / DeepSeek / 小米MiMo / Ollama，是 Java 初始化时的模板。
   实际在用的是另外 5 条 `type=OPENAI`（都走 OpenAI 兼容协议）。
   所以验证脚本不能按 `type=DEEPSEEK` 找测试目标，得按 `baseUrl 含 deepseek + active`。

### 连通性检查是真发请求

`GET /{id}/check` 和 `POST /check` 都会**真发一次 chat/completions**
（`max_tokens=16`，成本约等于零）。只校验"URL 格式 + key 非空"毫无意义 ——
用户最需要知道的是这个 key 到底能不能用。失败时把**上游原文**带回来，
用户才能区分"key 错"（401）和"模型名错"（404/400）。

模型选择四级回退，照抄 Java `resolveModelId`：
显式传入 → `config.modelId` → **该供应商下 type=llm 且 active 的第一个模型** → 最便宜模型。

第三步卡 `type == 'llm'` 是关键：SiliconFlow 那种供应商下可能只有 embedding 模型
（BAAI/bge-*），拿它调 chat 接口会被上游拒掉（Java 注释写的错误码 20012）。

自检：`uv run python -m scripts.model_provider_check`（默认不碰外网，4 秒）
加 `--live` 才做真实连通性检查（几分钱 + 依赖网络）

## 工作流外部节点（阶段 7）

### 先修了个更严重的问题：noop 把"没实现"伪装成"成功"

原来的 `handle_noop` 遇到未知节点类型时 **记一条 warning 就原样通过**，注释还写着
"跳过比失败更有用"。这个判断是错的，实际后果是这样一条链：

```
api 节点被跳过      → 没有 body
script 节点被跳过   → 没有 title / chartData
condition 拿不到 statusCode → 条件恒不成立，走错分支
llm 拿着空变量瞎编
→ 整条流程 status=success，用户看到一段编出来的答案
```

**失败没有消失，只是从"节点报错"变成了"答案错"，而且更隐蔽。**
这和之前踩的 `retrievalResult` 变量名写错是同一类问题：链路全绿、答案全错、无从排查。

现在未知节点一律抛 `UnsupportedNodeType`，由 executor 的 try/except 转成
`workflow_node_end(status=error)` + `workflow_done(error=...)`。
错误信息带**真实节点类型**（用闭包捕获，因为 `type` 在外层 node 对象上、不在 `data` 里）：

```
节点 mcp_1 的类型 'mcp' 尚未在 Python 版实现，工作流已中止。
已支持: api, condition, confirm, end, input, llm, output, retrieval, script, tool, variable, ...
```

### api 节点：SSRF 防护 + 内网开关

`api` 是唯一让用户填任意 URL 的入口。不校验就是标准 SSRF：
能打通云元数据（`169.254.169.254` 拿临时凭证）、内网 Redis/PG、K8s API。

黑名单（默认拒绝）：`10/8` `172.16/12` `192.168/16` `127/8` `169.254/16`
`0.0.0.0/8` `::1` `fc00::/7` `fe80::/10`，外加只允许 http/https。

**但硬禁内网会让这个节点在自托管环境里没法用** —— 老板的工作流大概率要调自家服务。
所以留了一个正规开关：

```bash
# .env：允许 api 节点访问内网（部署方决定，不是画布上能改的节点字段）
WORKFLOW_ALLOW_PRIVATE_NETWORK=true
```

刻意**不做成节点配置项**：那等于让画工作流的人自己关掉防护。

**已知残余风险**：先解析域名再放行，httpx 之后会再解析一次，两次之间 DNS 可变
（rebinding）。彻底修要自建连到固定 IP + 伪造 Host 头，Java 侧也没做。

输出 `statusCode`(int) / `body`(str) / `result`(=`body`)。
`body` **保持原始字符串**，不自动 parse 成 dict —— 响应可能是 HTML 报错页或纯文本，
自动 parse 会抛异常把工作流搞挂。下游 script 自己 `JSON.parse(params.body)`。

### script 节点：`main(params)` 约定

和 `execute_code` 工具**不是同一套**，别混：

| | 入口 | 参数 |
|---|---|---|
| `execute_code` 工具 | `def main():` | 无 |
| `script` 节点 | `def main(params):` | inputParams 渲染的 dict |

**参数走文件注入**（`__params__.json`）而不是拼进代码：params 里有任意中文、引号、
换行、反斜杠，拼进代码要过 JSON + 目标语言两层转义，一处漏了就 SyntaxError，
而且报错位置指向 wrapper 不是用户脚本，根本查不出来。

**Java 不用 Jackson 解析 params**：用户机器上只有 JDK，没有 Spring 的 jar。
第一版写成 `new ObjectMapper().readValue(...)`，编译直接报
「程序包 com.fasterxml.jackson.databind 不存在」。改成**生成 Java 字面量**：

```java
Map<String, Object> __p1 = new HashMap<>();
__p1.put("body", "...");
params.put("body", __p1);
```

不用 `Map.of()` / `List.of()` 是因为它们**不接受 null 值**，params 里 None 很常见。

失败策略 `errorStrategy: "defaultValue"` 时用 `defaultOutput` 降级，否则抛异常中止。

### tool 节点：复用 chat 链路

直接调 `tools/registry.py` + `tools/executor.py`，**不另写一套调用逻辑** ——
分开写的结果就是"聊天里能用的工具，工作流里行为不一样"（参数校验、错误包装、
超时都是现成的）。

结果 dict 会平铺出去（`{{success}}` / `{{files}}` 直接可用），同时保留
`toolResult` / `toolOk`。

### 失败也要落库，否则用户看不到

节点报错只存在于那一秒的 SSE 帧里，用户一刷新就只剩自己的提问。
所以失败时也存一条 assistant 消息（content 空），metadata 带：

```json
{
  "workflow": true,
  "workflowError": {"message": "节点 mcp_1 的类型 'mcp' 尚未在 Python 版实现..."},
  "workflowEvents": [ {"type":"workflow_node_complete","nodeLabel":"MCP工具调用","success":false} ]
}
```

前端 `resolveWorkflowFailureFromEvents()` 从 `workflowEvents` 里倒序找
`success === false` 的节点来渲染失败提示。不存这条，报错就等于白报。

自检：`uv run python -m scripts.nodes_ext_check`（本地 stub，4 秒，不碰公网）
自检加 `--with-chat` 会多验一项"失败落库"（写库 + 走公网，约 30 秒）

## 工具调用持久化（阶段 6）

一次工具调用的结果要写**两个地方**，粒度不同、用途不同：

```
tool_call 事件 ──┬─▶ message.tool_events (JSONB)   前端渲染卡片；历史列表再瘦身
                 └─▶ tool_calls 表 (TEXT)          完整原文；按 toolCallId 回查
```

### 为什么必须分两处

`message` 是分页查询的主力表，工具结果动辄几千字（一次 `local_read_file` 就
1320 字，一次 `execute_code` 能到 2766 字）。全塞进 `tool_events` 的后果：
每翻一页都要从 PG 拉几百 KB 的 JSONB，前端还得全量解析。所以：

- **tool_events 存"够渲染"的信息**，长结果在**读取时**剥离（不是写入时）
- **tool_calls 存原文**，前端点「查看结果」时才按 id 拉一条

### 一个 id 串起三处

```
new_id() ──┬─▶ SSE tool_call.toolCallId       前端立刻知道这张卡片是谁
           ├─▶ SSE tool_result.toolCallId     配对结果
           ├─▶ tool_events[].toolCallId       刷新后仍能回查
           └─▶ tool_calls.id (主键)           回查时按它查
```

**为什么在工具开始执行前就生成 id**：SSE 是流式的，`tool_call` 帧必须先推出去
（前端要立刻画出"正在执行"的卡片）。如果等结果回来才生成 id，那一帧就没法带 id，
前端后面拿不到回查凭据。所以 id 必须**预生成**，靠这个 id 把"事件"和"数据库行"绑在一起。

### 三条截断规则（阈值全抄 Java）

| 场景 | 规则 | 出处 |
|---|---|---|
| SSE `tool_result.result` | 纯文本 2000 字截断；**JSON 一律不截** | `ToolEventGenerator.truncateForSse` |
| 历史列表 `tool_events` | ≤500 字留原文；>500 删 `result` 写 `resultTotalLength` | `MessageServiceImpl` |
| 写文件类工具的 args | 单字段 >120 字 → 前 20 字 + `...(argument truncated...)` | `ToolArgsSanitizer` |

**「JSON 不截」是正确性要求不是优化**：前端拿到 `result` 会 `JSON.parse()`，
把一个 4000 字的 JSON 硬切一半会得到非法 JSON，解析抛异常后整张卡片渲染失败。
Java 的判据是只看首字符（`startsWith("{") || startsWith("[")`），这里用
`looks_like_json()` 照抄 —— 不做完整 parse，因为工具报错时结果本身可能就是
半截非法 JSON，parse 失败会走进截断分支，反而把内容切了。

**瘦身只发生在 DTO 层**：`list_messages` 里把 `MessageOut` 转出来后再裁，
绝不改 ORM 对象 —— 改了会让 session 变 dirty，后续任何 flush 都可能把
瘦身数据写回库，把完整记录永久毁掉。`tool_persist_check` 第 5 项专门盯这个。

### 越权：三跳校验

`toolCallId` 是雪花 ID（不可猜），但它会出现在分享截图、日志、浏览器 devtools 里。
所以回查时必须走完 tool_call → message → chat_session.user_id 三跳，
不能只凭 id 就返回。`tool_persist_check` 用一个不存在的 userId 验证会抛 404。

### 边界

- 正文为空但调了工具的轮次**也会存 assistant 消息**（content 空串）。
  否则 `tool_events` 没有宿主，刷新后工具卡片凭空消失。
- 删消息时**级联删 tool_calls**，否则留下可以按 id 回查的孤儿记录。
- `tool_input` 落 JSONB **对象**（不是"字符串包着 JSON"）—— 查过库里 Java 写的
  748 条记录，`jsonb_typeof` 全是 `object`，Python 侧存解析后的 dict 才能对齐。

自检：`uv run python -m scripts.tool_persist_check`（真跑一轮对话 + 五项核对）

## 流式架构：producer 独立于连接

这是本阶段最重要的设计，也是 `reconnect` 能真正续流的前提：

```
_produce(task)  ──写──▶  run.frames 缓冲  ◀──读──  stream 连接
                                        ◀──读──  reconnect 连接
```

- 生成逻辑跑在 `asyncio.create_task` 里，**客户端断开不取消 task**
- 连接只是"读缓冲"，断了只是少一个读者
- 重连带 `lastEventId`，从断点之后读（实测断开于 eventId=1，重连从 id: 2 精确接上）

别把它改回"生成逻辑直接写在生成器里"——那样断线就等于中断。

## 标题生成不用 AI

对齐 Java `TraceMiddleware`：标题 = **首条用户提问**压缩空白后截断 30 字。
Java 注释原文："彻底避免 AI 偏向回答内容"。只在标题还是 `"新对话"` 时生成，
不覆盖用户手动改过的标题。

## RAG（阶段 3）

```python
# 检索链路
question → embed(BAAI/bge-large-zh-v1.5, 1024d) → pgvector `<=>` 余弦距离
         → 1 - 距离 = 分数 → 按 Agent 绑定的知识库过滤 → topK
```

**⚠️ 元数据是错的，别信 `embedding.model_name`**

库里那个字段写 `Qwen/Qwen3-VL-Embedding-8B`，但实测反推（`scripts/rag_model_probe.py`）
证明实际是 **`BAAI/bge-large-zh-v1.5`**：

| 模型 | 与库里向量的相似度 |
|---|---|
| Qwen/Qwen3-VL-Embedding-8B | 0.0128 |
| BAAI/bge-large-zh-v1.5 | **1.000000** ✅ |

用错模型的后果是**静默失败**——维度都是 1024，`<=>` 不报错，只是永远返回空/不相关。
这类 bug 最难查，所以有 `rag_check.py` 的自命中测试：拿 chunk 自己的内容当查询，
唤不回自己就说明链路有问题。

**`SET LOCAL hnsw.ef_search`**：默认 40，调到 100 召回率 ~90%→~98%，代价 +10~20ms。
必须用 `SET LOCAL`（只影响当前事务），普通 `SET` 会污染连接池里的后续查询。

**检索范围走 ContextVar 不走工具参数**：`query_knowledge(question)` 只有 question
一个参数，搜哪几个库由后端从 `agent.config.knowledges` 注入（`app/tools/context.py`）。
让模型传 knowledgeId 它就会瞎编。

### 成本控制（实测 11,561 → 3,761 token，-67%）

模型会自发做 multi-query——实测它把"多 Agent 有哪些挑战"拆成 7 个角度重搜。
召回是好了，但每次结果都进上下文，成本按倍数涨。三道闸：

| 闸 | 参数 | 作用 |
|---|---|---|
| 跨轮去重 | `ctx.seen_chunk_ids` | 同一条 chunk 只给模型一次。**最有效**：换角度重搜命中的往往是同一批 |
| 减量 | `top_k=3`、`MAX_CHUNK_CHARS=400` | 砍掉一半上下文 |
| 次数上限 | `MAX_RETRIEVAL_ROUNDS=3` | 到上限返回"无新资料" |
| 步数硬闸 | `HARD_MAX_STEPS=8` | `config.maxExecutionSteps` 配的是 20，没考虑 token 成本 |

**一个 Agent 设计上的通用教训**：配额用完时**要返回 `ok: true` + 空结果**，
不能用 `ok: false`。错误语义会让模型认为"工具坏了，再试一次"——实测白烧 2 轮 step。

### 引用（ragReferences）

检索到的 chunk 会作为引用，随两处一起给前端：

| 出口 | 用途 |
|---|---|
| `[DONE]` 事件里的 `ragReferences` | 流式结束时实时渲染引用卡片 |
| `message.metadata.ragReferences` | 历史回看（`GET /messages` 读出来） |

结构（对齐前端 `debugPresets.js`，**所有 ID 都是字符串**）：

```json
{
  "documentName": "infoq.pdf",
  "contentPreview": "前 200 字...",
  "score": 0.6318,
  "sourceType": "document",
  "knowledgeId": "2094693967276167170",
  "documentId": "2108491635962597377",
  "chunkId": "2108495203083931650"
}
```

**它不进模型上下文**——只走 SSE 和 DB metadata，所以不影响 token 成本。
`contentPreview` 截 200 字：存 400 字会让 metadata 比正文还大。

## SSE 协议（改这块前必读）

帧格式 `data:<payload>\n\n`，**`data:` 后面不能有空格** —— 前端 `processSseLines`
用 `line.substring(5)` 后直接判断 `[DONE]` 前缀，多一个空格控制帧就会被当正文显示出来。

| payload 形态 | 含义 |
|---|---|
| `[REQUEST_ID]<id>` | 请求 ID，供断线重连（阶段 1 只发不用） |
| 纯文本 | 正文 token，直接追加到气泡 |
| `[STATUS]{...}` | 控制事件：`tool_call` / `tool_result` / `reasoning_content` / `error` |
| `[DONE]{...}` | 结束，带 `userMessageId` / `assistantMessageId` / `totalTokens` |

## 两个必须记住的坑

**1. 统计字段不用 SQL `now()`**
远程 PG 服务器时区是 UTC，`now()` 比应用时间早 8 小时。`last_message_at` 用 `now()`
会让会话排序错乱。统一用 Python `datetime.now()` 传参。

**2. NOT NULL 列必须在模型层给 default**
库里虽然有 `DEFAULT 'text'`，但 SQLAlchemy 会把每个映射列显式写进 INSERT，
插入 NULL 而不触发默认值 → not-null violation。见 `models/entities.py` 的 `Message`。

阶段 0-3 走完就覆盖了七成 Python 工程能力，再往后是体力活。

---

## Java → Python 对照（写代码时随时看）

| Java | 这里怎么写 |
|---|---|
| `@Autowired` 构造注入 | `Depends()` 或直接 `new`（`api/deps.py` 有例子） |
| `IUserService` + `UserServiceImpl` | **不要接口**，一个类就够 |
| DTO / VO / Entity 三套 | 一个 Pydantic model 常够 |
| `@Transactional` | `async with session.begin()` |
| `StpUtil.getLoginIdAsLong()` | `CurrentUserId` 依赖（阶段 0 固定 dev 用户） |
| `@RequestParam(defaultValue="1")` | `Query(1, alias="pageNum")` |
| `@JsonSerialize(ToStringSerializer)` | `@field_validator` 转 str（见 `schemas/chat_session.py`） |
| MyBatis-Plus `Page<T>` | `PageResult`（字段名对齐 MP） |
| Lombok `@Data` | Pydantic `BaseModel` / `@dataclass` |
| `Flux<String>` | `AsyncIterator[str]`（阶段 1） |
| 线程池 | asyncio 事件循环（IO 密集）/ `ProcessPoolExecutor`（CPU 密集） |

---

## 待核对清单

- [x] `page_by_user` 排序与 Java `listMySessions` 对齐（已核对：`pinned DESC, last_message_at DESC`，
      **PG 默认 NULLS FIRST，不要加 nulls_last**）
- [x] snowflake ID 生成器（`app/core/snowflake.py`）
- [x] `PageResult` 字段够用（前端只读 records/total/size/current/pages）
- [ ] **真认证**替换 `get_current_user_id`（现在固定返回 `dev_user_id`，
      登录接口也不校验密码 —— 见「已实现的接口」）
- [ ] 多 worker 部署（`ChatRun` 注册表 / HITL 快照 / 工具上下文都是**进程内存**，
      现在只能单进程跑；要扩需要换 Redis）

## 更新 dump 快照

```bash
PGPASSWORD=<远程密码> "C:/PostgreSQL/15/pgsql/bin/pg_dump" \
  -h localhost -p 15432 -U postgres -d lengbot \
  -Fc --no-owner --no-privileges -f db/lengbot_seed.dump
```
