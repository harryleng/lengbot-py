# 手写工具循环 vs LangGraph：实测对比

同一天、同一个模型（deepseek-flash）、同一个任务、同一批工具，两种实现各跑一遍。

任务：
> 请在工作区创建文件 notes/lg_test.md，内容写「LangGraph 写入成功」，然后用读取工具读回来告诉我内容

## 结果：两边都对了

| | 手写版 | LangGraph 版 |
|---|---|---|
| 工具调用次数 | 2（write → read） | 2（write → read） |
| 文件是否落盘 | ✅ 15 字节 | ✅ 24 字节 |
| 最终回答 | 准确复述两次调用 | 准确复述两次调用 |
| 耗时 | ~5s | 4.3s |

功能上等价。差别在学习成本和可控性。

## 代码量：3 : 1，但这个对比有水分

| 部分 | 手写版行数 | LangGraph |
|---|---|---|
| 工具循环主逻辑 | 113（含注释，有效约 60） | `build_graph` 约 40 |
| 流式 `tool_calls` 解析（分片累积） | 约 25 | 0（框架内部处理） |
| 工具执行器（参数解析/校验/异常兜底） | 95（有效约 40） | 0（`ToolNode` 接管） |
| 合计 | **约 125 行有效代码** | **约 40 行** |

**水分在哪**：LangGraph 版**没有实现 SSE 传输**，它只是把事件打到控制台。
如果要接到前端，还要写一层翻译（估计 80-100 行）：把 `on_chat_model_stream` 喂成
`data:` 帧、`on_tool_start/end` 翻译成 `[STATUS]{"type":"tool_call"}`、
自己算 `contentOffset`、自己处理取消。

所以真实比例更接近 **2 : 1**，不是 3 : 1。

## 框架接管了什么

| 你要做的事 | 手写版 | LangGraph 版 |
|---|---|---|
| 判断"还要不要继续调工具" | 自己写 `if not round_calls: break` | `add_conditional_edges` 声明 |
| 回灌 assistant 的 tool_calls | 手动 append 三种消息 | `ToolNode` 内部处理 |
| 工具失败不中断 | `execute_tool_call` 里 try/except | `ToolNode(handle_tool_errors=True)` |
| 防死循环 | `if step >= max_steps: 不传 tools` | `recursion_limit` |
| 状态传递 | 自己维护 `messages` 列表 | `MessagesState` + reducer |

这四个能力，LangGraph 用声明式写法一次给全。手写版每一条都要显式处理。

## 框架**不**接管什么（这才是重点）

**流式传输层一律不管。** 实测事件流：

```
on_chat_model_stream   × 128    ← 128 个 token chunk，要自己拼成 SSE 帧
on_chain_stream        × 10
on_chain_start         × 9
on_chain_end           × 9
on_chat_model_start    × 3
on_chat_model_end      × 3
on_tool_start          × 2     ← 要翻译成 tool_call 事件
on_tool_end            × 2     ← 要翻译成 tool_result 事件
```

`astream_events` 给的是**扁平的原始事件流**，不是网页能直接消费的 SSE。
前端要的 `data:[STATUS]{"type":"tool_call","contentOffset":17,...}` 这种帧，
一帧都不会自动产生。

这也解释了为什么 lengbot 有 500 行的 `ChatStreamSseHelper`——
框架换哪个都一样，传输层是业务自己的事。

## 真正值得用 LangGraph 的地方

工具循环不是它的杀手锏（40 行 vs 60 行，差别不大）。
它的价值在这两件事，而这两件**手写版完全没有**：

### 1. Checkpointer：每步持久化

```python
graph.compile(checkpointer=AsyncPostgresSaver(pool))
```

一行配置，得到：
- 每个 super-step 自动落库
- 崩溃后从最后一个断点恢复
- 同一个 `thread_id` 可以回放历史状态

lengbot 为这件事手写了 `workflow_checkpoint` 表 + `WorkflowCheckpointRecovery` 自愈扫描
（约 600 行），还有"非幂等节点重复执行"的边界风险。LangGraph 是原生的。

### 2. interrupt：HITL 挂起恢复

```python
answer = interrupt({"question": "确认要继续吗？"})
# 后续用 Command(resume=answer) 从断点继续
```

lengbot 用 Redis 存 `WorkflowSuspendedRun`（TTL 24h）+ 手动还原 `nodeOutputs`。
LangGraph 里挂起态在 checkpoint 里，**不会过期**，而且能跳过已执行的节点继续。

## 结论：什么时候用哪个

**手写更合适**：
- 只有"问答 + 工具"两个节点，没有分支（现在这个阶段）
- 需要对 SSE 每一帧做精确控制（占位事件、心跳、断点续传）
- 想彻底搞懂机制（学习阶段）

**LangGraph 更合适**：
- 图开始有分支、循环、并行（多 Agent 协作、条件路由）
- 需要断点恢复、HITL、状态回放
- 团队协作，需要图结构可视化

