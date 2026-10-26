"""聊天编排：落库 → 调模型 → 流式推 → 落库 → 生成标题。

四条铁律（都是踩过坑才立的）：
1. **流式过程中不持有数据库事务**。一次 LLM 调用可能几十秒，长事务会拖住
   autovacuum、占着连接、还可能被服务端断开。拆成「流式前短事务」+「流式后短事务」。
2. **统计字段用原子 UPDATE，不做读-改-写**。lengbot 的 lost update 事故就是
   「读实体 → 内存加 → 全字段回写」造成的，这里直接 `SET x = x + n`。
3. **SSE 帧 `data:` 后面不加空格**。前端是 `line.substring(5)` 直接判断
   `[DONE]` 前缀，多一个空格控制帧就会被当正文显示。
4. **不用 SQL 的 `now()`**。远程 PG 服务器时区是 UTC，比应用时间早 8 小时。

对照 Java：MessageMiddleware（组装+落库）+ ChatServiceImpl.streamCore（流式循环）
+ TraceMiddleware（收尾统计与标题）的合并简化版。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from datetime import datetime

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import DEFAULT_SYSTEM_PROMPT, DEFAULT_TITLE, HISTORY_LIMIT, TITLE_MAX_LEN
from app.core import auth_gate
from app.core.db import SessionLocal
from app.core.snowflake import new_id
from app.models.entities import Agent, ChatSession, Message, ModelProvider
from app.models.entities import ToolCall as ToolCallRecord
from app.schemas.chat import ChatRequestIn
from app.services.chat_runs import ChatRun, registry, tail
from app.services.llm_client import LlmConfig, ToolCall, stream_chat
from app.services.tool_events import (
    build_call_event,
    build_result_event,
    parse_args_for_db,
)
from app.tools.context import AgentToolContext, reset_context, set_context
from app.tools.executor import execute_tool_call, result_for_context
from app.tools.registry import registry as tool_registry
from app.workflow.events import to_sse_event
from app.workflow.executor import WorkflowExecutor
from app.workflow.graph import WorkflowGraph
from app.workflow.nodes import WorkflowContext

logger = logging.getLogger(__name__)


def _parse_ids(raw: object) -> list[int]:
    """把 config 里的 ID 数组（可能是 str/int 混合）转成 int 列表。"""
    if not raw:
        return []
    items = raw if isinstance(raw, (list, tuple)) else [raw]
    out: list[int] = []
    for item in items:
        try:
            out.append(int(item))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
    return out


# 工具循环的硬上限。
# agent.config.maxExecutionSteps 实测配的是 **20**，那个值是按"能跑多少步"设计的，
# 没有考虑 token 成本（实测 8 次工具调用就烧了 11,561 token）。
# 这里加一道硬闸：不管配置给多大，最多 8 步。
HARD_MAX_STEPS = 8


def _status_payload(payload: dict) -> str:
    return "[STATUS]" + json.dumps(payload, ensure_ascii=False)


def _estimate_tokens(value: str) -> int:
    """粗估 token：字符数/4，偏保守。真实值优先取模型返回的 usage。"""
    return max(1, len(value) // 4)


class ChatStreamService:
    async def stream(self, req: ChatRequestIn, user_id: int) -> AsyncIterator[str]:
        """SSE 入口：**只负责读帧**，真正的生成在后台 task 里跑。

        为什么这么分：如果生成逻辑直接写在生成器里，客户端一断开（切走页面、
        网络抖动），生成器被关闭，LLM 调用跟着停 —— 那么 `reconnect` 就只能重放
        已有内容，永远接不上后续。Java 侧用 Sinks.Many 广播实现了「run 独立于连接」，
        这里用 `asyncio.create_task` 达到同样效果：

            producer task  ──写──▶  frames 缓冲  ◀──读──  stream / reconnect 连接

        连接断了只是少一个读者，producer 照跑；重连回来从断点接着读。
        """
        request_id = str(new_id())
        run = registry.create(request_id, user_id)
        run.task = asyncio.create_task(self._produce(run, req, user_id))

        # 客户端断开时这里会被 GeneratorExit 打断，但**不要取消 task** ——
        # 那正是 reconnect 需要它继续跑的原因。task 由 GC 之外的引用（registry）持有。
        async for frame in tail(run, None):
            yield frame

    async def _produce(self, run: ChatRun, req: ChatRequestIn, user_id: int) -> None:
        """后台生产者：准备 → 流式调模型 → 收尾落库。所有帧写进 run.frames。"""
        parts: list[str] = []
        usage_total = 0
        failed = False
        cancelled = False
        assistant_id: int | None = None
        session_id: int | None = None
        user_msg_id: int | None = None
        tokens = 0
        tool_ctx_token = None
        tool_ctx: AgentToolContext | None = None
        # 本轮的工具事件流（进 message.tool_events）与完整结果（进 tool_calls 表）。
        # 两者是**同一次工具调用的两种粒度**：事件流给前端渲染，可能被截断/瘦身；
        # tool_call_logs 存完整原文，前端点「查看结果」时按 id 回查。
        tool_events: list[dict] = []
        tool_call_logs: list[dict] = []

        def push(payload: str) -> None:
            run.append(payload)

        try:
            # ---------- 准备（短事务） ----------
            try:
                async with SessionLocal() as db:
                    prepared = await self._prepare(db, req, user_id)
            except Exception as exc:  # noqa: BLE001
                logger.exception("准备阶段失败")
                push(_status_payload({"type": "error", "message": f"准备失败: {exc}"}))
                push("[DONE]")
                return

            if prepared is None:
                push(_status_payload({"type": "error", "message": "会话或 Agent 不存在"}))
                push("[DONE]")
                return

            session_id = prepared["session_id"]
            user_msg_id = prepared["user_msg_id"]

            # ---------- 工作流分支（WORKFLOW 型 Agent） ----------
            # 对照 Java WorkflowMiddleware：agentType == WORKFLOW 时跳过 LLM 中间件链，
            # 直接跑 DAG。这里同样在进入工具循环前就分走。
            if prepared.get("agent_type") == "workflow":
                output, suspended, wf_error, wf_events = await self._run_workflow(prepared, push)

                # 三种结局都要落一条 assistant 消息，只是内容不同：
                #   成功 → 正文 + metadata.workflow
                #   失败 → 正文为空，metadata 带 workflowError + workflowEvents
                #   挂起 → 不落（等人填表后续跑，此时落库会产生"空回答"的脏数据）
                wf_text = str(output) if output else ""
                wf_meta: dict[str, object] | None = None
                if output and not suspended:
                    wf_meta = {"workflow": True}
                elif wf_error and not suspended:
                    wf_meta = {"workflow": True, "workflowError": {"message": wf_error}}
                    if wf_events:
                        wf_meta["workflowEvents"] = wf_events

                if wf_meta is not None and (wf_text or wf_error):
                    tokens_wf = _estimate_tokens(wf_text)
                    async with SessionLocal() as db:
                        await self._save_assistant(
                            db, session_id, wf_text, tokens_wf, metadata=wf_meta
                        )
                        await db.execute(
                            text(
                                "UPDATE chat_session SET "
                                "message_count = message_count + 2, "
                                "total_tokens = total_tokens + :tokens, "
                                "last_message_at = :ts WHERE id = :sid"
                            ),
                            {"tokens": tokens_wf, "ts": datetime.now(), "sid": session_id},
                        )
                        await self._maybe_update_title(db, session_id)
                        await db.commit()
                    # 预算记账（Java ChatServiceImpl 同位置）：工作流分支也消耗 token
                    from app.services.token_budget_service import record_usage

                    await record_usage(user_id, tokens_wf)

                done_meta: dict[str, object] = {"userMessageId": str(user_msg_id)}
                if suspended:
                    # 挂起不算失败，但这一轮没有产出 —— 用 aborted 让前端别等正文
                    done_meta["workflowSuspended"] = True
                if wf_error:
                    done_meta["workflowError"] = wf_error
                push("[DONE]" + json.dumps(done_meta, ensure_ascii=False))
                return

            # 给工具注入本轮上下文（知识库检索要知道"搜哪几个库"）。
            # ContextVar 而不是全局变量：每个对话的 producer task 各有一份，并发不串。
            tool_ctx = AgentToolContext(
                agent_id=prepared["agent_id"],
                user_id=user_id,
                knowledge_ids=prepared["knowledge_ids"],
            )
            tool_ctx_token = set_context(tool_ctx)

            # 第一帧就把 requestId 告诉前端 —— 它后面 stop / reconnect 都要用
            push(f"[REQUEST_ID]{run.request_id}")

            # ---------- 工具循环（不持事务） ----------
            #
            # 这是 Agent 的心脏。一轮完整的交互是：
            #   问模型 → 模型说"我要调 calculator" → 我们执行 → 结果喂回去 → 再问模型
            # 直到模型不再要工具（或达到步数上限），才产出最终回答。
            #
            # 三个容易踩的点：
            # 1. 必须把 assistant 的 tool_calls 消息原样回灌，否则下一轮模型
            #    不知道"我刚刚请求过什么"，会重复调同一个工具。
            # 2. 每个工具结果要作为 role=tool 消息，带 tool_call_id 对应回去。
            # 3. 达到 step 上限后**不再传 tools**，模型只能输出文本 —— 这样就
            #    天然避免了死循环，不需要额外判断。
            messages = prepared["messages"]
            tools_by_name = {spec.name: (spec, row) for spec, row in prepared["tool_pairs"]}
            tools_schema = [spec.openai_schema() for spec, _ in prepared["tool_pairs"]] or None
            max_steps = prepared["max_steps"]
            content_offset = 0
            step = 0

            while True:
                round_text: list[str] = []
                round_calls: list[ToolCall] = []
                try:
                    allow_tools = tools_schema if step < max_steps else None
                    async for ev in stream_chat(prepared["llm"], messages, tools=allow_tools):
                        if run.cancel.is_set():
                            cancelled = True
                            break
                        if ev.kind == "text":
                            round_text.append(ev.text)
                            parts.append(ev.text)
                            content_offset += len(ev.text)
                            push(ev.text)
                        elif ev.kind == "tool_calls":
                            round_calls = ev.tool_calls
                        elif ev.kind == "usage" and ev.usage:
                            usage_total = int(ev.usage.get("total_tokens") or 0) or usage_total
                except Exception as exc:  # noqa: BLE001
                    failed = True
                    logger.exception("模型调用失败")
                    push(_status_payload({"type": "error", "message": f"模型调用失败: {exc}"}))
                    break

                if cancelled or not round_calls:
                    break

                # 1. 回灌 assistant 的 tool_calls（协议要求，不能省）
                messages.append(
                    {
                        "role": "assistant",
                        "content": "".join(round_text) or None,
                        "tool_calls": [
                            {
                                "id": c.id,
                                "type": "function",
                                "function": {"name": c.name, "arguments": c.arguments},
                            }
                            for c in round_calls
                        ],
                    }
                )

                # 2. 逐个执行并回灌结果
                for call in round_calls:
                    spec, row = tools_by_name.get(call.name, (None, None))
                    display = (row.display_name if row else "") or call.name
                    icon = (row.icon if row else "") or ""

                    # toolCallId 必须**在调用开始前**生成：SSE 的 tool_call 事件要带上它，
                    # 前端才能在结果回来之前就拿到"这张卡片的 id"，之后按 id 回查完整结果。
                    # 它同时是 tool_calls 表主键 —— 一个 id 两处用，不会对不上。
                    tool_call_id = new_id()

                    call_evt = build_call_event(
                        tool_name=call.name,
                        display_name=display,
                        icon=icon,
                        args_json=call.arguments,
                        content_offset=content_offset,
                        tool_call_id=tool_call_id,
                    )
                    tool_events.append(call_evt)
                    push(_status_payload(call_evt))

                    result, ok = await execute_tool_call(spec, call.name, call.arguments)
                    logger.info("工具执行 %s ok=%s len=%d", call.name, ok, len(result or ""))

                    result_evt = build_result_event(
                        tool_name=call.name,
                        display_name=display,
                        icon=icon,
                        result=result or "",
                        content_offset=content_offset,
                        tool_call_id=tool_call_id,
                    )
                    tool_events.append(result_evt)
                    push(_status_payload(result_evt))

                    # 完整结果留一份待落库（tool_events 里那份会被截断，不够用）
                    tool_call_logs.append(
                        {
                            "id": tool_call_id,
                            "tool_name": call.name,
                            "tool_input": parse_args_for_db(call.arguments),
                            "tool_output": result or "",
                            "status": "success" if ok else "error",
                            # Java 侧把整个错误输出同时塞进 error_message，
                            # 这里保持一致 —— 排查时不用再去翻 tool_output
                            "error_message": None if ok else (result or ""),
                        }
                    )

                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            # 进上下文的是更严格的截断（2000 字符）
                            "content": result_for_context(result, tool_name=call.name),
                        }
                    )

                # 3. 通知前端这批工具都跑完了（前端据此把卡片从"进行中"改成"已完成"）
                push(_status_payload({"type": "tool_complete", "contentOffset": content_offset}))
                step += 1

            # ---------- 收尾（短事务） ----------
            full = "".join(parts)
            tokens = usage_total or _estimate_tokens(full)

            try:
                async with SessionLocal() as db:
                    # metadata 里除了 aborted，还要存 ragReferences ——
                    # 前端从历史消息回看时也要显示引用来源，不能只在流式时给。
                    msg_meta: dict[str, object] = {}
                    if cancelled or failed:
                        msg_meta["aborted"] = True
                    if tool_ctx is not None and tool_ctx.rag_references:
                        msg_meta["ragReferences"] = tool_ctx.rag_references

                    # 正文为空也要存：一轮只调工具、没说人话时（模型把话都放在
                    # 最终回答里，但可能被 stop 或 max_tokens 吃掉），如果不存这条
                    # assistant 消息，tool_events 就没有宿主，刷新页面工具卡片全丢。
                    if full.strip() or tool_events:
                        assistant_id = await self._save_assistant(
                            db,
                            session_id,
                            full,
                            tokens,
                            metadata=msg_meta or None,
                            tool_events=tool_events or None,
                        )
                        # tool_calls 落库必须晚于 assistant 消息 id 确定，
                        # 因为 message_id 是外键语义上的归属（虽然库里没建约束）。
                        await self._save_tool_calls(db, assistant_id, tool_call_logs)
                    await db.execute(
                        text(
                            "UPDATE chat_session SET "
                            "message_count = message_count + :delta, "
                            "total_tokens = total_tokens + :tokens, "
                            "last_message_at = :ts "
                            "WHERE id = :sid"
                        ),
                        {
                            "delta": 2 if assistant_id else 1,
                            "tokens": tokens,
                            # 不用 SQL now()：服务器时区是 UTC，会比应用时间早 8 小时
                            "ts": datetime.now(),
                            "sid": session_id,
                        },
                    )
                    await self._maybe_update_title(db, session_id)
                    await db.commit()
                    run.assistant_message_id = assistant_id
            except Exception:  # noqa: BLE001
                logger.exception("收尾落库失败")
                assistant_id = None

            # 预算记账（Java ChatServiceImpl:252/639 同位置：两条 persist 路径
            # 只记一次）。放在落库 try 外 —— 记账失败不该把 assistant_id 清掉。
            # cancelled/failed 也记：消耗已经发生，Java 同样照记。
            if tokens:
                from app.services.token_budget_service import record_usage

                await record_usage(user_id, tokens)

            # API Key 日配额扣减（Java ChatServiceImpl:256 同位置）。
            # Java 对扣减失败不做拦截（只扣不拦，超配额调用照样完成），Python 同语义。
            api_key_id = auth_gate.current_api_key_id.get()
            if api_key_id and tokens:
                from app.services.api_key_service import consume_quota

                async with SessionLocal() as db:
                    allowed = await consume_quota(db, api_key_id, tokens)
                if not allowed:
                    logger.warning(
                        "[API Key] 日配额不足，本次未扣减（调用已完成）: keyId=%s tokens=%s",
                        api_key_id, tokens,
                    )

            # ---------- 结束帧 ----------
            done_meta: dict[str, object] = {}
            if user_msg_id:
                done_meta["userMessageId"] = str(user_msg_id)
            if assistant_id:
                done_meta["assistantMessageId"] = str(assistant_id)
            if tokens:
                done_meta["totalTokens"] = tokens
            if cancelled or failed:
                done_meta["aborted"] = True
            # 引用随 [DONE] 一起推给前端（实时渲染）；历史回看走 message.metadata
            if tool_ctx is not None and tool_ctx.rag_references:
                done_meta["ragReferences"] = tool_ctx.rag_references
            push("[DONE]" + json.dumps(done_meta, ensure_ascii=False))
        finally:
            if tool_ctx_token is not None:
                reset_context(tool_ctx_token)
            # 无论正常结束、取消、异常，都要放开重连的订阅者
            run.done.set()

    # ------------------------------------------------------------------
    # 准备阶段
    # ------------------------------------------------------------------

    async def _prepare(self, db: AsyncSession, req: ChatRequestIn, user_id: int) -> dict | None:
        # 1. 会话：有 id 就校验归属，没有就新建
        if req.session_id:
            session = await db.get(ChatSession, int(req.session_id))
            if session is None or session.deleted != 0 or session.user_id != user_id:
                return None
            session_id = session.id
            agent_id = session.agent_id
        else:
            agent_id = int(req.agent_id) if req.agent_id else None
            if agent_id is None:
                return None
            session_id = new_id()
            now = datetime.now()
            db.add(
                ChatSession(
                    id=session_id,
                    agent_id=agent_id,
                    user_id=user_id,
                    title=DEFAULT_TITLE,
                    status="active",
                    context={},
                    message_count=0,
                    total_tokens=0,
                    pinned=False,
                    create_time=now,
                    update_time=now,
                    deleted=0,
                )
            )

        # 2. Agent 配置 → 模型供应商
        agent = await db.get(Agent, agent_id) if agent_id else None
        if agent is None:
            return None

        # WORKFLOW 型 Agent：走工作流引擎，不进 LLM 中间件链。
        # 关键差别：它的 config 通常是空 `{}`，模型配置在**各个 LLM 节点**里，
        # 所以不能像 chat 那样要求 agent 级 provider 存在（否则直接 return None）。
        if (agent.agent_type or "chat") == "workflow":
            now = datetime.now()
            user_msg_id = new_id()
            db.add(
                Message(
                    id=user_msg_id,
                    session_id=session_id,
                    role="user",
                    content=req.message,
                    content_type="text",
                    message_type="text",
                    token_count=_estimate_tokens(req.message or ""),
                    create_time=now,
                )
            )
            await db.commit()
            return {
                "session_id": session_id,
                "user_msg_id": user_msg_id,
                "agent_type": "workflow",
                "agent_id": agent_id,
                "user_id": user_id,
                "user_message": req.message,
                "knowledge_ids": _parse_ids((agent.config or {}).get("knowledges")),
            }

        cfg_map = agent.config or {}
        provider_id = cfg_map.get("providerId")
        model_name = cfg_map.get("modelId")
        provider = await db.get(ModelProvider, int(provider_id)) if provider_id else None
        if provider is None or not provider.base_url or not provider.api_key:
            return None

        llm = LlmConfig(
            base_url=provider.base_url,
            api_key=provider.api_key,
            model=str(model_name),
            max_tokens=int(cfg_map.get("maxTokens") or 2048),
            temperature=cfg_map.get("temperature"),
            top_p=cfg_map.get("topP"),
        )

        # 3. 落库用户消息
        now = datetime.now()
        user_msg_id = new_id()
        db.add(
            Message(
                id=user_msg_id,
                session_id=session_id,
                role="user",
                content=req.message,
                content_type="text",
                message_type="text",
                token_count=_estimate_tokens(req.message or ""),
                create_time=now,
            )
        )
        await db.commit()

        # 4. 组装 messages：system + 最近 N 条历史（含刚插入的这条）
        messages: list[dict[str, str]] = [
            {
                "role": "system",
                "content": (agent.system_prompt or "").strip() or DEFAULT_SYSTEM_PROMPT,
            }
        ]
        rows = (
            (
                await db.execute(
                    select(Message)
                    .where(Message.session_id == session_id)
                    .order_by(Message.create_time.desc(), Message.id.desc())
                    .limit(HISTORY_LIMIT)
                )
            )
            .scalars()
            .all()
        )
        for m in reversed(rows):
            if m.content and m.role in ("user", "assistant"):
                messages.append({"role": m.role, "content": m.content})

        return {
            "session_id": session_id,
            "user_msg_id": user_msg_id,
            "llm": llm,
            "messages": messages,
            # 工具：spec 负责执行和 schema，row 负责给前端展示用的中文名和图标
            "tool_pairs": await tool_registry.resolve_with_meta(db, cfg_map),
            "max_steps": min(int(cfg_map.get("maxExecutionSteps") or 5), HARD_MAX_STEPS),
            # 传给工具的上下文（知识库检索要知道"搜哪几个库"）
            "agent_id": agent_id,
            "knowledge_ids": _parse_ids(cfg_map.get("knowledges")),
        }

    async def _run_workflow(
        self, prepared: dict, push
    ) -> tuple[str | None, bool, str | None, list[dict[str, object]]]:
        """执行工作流，把事件翻译成 SSE 帧推给前端。

        @return (最终输出, 是否挂起, 错误信息, 节点事件列表)

        为什么要返回**节点事件列表**：失败时 `output` 是 None，正文一片空白。
        如果不把失败的 `workflow_node_complete` 存进 `message.metadata.workflowEvents`，
        用户一刷新就只剩自己的提问 —— 没有回答，也没有"哪一步失败了"，
        看起来像消息丢了。前端 `resolveWorkflowFailureFromEvents()` 正是从这个
        数组里倒序找 `success === false` 的节点来渲染失败提示的。
        """
        agent_id = prepared["agent_id"]
        # 取图走统一入口：**优先已发布版，没有才回退草稿**。
        #
        # 原来这里是一段自己拼的 SQL：
        #     WHERE agent_id=:aid AND node_count > 0 ORDER BY version DESC LIMIT 1
        # 用 `node_count` 当"有没有图"的判据是错的 —— 那是缓存字段，
        # 库里 published 版本的 node_count 普遍是 0（历史数据没维护），
        # 结果就是**已发布版被筛掉、实际一直在跑草稿**。用户在编排页改了没发布，
        # 聊天里就已经生效了，还以为是发布没生效。
        from app.services.workflow_editor_service import WorkflowEditorService

        async with SessionLocal() as db:
            graph_raw = await WorkflowEditorService(db).load_definition(
                agent_id, use_draft=False
            )
        if not graph_raw:
            push(_status_payload({"type": "error", "message": "该 Agent 还没有可运行的工作流版本"}))
            return None, False, "该 Agent 还没有可运行的工作流版本", []

        graph = WorkflowGraph.parse(graph_raw)
        from app.core.config import settings

        ctx = WorkflowContext(
            agent_id=agent_id,
            user_id=prepared.get("user_id"),
            knowledge_ids=prepared.get("knowledge_ids") or [],
            allow_private_network=settings.workflow_allow_private_network,
        )
        output = None
        suspended = False
        error: str | None = None
        node_events: list[dict[str, object]] = []
        async for ev in WorkflowExecutor(graph, ctx).run({"query": prepared["user_message"]}):
            sse_ev = self._to_sse_event(ev)
            push(_status_payload(sse_ev))
            t = ev.get("type")
            if t == "workflow_done":
                output = ev.get("output")
                error = ev.get("error")
            elif t == "workflow_suspended":
                suspended = True
            if t == "workflow_node_end":
                node_events.append(sse_ev)
        return (str(output) if output is not None else None), suspended, error, node_events

    @staticmethod
    def _to_sse_event(ev: dict) -> dict:
        """工作流事件 → 前端认的事件名。

        实现已提到 `app/workflow/events.py`：编排页的调试流要用同一套翻译，
        复制一份迟早漂移。这里保留包装是为了不让调用点全改。
        """
        return to_sse_event(ev)

    async def _save_assistant(
        self,
        db: AsyncSession,
        session_id: int,
        content: str,
        tokens: int,
        *,
        metadata: dict[str, object] | None,
        tool_events: list[dict] | None = None,
    ) -> int:
        assistant_id = new_id()
        db.add(
            Message(
                id=assistant_id,
                session_id=session_id,
                role="assistant",
                content=content,
                content_type="text",
                message_type="text",
                token_count=tokens,
                msg_metadata=metadata,
                # 顶层 tool_events 列（不是塞进 metadata）—— 前端 parseMessage
                # 明确「顶层优先于 metadata.toolEvents」，新数据都走这条路径。
                tool_events=tool_events,
                create_time=datetime.now(),
            )
        )
        return assistant_id

    async def _save_tool_calls(
        self,
        db: AsyncSession,
        message_id: int,
        logs: list[dict],
    ) -> None:
        """把完整工具结果写进 tool_calls 表。

        为什么不等 flush 后再写：主键是我们自己预生成的雪花 ID，不依赖数据库
        自增，所以可以和 assistant 消息在同一个事务里一次提交，不会出现
        「消息存了但工具记录丢了」的半截状态。

        注意用的是 `ToolCallRecord`（实体）而不是 `ToolCall`（llm_client 里那个
        表示"模型请求调工具"的 dataclass）—— 两者同名但完全不同，混用会在
        INSERT 时抛 TypeError。
        """
        if not logs:
            return
        for log in logs:
            db.add(
                ToolCallRecord(
                    id=log["id"],
                    message_id=message_id,
                    tool_name=log["tool_name"],
                    tool_input=log["tool_input"],
                    tool_output=log["tool_output"],
                    status=log["status"],
                    error_message=log["error_message"],
                    created_at=datetime.now(),
                )
            )

    # ------------------------------------------------------------------
    # 标题生成
    # ------------------------------------------------------------------

    async def _maybe_update_title(self, db: AsyncSession, session_id: int) -> None:
        """对齐 Java `TraceMiddleware.buildFallbackTitle`：**不用 AI**。

        Java 侧的原始考量写在注释里：标题必须是"用户提问的截取"，
        因为模型会偏向概括回答内容，导致标题和用户意图不符。

        规则：首条 user 消息 → 空白压成单空格 → 截断 30 字。
        只在标题还是默认值时改，不覆盖用户手动改过的标题。
        """
        row = (
            await db.execute(text("SELECT title FROM chat_session WHERE id = :sid"), {"sid": session_id})
        ).first()
        current = row[0] if row else None
        if current and current != DEFAULT_TITLE:
            return

        first = (
            await db.execute(
                text(
                    "SELECT content FROM message "
                    "WHERE session_id = :sid AND role = 'user' "
                    "ORDER BY create_time, id LIMIT 1"
                ),
                {"sid": session_id},
            )
        ).first()
        if not first or not first[0]:
            return

        # " ".join(split()) 等价于 Java 的 replaceAll("\\s+", " ").trim()
        title = " ".join(str(first[0]).split())[:TITLE_MAX_LEN]
        if not title:
            return

        await db.execute(
            text(
                "UPDATE chat_session SET title = :title WHERE id = :sid "
                "AND (title IS NULL OR title = '' OR title = :default_title)"
            ),
            {"title": title, "sid": session_id, "default_title": DEFAULT_TITLE},
        )
        logger.info("[Chat] 标题已生成: sessionId=%s title=%r", session_id, title)
