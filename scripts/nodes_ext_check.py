"""外部能力节点验证：api / script / tool + 未知节点必须报错。

用**库里真实的工作流**跑：「示例：外部集成与 MCP 助手」
    start → api(调 jsonplaceholder) → script(解析响应) → condition(状态检查)
         ├─ out_a → mcp(mcp-server-chart)   ← Python 侧未实现，应明确报错
         └─ out_b → variable_err → output_err

预期结果：
    api / script / condition 三个节点成功
    mcp 节点**报错中止**（不是静默跳过！）
    → 若看到 status=success 跑完整条，说明 noop 又回来了，是真 bug

用法：
    python -m scripts.nodes_ext_check          # 跑真实工作流（含预期失败）
    python -m scripts.nodes_ext_check --unit   # 只跑离线单节点
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys

from sqlalchemy import text

from app.core.db import SessionLocal
from app.workflow.executor import WorkflowExecutor
from app.workflow.graph import WorkflowGraph
from app.workflow.nodes import WorkflowContext, get_handler
from app.workflow.nodes_external import validate_url
from app.workflow.state import ExecState

AGENT_WORKFLOW = 2092436112782589954  # agent_version id：示例：外部集成与 MCP 助手
AGENT_WORKFLOW_AGENT = 2092436112774201346  # 上面那个版本所属的 agent id

failures = 0


def ok(label: str, cond: bool, detail: str = "") -> None:
    global failures
    if not cond:
        failures += 1
    print(f"  {'✅' if cond else '❌'} {label}" + (f"  → {detail}" if detail else ""))


# ----------------------------------------------------------------------
# 1. SSRF 防护（纯离线）
# ----------------------------------------------------------------------
def check_ssrf() -> None:
    print("=" * 70)
    print("1. SSRF 防护")
    print("=" * 70)
    blocked = [
        ("http://localhost/x", "localhost"),
        ("http://127.0.0.1/x", "回环"),
        ("http://10.0.0.5/x", "10/8"),
        ("http://172.16.3.4/x", "172.16/12"),
        ("http://192.168.1.1/x", "192.168/16"),
        ("http://169.254.169.254/latest/meta-data/", "云元数据"),
        ("http://0.0.0.0:8080/x", "0.0.0.0"),
        ("ftp://example.com/x", "非 http 协议"),
        ("file:///etc/passwd", "file 协议"),
        ("", "空 URL"),
    ]
    for url, label in blocked:
        err = validate_url(url)
        ok(f"拒绝 {label:<12} {url[:45]}", err is not None, err or "竟然放行了")

    allowed = [
        "https://jsonplaceholder.typicode.com/posts/1",
        "http://example.com/",
    ]
    for url in allowed:
        err = validate_url(url)
        ok(f"放行 {url[:50]}", err is None, err or "")


# ----------------------------------------------------------------------
# 2. script 节点（单元）
# ----------------------------------------------------------------------
async def _start_stub_http() -> tuple[asyncio.AbstractServer, int]:
    """起一个本地假 API，返回 (server, port)。

    为什么不打 jsonplaceholder：那台机器在国内时通时断，测试会随机红。
    而且它一旦不通，api 节点就失败、流程在 api 处中止，**根本走不到 mcp**，
    "未知节点必须报错"这条核心断言就验证不到了。
    本地 stub 让 api → script → condition → mcp 这条链稳定可复现。
    """

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        # 客户端可能不发满 8KB 就等着响应，读超时是常态，不是错误
        with contextlib.suppress(TimeoutError, ConnectionError):
            await asyncio.wait_for(reader.read(8192), timeout=2)
        body = json.dumps(
            {"title": "本地 stub 标题", "body": "alpha beta gamma delta"}, ensure_ascii=False
        ).encode("utf-8")
        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: application/json; charset=utf-8\r\n"
            + f"Content-Length: {len(body)}\r\n".encode()
            + b"Connection: close\r\n\r\n"
            + body
        )
        with contextlib.suppress(ConnectionError):
            await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, int(port)


async def check_api_node() -> None:
    print("\n" + "=" * 70)
    print("2. api 节点（本地 stub，真实 HTTP 请求）")
    print("=" * 70)
    server, port = await _start_stub_http()
    try:
        state = ExecState()
        data = {"url": f"http://127.0.0.1:{port}/posts/1", "method": "GET", "timeout": 5}

        # 默认策略下必须拦住（127.0.0.1 属回环）
        try:
            await get_handler("api")(data, ExecState(), WorkflowContext())
            ok("默认策略拒绝回环地址", False, "竟然放行了")
        except ValueError as exc:
            ok("默认策略拒绝回环地址", "内网/保留" in str(exc) or "回环" in str(exc), str(exc)[:60])

        # 打开开关后放行（自托管调内网服务走这条）
        await get_handler("api")(data, state, WorkflowContext(allow_private_network=True))
        ok("allow_private_network=True 时放行", state.variables.get("statusCode") == 200,
           str(state.variables.get("statusCode")))
        body = state.variables.get("body") or ""
        ok("body 是原始字符串（非自动解析）", isinstance(body, str), type(body).__name__)
        ok("body 内容正确", "本地 stub 标题" in body, body[:60])
        ok("result 等于 body（兼容老工作流）",
           state.variables.get("result") == body)

        # 不存在的端口 → 明确抛错而不是静默返回空
        try:
            await get_handler("api")(
                {"url": "http://127.0.0.1:1/x", "timeout": 2},
                ExecState(),
                WorkflowContext(allow_private_network=True),
            )
            ok("连接失败时抛错", False, "竟然没抛")
        except ValueError as exc:
            ok("连接失败时抛错", "HTTP 请求失败" in str(exc) or "超时" in str(exc), str(exc)[:70])
    finally:
        server.close()
        await server.wait_closed()


async def check_script_node() -> None:
    print("\n" + "=" * 70)
    print("3. script 节点（javascript，真实执行）")
    print("=" * 70)
    data = {
        "scriptLanguage": "javascript",
        "scriptContent": (
            "function main(params) {\n"
            "  var body = JSON.parse(params.body || '{}');\n"
            "  return { title: body.title, n: (body.body || '').split(/\\s+/).length };\n"
            "}"
        ),
        "inputParams": [
            {"key": "body", "value": "{{body}}"},
            {"key": "statusCode", "value": "{{statusCode}}"},
        ],
    }
    state = ExecState()
    state.set("body", json.dumps({"title": "T1", "body": "a b c d"}))
    state.set("statusCode", 200)
    await get_handler("script")(data, state, WorkflowContext())
    ok("返回 dict 被平铺进变量池", state.variables.get("title") == "T1",
       str(state.variables.get("title")))
    ok("统计字符数正确", state.variables.get("n") == 4, str(state.variables.get("n")))

    # 失败策略
    bad = {
        "scriptLanguage": "python",
        "scriptContent": "def main(params):\n    return 1/0",
        "errorStrategy": "defaultValue",
        "defaultOutput": '{"title": "降级值"}',
    }
    state2 = ExecState()
    await get_handler("script")(bad, state2, WorkflowContext())
    ok("errorStrategy=defaultValue 时降级成功", state2.variables.get("title") == "降级值",
       str(state2.variables))

    raise_state = ExecState()
    try:
        await get_handler("script")(
            {"scriptLanguage": "python", "scriptContent": "def main(p):\n    return 1/0"},
            raise_state,
            WorkflowContext(),
        )
        ok("无 errorStrategy 时抛异常", False, "竟然没抛")
    except ValueError as exc:
        ok("无 errorStrategy 时抛异常", "脚本执行失败" in str(exc))


# ----------------------------------------------------------------------
# 3. tool 节点（单元）
# ----------------------------------------------------------------------
async def check_tool_node() -> None:
    print("\n" + "=" * 70)
    print("4. tool 节点（calculator）")
    print("=" * 70)
    data = {
        "toolName": "calculator",
        "inputParams": [
            {"key": "a", "value": "{{x}}"},
            {"key": "b", "value": "8"},
            {"key": "operation", "value": "multiply"},
        ],
    }
    state = ExecState()
    state.set("x", 6)
    await get_handler("tool")(data, state, WorkflowContext())
    ok("toolOk=true", state.variables.get("toolOk") is True, str(state.variables.get("toolOk")))
    tr = state.variables.get("toolResult")
    ok("结果平铺（result=48）", isinstance(tr, dict) and tr.get("result") == "48",
       json.dumps(tr, ensure_ascii=False)[:120] if tr else "None")

    # 不存在的工具必须报错
    try:
        await get_handler("tool")({"toolName": "no_such_tool"}, ExecState(), WorkflowContext())
        ok("未注册工具报错", False, "竟然没抛")
    except ValueError as exc:
        ok("未注册工具报错", "未注册" in str(exc), str(exc)[:80])


# ----------------------------------------------------------------------
# 4. 真实工作流（api → script → condition → mcp）
# ----------------------------------------------------------------------
async def check_real_workflow() -> None:
    print("\n" + "=" * 70)
    print("5. 真实工作流：外部集成与 MCP 助手")
    print("=" * 70)

    async with SessionLocal() as db:
        row = (
            await db.execute(
                text(
                    "SELECT av.config FROM agent_version av "
                    "WHERE av.id = :vid AND av.deleted = 0"
                ),
                {"vid": AGENT_WORKFLOW},
            )
        ).first()
    if row is None:
        ok("找到工作流版本", False, f"agent_version {AGENT_WORKFLOW} 不存在")
        return

    graph = WorkflowGraph.parse((row[0] or {}).get("graph") or {})
    print(f"  {len(graph.nodes)} 个节点")

    # 把图里 api 节点的 url 指向本地 stub —— 不改库里的配置，只改内存副本。
    # 原因：原来的 https://jsonplaceholder.typicode.com 在国内时通时断，
    # 一旦不通，流程在 api 就中止，walk 不到 mcp，"未知节点必须报错"验不到。
    server, port = await _start_stub_http()
    patched = 0
    for node in graph.nodes.values():
        if str(node.get("type") or "") == "api":
            data = node.setdefault("data", {})
            data["url"] = f"http://127.0.0.1:{port}/posts/1"
            patched += 1
    print(f"  已把 {patched} 个 api 节点的 url 改指向本地 stub (127.0.0.1:{port})")

    executor = WorkflowExecutor(graph, WorkflowContext(allow_private_network=True))
    seen: dict[str, dict] = {}
    done: dict = {}

    try:
        async for ev in executor.run({"query": "跑一下"}):
            t = ev.get("type")
            if t == "workflow_node_start":
                print(f"    ▶ {ev['nodeLabel']:<14} ({ev['nodeType']})")
            elif t == "workflow_node_end":
                seen[ev["nodeType"]] = ev
                mark = "✅" if ev.get("status") == "success" else "❌"
                print(f"    {mark} {ev['nodeLabel']:<14} handle={ev.get('handle')}")
                if ev.get("status") == "error":
                    print(f"       错误: {str(ev.get('message'))[:150]}")
            elif t == "workflow_done":
                done = ev
    finally:
        server.close()
        await server.wait_closed()

    api_ev = seen.get("api") or {}
    ok("api 节点成功", api_ev.get("status") == "success")
    ok("api 输出了 statusCode/body", "statusCode" in str(api_ev.get("output")),
       str(api_ev.get("output"))[:100])

    script_ev = seen.get("script") or {}
    ok("script 节点成功", script_ev.get("status") == "success")
    ok("script 输出了 title", "title" in str(script_ev.get("output")),
       str(script_ev.get("output"))[:120])

    cond_ev = seen.get("condition") or {}
    ok("condition 走 out_a（状态 200）", cond_ev.get("handle") == "out_a",
       str(cond_ev.get("handle")))

    # 核心断言：mcp 未实现 → 必须报错中止，**绝不能** success
    mcp_ev = seen.get("mcp") or {}
    ok("mcp 节点明确报错（不是静默跳过）", mcp_ev.get("status") == "error",
       f"status={mcp_ev.get('status')}")
    ok("错误信息说明了未实现", "尚未" in str(mcp_ev.get("message")),
       str(mcp_ev.get("message"))[:120])
    ok("整条流程中止且带 error", bool(done.get("error")), str(done.get("error"))[:80])
    ok("流程**没有**跑到 output 节点（中止生效）", "output" not in seen,
       f"实际执行了 {list(seen)}")


async def check_failure_persisted() -> None:
    """第 6 节：失败要能被用户看见 —— 走真实 chat 链路跑一次。

    为什么要这一节：节点报错了，但如果失败信息只存在于这一秒的 SSE 帧里，
    用户刷新页面就只剩自己的提问（没有回答、也没有"哪一步失败了"），
    等于报错白报了。这里验证失败消息确实落了库、metadata 里带得动前端。

    ⚠️ 两个代价，所以默认不跑（要 `--with-chat`）：
      1. **写库**：建一条会话 + 两条消息（副本库，可接受）
      2. **走公网**：这条链路读的是**库里的原始 graph**，api 节点指向
         jsonplaceholder。国内网络不稳时会超时 30 秒才中止 —— 慢，而且
         失败节点会变成 api 而不是 mcp（第 5 节已用本地 stub 验过 mcp）。
         所以这里**不绑定具体失败节点**，只验"失败被持久化"这个机制本身。
    """
    print("\n" + "=" * 70)
    print("6. 失败落库：刷新后前端仍能定位失败节点（走真实 chat 链路）")
    print("=" * 70)

    from app.core.config import settings
    from app.repositories.chat_session_repo import ChatSessionRepository
    from app.schemas.chat import ChatRequestIn
    from app.services.chat_session_service import ChatSessionService
    from app.services.chat_stream_service import ChatStreamService

    svc = ChatStreamService()
    done: dict = {}
    async for frame in svc.stream(
        ChatRequestIn(message="跑一次外部 API 流程", agentId=str(AGENT_WORKFLOW_AGENT)),
        settings.dev_user_id,
    ):
        payload = frame.split("data:", 1)[1] if "data:" in frame else frame
        if payload.startswith("[DONE]"):
            done = json.loads(payload[6:] or "{}")

    ok("DONE 帧带 workflowError", bool(done.get("workflowError")),
       str(done.get("workflowError"))[:80])

    uid = done.get("userMessageId")
    if not uid:
        ok("拿到 userMessageId", False)
        return

    async with SessionLocal() as db:
        row = (
            await db.execute(
                text(
                    "SELECT m.id, m.session_id, m.content, m.metadata FROM message m "
                    "WHERE m.session_id = (SELECT session_id FROM message WHERE id = :uid) "
                    "AND m.role = 'assistant' ORDER BY m.id DESC LIMIT 1"
                ),
                {"uid": int(uid)},
            )
        ).mappings().first()
        ok("失败也落了 assistant 消息", row is not None)
        if row is None:
            return

        meta = row["metadata"] or {}
        ok("metadata.workflowError 存在", bool(meta.get("workflowError")),
           str(meta.get("workflowError"))[:80])
        events = meta.get("workflowEvents") or []
        ok("metadata.workflowEvents 非空", len(events) > 0, f"{len(events)} 条")
        failure = next(
            (e for e in reversed(events)
             if e.get("type") == "workflow_node_complete" and e.get("success") is False),
            None,
        )
        ok("事件里有 success=false 的节点", failure is not None,
           str(failure.get("nodeLabel")) if failure else "无")
        # 不绑定 mcp：公网不通时会是 api。只要"某个节点失败并被记录下来"即算通过。
        ok("失败节点是 api 或 mcp（任一）",
           (failure or {}).get("nodeType") in ("api", "mcp"),
           str((failure or {}).get("nodeType")))

        # 走一遍 list_messages，模拟前端刷新
        s = ChatSessionService(ChatSessionRepository(db))
        page = await s.list_messages(row["session_id"], settings.dev_user_id, 1, 3)
        target = next((m for m in page.records if str(m.id) == str(row["id"])), None)
        ok("刷新后（list_messages）仍能拿到这条消息", target is not None)
        if target:
            m2 = target.metadata or {}
            ok("刷新后仍能读到失败原因", bool((m2.get("workflowError") or {}).get("message")))


async def main() -> None:
    unit_only = "--unit" in sys.argv
    with_chat = "--with-chat" in sys.argv
    check_ssrf()
    await check_api_node()
    await check_script_node()
    await check_tool_node()
    if not unit_only:
        await check_real_workflow()
    if with_chat:
        await check_failure_persisted()
    elif not unit_only:
        print("\n- 跳过「失败落库」（加 --with-chat 才跑：写库 + 依赖公网，约 30s）")

    print()
    if failures:
        print(f"❌ 失败 {failures} 项")
        raise SystemExit(1)
    print("✅ 全部通过")


if __name__ == "__main__":
    asyncio.run(main())