**一句话**：LangGraph 省的是"状态与调度"，不省"传输与协议"。
所以它替代不了 `ChatStreamSseHelper`，但能替掉 `WorkflowCheckpointRecovery`。

## 实测：checkpointer + interrupt（LangGraph 真正的杀手锏）

代码：`app/agents/langgraph_durable.py`，验证脚本 `scripts/durable_check.py`。

### 场景 1：写文件任务被挂起

```
▶ 节点 agent AIMessage: 我现在就创建这个文件。
⏸  挂起 @ __interrupt__: {
      "type": "approval_required",
      "message": "以下操作会修改工作区文件，确认执行吗？",
      "tools": [{"name": "local_write_file",
                 "args": {"path": "notes/durable.md", "content": "HITL 测试通过"}}]
    }
挂起后状态: next=('approval',)
库里 checkpoints 行数: 3
checkpointer 自动创建的表: ['checkpoint_blobs', 'checkpoint_migrations',
                          'checkpoint_writes', 'checkpoints']
```

**一行 `graph.compile(checkpointer=saver)` 换来 4 张表 + 自动落库。**
lengbot 为同样的事手写了 `workflow_checkpoint` 表和 `WorkflowCheckpointRecovery`，
还要处理"崩溃时正在执行的节点副作用重复"。

### 场景 2：`Command(resume="approve")` 继续

```
▶ 节点 approval
▶ 节点 tools  {"ok": true, "path": "notes/durable.md", "bytes": 17, "created": true}
▶ 节点 agent
▶ 节点 tools  {"ok": true, "size": 17, "content": "HITL 测试通过"}
▶ 节点 agent  AIMessage: 已完成...
结束后 next=() （空表示已完成）
```

文件真的写进去了（17 字节）。**挂起前执行过的 agent 节点没有重跑**，
直接从 `approval` 继续 —— 这就是前向续跑，和 lengbot 的 `resumeAfterConfirm` 语义一致。

### 每一步都能回放

```
step=6  source=loop   next=END
step=5  source=loop   next=('agent',)
step=4  source=loop   next=('tools',)
step=3  source=loop   next=('agent',)
step=2  source=loop   next=('tools',)
step=1  source=loop   next=('approval',)
step=0  source=loop   next=('agent',)
step=-1 source=input  next=('__start__',)
```

`graph.aget_state_history(config)` 把每一步都取出来了，含 `next` 和 `source`。
**这是"时间旅行"能力的底座**：想从第 3 步重新分叉，拿那条 snapshot 就行。

lengbot 的 checkpoint 只能"从最后一个挂起点前向续跑"，做不到任意步骤回放。

### 场景 3：`reject` 后模型重新措辞

```
⏸  挂起 ...
→ 回复 reject
▶ 节点 approval  ToolMessage: 用户拒绝执行该操作，请询问用户下一步意图。
▶ 节点 agent     AIMessage: 操作没有执行：写入 notes/durable.md 时，该请求被拒绝了。
                            因此文件尚未创建... 请问接下来希望我怎么做？
```

**关键实现细节**：拒绝时必须给每个 `tool_call` 补一条 `ToolMessage`，
否则 assistant 消息里的 `tool_calls` 没有对应响应，模型会一直等工具结果、下一轮报错说不出话。

```python
return {"messages": [
    ToolMessage(content="用户拒绝执行该操作，请询问用户下一步意图。", tool_call_id=c["id"])
    for c in calls
]}
```

## 两个 Windows 专属的坑

**1. psycopg 不支持 ProactorEventLoop**

```
Psycopg cannot use the 'ProactorEventLoop' to run in async mode
```

Windows 上 Python 3.8+ 默认是 Proactor，必须在建连接池**之前**切：

```python
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
```

注意时机：在协程内设置无效，必须在事件循环创建之前。Linux 服务器没这个问题。

**2. checkpointer 用 psycopg，项目主体用 asyncpg**

两套驱动并存，连接串要转换（去掉 SQLAlchemy 的 `+asyncpg`）：

```python
dsn = settings.database_url.replace("postgresql+asyncpg://", "postgresql://")
```

不能直接把 SQLAlchemy 的 engine 塞给 `AsyncPostgresSaver`。

## 最终结论

| 能力 | 手写版 | LangGraph |
|---|---|---|
| 工具循环 | 60 行 | 40 行（但**不含** SSE 传输） |
| 每步持久化 | 手写 600 行 + 自愈扫描 | `compile(checkpointer=saver)` |
| HITL 挂起 | Redis + TTL 24h，会丢 | `interrupt()`，永不过期 |
| 任意步骤回放 | 做不到 | `get_state_history()` |
| SSE 传输 | 完全可控 | **仍需自己写** |

**LangGraph 省的是「状态与调度」，不省「传输与协议」。**
