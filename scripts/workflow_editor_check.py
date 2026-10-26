"""工作流编排 16 接口验证：草稿 / 发布 / 校验 / 版本 / 调试 / 记录。

分两部分：
- **离线**：`validate_graph` 的 12 条规则（纯函数，不用库）。这是最容易写错的地方，
  比如"条件分支缺默认路径"不报错的话，运行时会卡在条件节点一直转圈。
- **在线**：走真实 HTTP（httpx + ASGITransport），含 SSE 流。

⚠️ 会写库（建一个 `zz-自检工作流` agent + 版本 + 调试记录），跑完物理删除。
⚠️ 需要 `dangerouslyDisableSandbox`（沙箱挡远程库）。

用法：
    python -m scripts.workflow_editor_check           # 离线 + 在线
    python -m scripts.workflow_editor_check --offline # 只跑离线规则
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

import httpx
from sqlalchemy import text

from app.core.db import SessionLocal
from app.main import app
from app.services.workflow_editor_service import build_io_schema, validate_graph

BASE = "http://test"
PREFIX = "zz-自检工作流"
failures = 0


def ok(label: str, cond: bool, detail: str = "") -> None:
    global failures
    if not cond:
        failures += 1
    print(f"  {'✅' if cond else '❌'} {label}" + (f"  → {detail}" if detail else ""))


# ----------------------------------------------------------------------
# 测试图：start → variable → output → end（纯本地，不碰网络）
# ----------------------------------------------------------------------
def good_graph(output_expr: str = "{{greeting}}") -> dict[str, Any]:
    return {
        "nodes": [
            {"id": "start_1", "type": "start", "position": {"x": 0, "y": 0},
             "data": {"label": "开始"}},
            {"id": "var_1", "type": "variable", "position": {"x": 200, "y": 0},
             "data": {"label": "拼装问候", "variableName": "greeting",
                      "variableValue": "{{query}}-已处理"}},
            {"id": "out_1", "type": "output", "position": {"x": 400, "y": 0},
             "data": {"label": "输出", "output": output_expr}},
            {"id": "end_1", "type": "end", "position": {"x": 600, "y": 0},
             "data": {"label": "结束"}},
        ],
        "edges": [
            {"id": "e1", "source": "start_1", "target": "var_1",
             "sourceHandle": "out", "targetHandle": "in"},
            {"id": "e2", "source": "var_1", "target": "out_1",
             "sourceHandle": "out", "targetHandle": "in"},
            {"id": "e3", "source": "out_1", "target": "end_1",
             "sourceHandle": "out", "targetHandle": "in"},
        ],
        "publishDescription": "自检发布",
    }


# ----------------------------------------------------------------------
# 1. 离线：校验规则
# ----------------------------------------------------------------------
def check_offline() -> None:
    print("=" * 70)
    print("1. validate_graph —— 12 条规则")
    print("=" * 70)

    ok("合格图无错误", validate_graph(good_graph()) == [], str(validate_graph(good_graph())))
    ok("空图报错", validate_graph({}) == ["工作流节点为空"])
    ok("None 报错", validate_graph(None) == ["工作流节点为空"])

    # 缺 start / end
    g = good_graph()
    g["nodes"] = [n for n in g["nodes"] if n["type"] != "start"]
    errs = validate_graph(g)
    ok("缺开始节点", any("缺少开始节点" in e for e in errs), str(errs))

    g = good_graph()
    g["nodes"].append({"id": "start_2", "type": "start", "data": {}})
    errs = validate_graph(g)
    ok("两个开始节点", any("只能有一个开始节点" in e for e in errs), str(errs))

    g = good_graph()
    g["nodes"] = [n for n in g["nodes"] if n["type"] != "end"]
    g["edges"] = [e for e in g["edges"] if e["target"] != "end_1"]
    errs = validate_graph(g)
    ok("缺结束节点", any("缺少结束节点" in e for e in errs), str(errs))

    # 未连接
    g = good_graph()
    g["nodes"].append({"id": "orphan", "type": "variable", "data": {}})
    errs = validate_graph(g)
    ok("孤立节点报未连接", any("节点未连接: orphan" in e for e in errs), str(errs))

    # LLM 缺配置
    g = good_graph()
    g["nodes"].insert(1, {"id": "llm_1", "type": "llm", "data": {"label": "问答"}})
    g["edges"].insert(0, {"source": "start_1", "target": "llm_1", "sourceHandle": "out"})
    g["edges"].insert(1, {"source": "llm_1", "target": "var_1", "sourceHandle": "out"})
    errs = validate_graph(g)
    ok("LLM 缺 providerId", any("LLM节点未选择提供商: llm_1" in e for e in errs), str(errs))
    ok("LLM 缺 modelId", any("LLM节点未选择模型: llm_1" in e for e in errs), str(errs))

    # retrieval / tool
    g = good_graph()
    g["nodes"].insert(1, {"id": "ret_1", "type": "retrieval", "data": {}})
    g["edges"].insert(0, {"source": "start_1", "target": "ret_1", "sourceHandle": "out"})
    g["edges"].insert(1, {"source": "ret_1", "target": "var_1", "sourceHandle": "out"})
    errs = validate_graph(g)
    ok("检索缺 knowledgeId", any("知识检索节点未选择知识库" in e for e in errs), str(errs))

    g = good_graph()
    g["nodes"].insert(1, {"id": "tool_1", "type": "tool", "data": {}})
    g["edges"].insert(0, {"source": "start_1", "target": "tool_1", "sourceHandle": "out"})
    g["edges"].insert(1, {"source": "tool_1", "target": "var_1", "sourceHandle": "out"})
    errs = validate_graph(g)
    ok("工具缺 toolId", any("工具节点未选择工具" in e for e in errs), str(errs))

    # condition 缺默认路径
    g = good_graph()
    g["nodes"].insert(1, {"id": "cond_1", "type": "condition", "data": {"label": "判断"}})
    g["edges"] = [
        {"source": "start_1", "target": "cond_1", "sourceHandle": "out"},
        {"source": "cond_1", "target": "var_1", "sourceHandle": "out_a"},
    ]
    errs = validate_graph(g)
    ok("条件分支缺 out_c 默认路径",
       any("条件分支节点缺少默认路径: cond_1" in e for e in errs), str(errs))

    # 多出边
    g = good_graph()
    g["nodes"].append({"id": "var_2", "type": "variable", "data": {"label": "分支甲"}})
    g["edges"].append({"source": "var_1", "target": "var_2", "sourceHandle": "out2"})
    g["edges"].append({"source": "var_2", "target": "out_1", "sourceHandle": "out"})
    errs = validate_graph(g)
    ok("非分支节点多出边报错", any("有 2 条出边" in e for e in errs), str(errs))

    # condition 多出边允许
    g = good_graph()
    g["nodes"].insert(1, {"id": "cond_2", "type": "condition", "data": {"label": "判断"}})
    g["nodes"].append({"id": "var_b", "type": "variable", "data": {"label": "分支乙"}})
    g["edges"] = [
        {"source": "start_1", "target": "cond_2", "sourceHandle": "out"},
        {"source": "cond_2", "target": "var_1", "sourceHandle": "out_a"},
        {"source": "cond_2", "target": "var_b", "sourceHandle": "out_b"},
        {"source": "cond_2", "target": "out_1", "sourceHandle": "out_c"},
        {"source": "var_1", "target": "out_1", "sourceHandle": "out"},
        {"source": "var_b", "target": "out_1", "sourceHandle": "out2"},
    ]
    g["nodes"] = [n for n in g["nodes"] if n["id"] != "end_1"]
    g["edges"] = [e for e in g["edges"] if e["target"] != "end_1" and e["source"] != "out_1"]
    errs = validate_graph(g)
    ok("条件分支多出边允许", not any("条出边" in e for e in errs), str(errs))

    # 环路
    g = good_graph()
    g["edges"].append({"source": "var_1", "target": "start_1", "sourceHandle": "back"})
    errs = validate_graph(g)
    ok("环路被检出", any("工作流存在环路" in e for e in errs), str(errs))
    ok("环路信息带路径（能定位）", any("→" in e for e in errs if "环路" in e), str(errs))

    # 未知类型 / 未实现类型
    g = good_graph()
    g["nodes"].insert(1, {"id": "weird", "type": "teleport", "data": {}})
    g["edges"].insert(0, {"source": "start_1", "target": "weird", "sourceHandle": "out"})
    g["edges"].insert(1, {"source": "weird", "target": "var_1", "sourceHandle": "out"})
    errs = validate_graph(g)
    ok("未知类型报错", any("未知节点类型: teleport" in e for e in errs), str(errs))

    g = good_graph()
    g["nodes"].insert(1, {"id": "mcp_1", "type": "mcp", "data": {"label": "MCP"}})
    g["edges"].insert(0, {"source": "start_1", "target": "mcp_1", "sourceHandle": "out"})
    g["edges"].insert(1, {"source": "mcp_1", "target": "var_1", "sourceHandle": "out"})
    errs = validate_graph(g)
    ok("合法但未实现的类型明确报错（不装没事）",
       any("尚未在 Python 版实现" in e for e in errs), str(errs))

    print("\n" + "=" * 70)
    print("2. build_io_schema")
    print("=" * 70)

    schema = build_io_schema(good_graph())
    keys = [i["key"] for i in schema["inputs"]]
    ok("默认带 query/input", keys == ["query", "input"], str(keys))
    ok("outputs 只有 result", [o["key"] for o in schema["outputs"]] == ["result"])

    g = good_graph()
    g["nodes"].append(
        {"id": "in_1", "type": "input", "data":
         {"outputParams": [{"key": "city"}, {"key": "days"}, {"key": "city"}]}}
    )
    keys = [i["key"] for i in build_io_schema(g)["inputs"]]
    ok("input 节点参数被收集", keys == ["query", "input", "city", "days"], str(keys))
    ok("重复 key 去重", keys.count("city") == 1)

    g = good_graph()
    g["nodes"].append(
        {"id": "in_2", "type": "input", "data": {"output_params": [{"key": "topic"}]}}
    )
    keys = [i["key"] for i in build_io_schema(g)["inputs"]]
    ok("下划线写法 output_params 也认", "topic" in keys, str(keys))


# ----------------------------------------------------------------------
# 在线
# ----------------------------------------------------------------------
async def cleanup() -> None:
    async with SessionLocal() as db:
        ids = [
            r[0]
            for r in (
                await db.execute(
                    text("SELECT id FROM agent WHERE name LIKE :p"), {"p": f"{PREFIX}%"}
                )
            ).all()
        ]
        for aid in ids:
            await db.execute(text("DELETE FROM workflow_test_run WHERE agent_id = :a"), {"a": aid})
            await db.execute(text("DELETE FROM agent_version WHERE agent_id = :a"), {"a": aid})
            await db.execute(text("DELETE FROM agent WHERE id = :a"), {"a": aid})
        await db.commit()


async def api(c: httpx.AsyncClient, method: str, path: str, **kw) -> dict:
    return (await c.request(method, BASE + path, **kw)).json()


async def check_online() -> None:
    await cleanup()

    print("\n" + "=" * 70)
    print("3. 建测试 Agent + 编辑器状态")
    print("=" * 70)

    async with SessionLocal() as db:
        from app.core.config import settings

        user_id = settings.dev_user_id

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=BASE, timeout=60) as c:
        created = (await api(c, "POST", "/api/agents", json={
            "name": PREFIX,
            "description": "编排接口自检",
            "agentType": "workflow",
            "config": "{}",
        }))["data"]
        aid = created["id"]
        ok("测试 Agent 创建成功", bool(aid), str(aid))
        base = f"/api/agents/{aid}/workflow"

        state = (await api(c, "GET", base))["data"]
        ok("初始状态是 draft", state["status"] == "draft", str(state["status"]))
        ok("初始无发布版", state["published"] is None, str(state["published"]))
        ok("初始 publishedVersion=0", state["publishedVersion"] == 0, str(state["publishedVersion"]))
        ok("初始 draft 为空", state["draft"] is None, str(state["draft"]))

        print("\n" + "=" * 70)
        print("4. 草稿 / 校验 / 发布")
        print("=" * 70)

        r = await api(c, "POST", f"{base}/draft", json=good_graph())
        ok("存草稿成功", r.get("code") == 200, str(r.get("message")))

        state = (await api(c, "GET", base))["data"]
        ok("草稿能读回", state["draft"] is not None
           and len(state["draft"]["nodes"]) == 4, str(len((state["draft"] or {}).get("nodes", []))))
        ok("草稿带 draftVersionId", bool(state.get("draftVersionId")), str(state.get("draftVersionId")))
        ok("节点 position 被保留（画布布局不丢）",
           "position" in state["draft"]["nodes"][0], str(state["draft"]["nodes"][0]))

        # 草稿不校验：坏图也能存
        bad = good_graph()
        bad["nodes"] = [n for n in bad["nodes"] if n["type"] != "start"]
        r = await api(c, "POST", f"{base}/draft", json=bad)
        ok("坏图也能存草稿（不校验）", r.get("code") == 200, str(r.get("message")))

        r = await api(c, "POST", f"{base}/validate", json=bad)
        ok("validate 返回错误数组", r.get("code") == 200 and len(r["data"]) > 0, str(r.get("data")))

        r = await api(c, "POST", f"{base}/validate", json=good_graph())
        ok("合格图 validate 返回空数组", r.get("code") == 200 and r["data"] == [], str(r.get("data")))

        r = await api(c, "POST", f"{base}/publish", json=bad)
        ok("坏图发布被拒（400）", r.get("code") == 400, str(r.get("message"))[:80])

        r = await api(c, "POST", f"{base}/publish", json=good_graph())
        ok("合格图发布成功", r.get("code") == 200 and r["data"]["version"] == 1, str(r.get("data")))

        state = (await api(c, "GET", base))["data"]
        ok("状态变 published", state["status"] == "published", str(state["status"]))
        ok("publishedVersion=1", state["publishedVersion"] == 1, str(state["publishedVersion"]))
        ok("published 图能读回", state["published"] is not None, str(state["published"]))

        # 发布后再改草稿 → published_editing
        changed = good_graph("改过的输出")
        await api(c, "POST", f"{base}/draft", json=changed)
        state = (await api(c, "GET", base))["data"]
        ok("改草稿后状态变 published_editing",
           state["status"] == "published_editing", str(state["status"]))
        ok("发布版没被改动", state["publishedVersion"] == 1, str(state["publishedVersion"]))

        print("\n" + "=" * 70)
        print("5. 版本")
        print("=" * 70)

        versions = (await api(c, "GET", f"{base}/versions"))["data"]
        ok("版本列表 1 条", len(versions) == 1, f"{len(versions)} 条")
        v0 = versions[0]
        ok("版本号 1", v0["version"] == 1, str(v0["version"]))
        ok("current=true", v0["current"] is True, str(v0["current"]))
        ok("id 是字符串", isinstance(v0["id"], str), type(v0["id"]).__name__)
        ok("nodeCount=4", v0["nodeCount"] == 4, str(v0["nodeCount"]))
        ok("有 draftVersionId（前端匹配草稿用）", bool(v0.get("draftVersionId")))
        ok("发布说明落库", v0["description"] == "自检发布", str(v0["description"]))

        vg = (await api(c, "GET", f"{base}/versions/1"))["data"]
        ok("版本画布能读", len(vg.get("nodes") or []) == 4, str(len(vg.get("nodes") or [])))

        r = await api(c, "POST", f"{base}/versions/1/restore")
        ok("恢复到 v1 成功", r.get("code") == 200, str(r.get("message")))
        state = (await api(c, "GET", base))["data"]
        out_node = next(n for n in state["draft"]["nodes"] if n["type"] == "output")
        ok("草稿被恢复成 v1 的内容",
           out_node["data"]["output"] == "{{greeting}}", str(out_node["data"]["output"]))

        r = await api(c, "GET", f"{base}/versions/99")
        ok("不存在的版本 → 404", r.get("code") == 404, str(r.get("code")))

        print("\n" + "=" * 70)
        print("6. IO Schema")
        print("=" * 70)

        io = (await api(c, "GET", f"{base}/io-schema"))["data"]
        ok("io-schema 有 inputs/outputs", {"inputs", "outputs"} <= set(io), str(list(io)))
        ok("agentId 是字符串", isinstance(io["agentId"], str), type(io["agentId"]).__name__)
        ok("publishedVersion=1", io["publishedVersion"] == 1, str(io["publishedVersion"]))
        ok("inputs 含 query", any(i["key"] == "query" for i in io["inputs"]),
           str([i["key"] for i in io["inputs"]]))

        print("\n" + "=" * 70)
        print("7. 调试运行（REST）")
        print("=" * 70)

        res = (await api(c, "POST", f"{base}/test", json={
            "input": "你好", "useDraft": True, "testMode": "generation",
        }))["data"]
        ok("test 返回 output", res["output"] == "你好-已处理", repr(res["output"]))
        ok("test 返回 nodeEvents", len(res["nodeEvents"]) >= 3, f"{len(res['nodeEvents'])} 个")
        ok("nodeEvents 用了前端事件名",
           any(e.get("type") == "workflow_node_complete" for e in res["nodeEvents"]),
           str([e.get("type") for e in res["nodeEvents"]][:4]))
        ok("node_complete 带 success", all("success" in e for e in res["nodeEvents"]
                                        if e.get("type") == "workflow_node_complete"))
        ok("node_complete 带 durationMs（节点卡片显示耗时）",
           all("durationMs" in e for e in res["nodeEvents"]
               if e.get("type") == "workflow_node_complete"),
           str([e.get("durationMs") for e in res["nodeEvents"]
                if e.get("type") == "workflow_node_complete"]))
        ok("返回 runId", bool(res.get("runId")), str(res.get("runId")))
        ok("返回 variables", res["variables"].get("greeting") == "你好-已处理",
           str(res["variables"].get("greeting")))
        ok("usedDraft=true", res["usedDraft"] is True, str(res["usedDraft"]))

        # 用请求体里的图覆盖库里的（画布没保存也能调）
        res2 = (await api(c, "POST", f"{base}/test", json={
            "input": "草稿", "graph": good_graph("直接覆盖输出"),
        }))["data"]
        ok("传 graph 时优先用请求体", res2["output"] == "直接覆盖输出", repr(res2["output"]))

        print("\n" + "=" * 70)
        print("8. 调试运行（SSE）")
        print("=" * 70)

        frames: list[dict] = []
        done_meta: dict = {}
        raw_lines: list[str] = []
        async with c.stream("POST", f"{base}/test/stream", json={
            "input": "流式", "useDraft": True,
        }) as resp:
            ok("SSE 状态码 200", resp.status_code == 200, str(resp.status_code))
            ok("Content-Type 是 event-stream",
               "text/event-stream" in resp.headers.get("content-type", ""),
               resp.headers.get("content-type", ""))
            async for chunk in resp.aiter_text():
                for line in chunk.split("\n"):
                    if not line.strip():
                        continue
                    raw_lines.append(line)
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:]
                    if payload.startswith("[DONE]"):
                        done_meta = json.loads(payload[6:] or "{}")
                    elif payload.startswith("[STATUS]"):
                        frames.append(json.loads(payload[8:]))

        ok("所有帧都带 data: 前缀（前端按行解析）",
           all(x.startswith("data:") for x in raw_lines),
           str([x[:20] for x in raw_lines if not x.startswith("data:")][:3]))
        ok("收到 workflow_node_start", any(f.get("type") == "workflow_node_start" for f in frames),
           str([f.get("type") for f in frames][:6]))
        ok("收到 workflow_node_complete",
           any(f.get("type") == "workflow_node_complete" for f in frames))
        ok("收到 workflow_complete", any(f.get("type") == "workflow_complete" for f in frames))
        ok("节点数量正确（start/var/output 三个）",
           sum(1 for f in frames if f.get("type") == "workflow_node_complete") == 3,
           str(sum(1 for f in frames if f.get("type") == "workflow_node_complete")))
        ok("[DONE] 带最终输出", done_meta.get("output") == "流式-已处理", repr(done_meta.get("output")))
        ok("[DONE] 带 runId", bool(done_meta.get("runId")))
        ok("事件里带 runId（便于对账）",
           all(f.get("runId") for f in frames if f.get("type") != "error"))

        print("\n" + "=" * 70)
        print("9. 单节点调试")
        print("=" * 70)

        node_res = (await api(c, "POST", f"{base}/test-node", json={
            "nodeId": "var_1", "inputParams": {"query": "单节点"},
        }))["data"]
        ok("单节点返回 output", "单节点-已处理" in str(node_res["output"]), repr(node_res["output"]))
        ok("单节点返回 nodeEvents", len(node_res["nodeEvents"]) == 1, str(len(node_res["nodeEvents"])))
        ok("单节点事件带 success", node_res["nodeEvents"][0]["success"] is True,
           str(node_res["nodeEvents"][0]))
        ok("单节点把变量写进 variables",
           node_res["variables"].get("greeting") == "单节点-已处理",
           str(node_res["variables"].get("greeting")))

        r = await api(c, "POST", f"{base}/test-node", json={"nodeId": "no_such_node"})
        ok("不存在的节点 → 404", r.get("code") == 404, str(r.get("code")))

        # 失败节点：llm 没配 provider → 结构化失败，不是 500
        bad_graph = good_graph()
        bad_graph["nodes"].insert(1, {"id": "llm_x", "type": "llm",
                                      "data": {"label": "空模型", "promptTemplate": "hi"}})
        r = await api(c, "POST", f"{base}/test-node", json={
            "nodeId": "llm_x", "graph": bad_graph, "inputParams": {},
        })
        ok("单节点失败不抛 500（返回结构化结果）", r.get("code") == 200, str(r.get("code")))
        ok("失败事件 success=false", r["data"]["nodeEvents"][0]["success"] is False,
           str(r["data"]["nodeEvents"][0].get("success")))

        print("\n" + "=" * 70)
        print("10. 调试记录")
        print("=" * 70)

        runs = (await api(c, "GET", f"{base}/test-runs"))["data"]
        ok("记录列表非空", len(runs) >= 3, f"{len(runs)} 条")
        r0 = runs[0]
        ok("列表字段齐全",
           {"runId", "status", "testMode", "usedDraft", "userInputSummary",
            "outputSummary", "durationMs", "startTime"} <= set(r0), str(list(r0)))
        ok("状态是 completed", any(x["status"] == "completed" for x in runs),
           str([x["status"] for x in runs]))
        ok("summary 截断到 120 字内",
           all(len(x.get("userInputSummary") or "") <= 121 for x in runs))
        ok("usedDraft 是布尔", isinstance(r0["usedDraft"], bool), type(r0["usedDraft"]).__name__)

        run_id = r0["runId"]
        detail = (await api(c, "GET", f"{base}/test-runs/{run_id}"))["data"]
        ok("详情有 nodeEvents", len(detail["nodeEvents"]) > 0, str(len(detail["nodeEvents"])))
        ok("详情有 workflowGraph（可回放）",
           len((detail.get("workflowGraph") or {}).get("nodes") or []) > 0)
        ok("详情有 output", bool(detail.get("output")), repr(detail.get("output"))[:60])

        r = await api(c, "DELETE", f"{base}/test-runs/{run_id}")
        ok("删除单条成功", r.get("code") == 200, str(r.get("message")))
        r = await api(c, "GET", f"{base}/test-runs/{run_id}")
        ok("删除后 404", r.get("code") == 404, str(r.get("code")))

        # 保留上限 50
        async with SessionLocal() as db:
            await db.execute(text(
                "INSERT INTO workflow_test_run "
                "(id, run_id, agent_id, user_id, test_mode, used_draft, status, node_events, "
                " user_input, start_time, create_time, update_time, deleted) "
                "SELECT 900000000000000000 + g, 'zz-seed-' || g, :aid, :uid, 'generation', 1, "
                "       'completed', '[]'::jsonb, 'seed', "
                "       now() - (g || ' minutes')::interval, now(), now(), 0 "
                "FROM generate_series(1, 52) g"
            ), {"aid": int(aid), "uid": int(user_id)})
            await db.commit()

        # 再跑一次 → 触发 trim
        await api(c, "POST", f"{base}/test", json={"input": "触发清理"})
        runs2 = (await api(c, "GET", f"{base}/test-runs"))["data"]
        ok("保留上限 50 条（超出删最旧的）", len(runs2) == 50, f"{len(runs2)} 条")

        r = await api(c, "DELETE", f"{base}/test-runs")
        ok("清空成功", r.get("code") == 200)
        runs3 = (await api(c, "GET", f"{base}/test-runs"))["data"]
        ok("清空后为空", runs3 == [], str(len(runs3)))

        print("\n" + "=" * 70)
        print("11. 运行时取图：优先发布版（修掉的旧 bug）")
        print("=" * 70)

        async with SessionLocal() as db:
            from app.services.workflow_editor_service import WorkflowEditorService

            svc = WorkflowEditorService(db)
            runtime_graph = await svc.load_definition(int(aid), use_draft=False)
        out_node = next(n for n in runtime_graph["nodes"] if n["type"] == "output")
        ok("chat 取到的是**发布版**而不是草稿",
           out_node["data"]["output"] == "{{greeting}}", str(out_node["data"]["output"]))

        # 再发一版，让 version=2
        await api(c, "POST", f"{base}/publish", json=good_graph("v2 输出"))
        versions = (await api(c, "GET", f"{base}/versions"))["data"]
        ok("第二次发布 version=2", versions[0]["version"] == 2, str(versions[0]["version"]))
        ok("只有一条 current", sum(1 for v in versions if v["current"]) == 1,
           str([(v["version"], v["current"]) for v in versions]))

    await cleanup()
    print("\n已清理测试数据")


async def main() -> None:
    offline_only = "--offline" in sys.argv
    check_offline()
    if not offline_only:
        await check_online()

    print()
    if failures:
        print(f"❌ 失败 {failures} 项")
        raise SystemExit(1)
    print("✅ 全部通过")


if __name__ == "__main__":
    asyncio.run(main())
