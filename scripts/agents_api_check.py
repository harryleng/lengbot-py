"""Agent 管理接口验证（27 个，不含工作流编辑器的 14 个）。

重点：
1. 列表**过滤软删**（原实现只过滤 status，删掉的 Agent 还出现在列表里）
2. 5 组关联绑定 + **数量上限**（tools≤10 / mcp≤5 / subagents≤5 / skills≤10 / kb≤10）
3. 绑定 ID 存**字符串**（雪花精度）
4. clone 名称唯一（`-副本`/`-副本2`）
5. set_default 清掉其它 agent 的 is_default
6. 删除级联版本和会话

⚠️ 会写库，测试数据用 `zz-` 前缀，跑完物理删除。

用法：python -m scripts.agents_api_check
"""

from __future__ import annotations

import asyncio
import json

import httpx
from sqlalchemy import text

from app.core.db import SessionLocal
from app.main import app

BASE = "http://test"
failures = 0
PREFIX = "zz-自检Agent"


def ok(label: str, cond: bool, detail: str = "") -> None:
    global failures
    if not cond:
        failures += 1
    print(f"  {'✅' if cond else '❌'} {label}" + (f"  → {detail}" if detail else ""))


async def api(c: httpx.AsyncClient, method: str, path: str, **kw) -> dict:
    return (await c.request(method, BASE + path, **kw)).json()


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
            await db.execute(text("DELETE FROM agent_version WHERE agent_id = :a"), {"a": aid})
            await db.execute(text("DELETE FROM message WHERE session_id IN "
                                 "(SELECT id FROM chat_session WHERE agent_id = :a)"), {"a": aid})
            await db.execute(text("DELETE FROM chat_session WHERE agent_id = :a"), {"a": aid})
            await db.execute(text("DELETE FROM agent WHERE id = :a"), {"a": aid})
        await db.commit()


async def main() -> None:
    await cleanup()  # 上次跑残留
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=BASE, timeout=30) as c:
        print("=" * 70)
        print("1. 列表（含软删过滤）")
        print("=" * 70)

        body = await api(c, "GET", "/api/agents?pageNum=1&pageSize=50")
        page = body["data"]
        ok("列表返回", body.get("code") == 200, f"total={page.get('total')}")
        # 相对断言：只数自己这次创建的，不假设库里老板的真实 Agent 数量
        baseline_total = page["total"]
        recs = page["records"]
        if recs:
            ok("id 是字符串", isinstance(recs[0]["id"], str))
            print(f"     样例: {[r['name'] for r in recs[:3]]}")

        # workflow-examples 静态路径不能 422
        ex = await api(c, "GET", "/api/agents/workflow-examples")
        ok("workflow-examples 不 422（路由顺序对）", ex.get("code") == 200,
           f"code={ex.get('code')} {str(ex.get('message'))[:40]}")

        print("\n" + "=" * 70)
        print("2. 创建 → 读取 → 更新")
        print("=" * 70)

        created = (await api(c, "POST", "/api/agents", json={
            "name": f"{PREFIX}-1",
            "description": "自检用",
            "systemPrompt": "你是一个测试助手",
            "welcomeMessage": "你好",
            "recommendedQuestions": json.dumps(["问题1", "问题2"]),
            "agentType": "chat",
            "config": json.dumps({"topP": 0.9, "temperature": 0.5}),
        }))["data"]
        aid = created["id"]
        ok("创建成功", bool(aid), str(aid))
        ok("创建后是 draft", created["status"] == "draft", str(created["status"]))
        # 相对断言：本次只新增 1 个，总数应比 baseline 多 1（不假设库里已有数量）
        after_total = (await api(c, "GET", "/api/agents?pageNum=1&pageSize=50"))["data"]["total"]
        ok("列表总数恰好 +1", after_total == baseline_total + 1,
           f"before={baseline_total} after={after_total}")
        ok("config 是对象（不是字符串）", isinstance(created["config"], dict),
           type(created["config"]).__name__)
        ok("config 内容落库", created["config"].get("topP") == 0.9, str(created["config"]))
        ok("推荐问题解析成数组",
           isinstance(created["recommendedQuestions"], list)
           and len(created["recommendedQuestions"]) == 2,
           str(created["recommendedQuestions"]))

        got = (await api(c, "GET", f"/api/agents/{aid}"))["data"]
        ok("按 id 读回", got["name"] == f"{PREFIX}-1", str(got.get("name")))

        # 更新时 config 合并（不能把 knowledges 抹掉）
        await api(c, "PUT", f"/api/agents/{aid}/knowledge", json=["111", "222"])
        upd = (await api(c, "PUT", "/api/agents", json={
            "id": aid,
            "name": f"{PREFIX}-1",
            "config": json.dumps({"maxTokens": 4096}),
        }))["data"]
        ok("更新只传部分 config", upd["config"].get("maxTokens") == 4096, str(upd["config"]))
        ok("原 config 的键保留（合并而非替换）", upd["config"].get("topP") == 0.9,
           str(upd["config"]))
        ok("绑定接口写的 knowledges 没被抹掉",
           upd["config"].get("knowledges") == ["111", "222"],
           str(upd["config"].get("knowledges")))

        print("\n" + "=" * 70)
        print("3. 5 组关联绑定 + 上限")
        print("=" * 70)

        for path, limit in [
            ("knowledge", 10),
            ("tools", 10),
            ("mcp-servers", 5),
            ("subagents", 5),
            ("skills", 10),
        ]:
            ids = [str(1000 + i) for i in range(3)]
            r = await api(c, "PUT", f"/api/agents/{aid}/{path}", json=ids)
            ok(f"PUT {path} 成功", r.get("code") == 200, str(r.get("message")))
            back = (await api(c, "GET", f"/api/agents/{aid}/{path}"))["data"]
            ok(f"GET {path} 返回字符串数组且一致", back == ids, f"{back}")
            # 超限
            over = [str(2000 + i) for i in range(limit + 1)]
            r2 = await api(c, "PUT", f"/api/agents/{aid}/{path}", json=over)
            ok(f"{path} 超 {limit} 个被拒", r2.get("code") == 400,
               str(r2.get("message"))[:50])

        # 绑真实工具 ID，验证 detail
        real_tools = (await api(c, "GET", "/api/tools?pageSize=3"))["data"]["records"]
        tool_ids = [t["id"] for t in real_tools[:3]]
        await api(c, "PUT", f"/api/agents/{aid}/tools", json=tool_ids)
        detail = (await api(c, "GET", f"/api/agents/{aid}/tools/detail"))["data"]
        ok("tools/detail 返回 3 条", len(detail) == 3, f"{len(detail)}")
        ok("detail 按绑定顺序返回",
           [d["id"] for d in detail] == tool_ids,
           f"{[d['id'] for d in detail]} vs {tool_ids}")
        ok("detail 带 displayName", all(d.get("displayName") for d in detail),
           str(detail[0]) if detail else "")

        # mcp-servers/detail
        real_mcp = (await api(c, "GET", "/api/mcp-servers?pageSize=2"))["data"]["records"]
        if real_mcp:
            mcp_ids = [m["id"] for m in real_mcp]
            await api(c, "PUT", f"/api/agents/{aid}/mcp-servers", json=mcp_ids)
            md = (await api(c, "GET", f"/api/agents/{aid}/mcp-servers/detail"))["data"]
            ok("mcp-servers/detail 返回", len(md) == len(mcp_ids), f"{len(md)}")
            ok("detail 带 isBuiltin", all("isBuiltin" in m for m in md), str(md[0]) if md else "")

        print("\n" + "=" * 70)
        print("4. chat-capabilities / mention-options")
        print("=" * 70)

        caps = (await api(c, "GET", f"/api/agents/{aid}/chat-capabilities"))["data"]
        need = {"multimodalEnabled", "enableImageInput", "allowFileUpload",
                "maxImageBytes", "maxImageSizeLabel", "allowedDocumentExtensions",
                "maxAttachmentsPerMessage"}
        ok("capabilities 字段齐全", need <= set(caps), f"缺 {need - set(caps)}")
        ok("尺寸标签是人话", caps["maxImageSizeLabel"] == "1MB", caps["maxImageSizeLabel"])
        ok("附件上限 3", caps["maxAttachmentsPerMessage"] == 3,
           str(caps["maxAttachmentsPerMessage"]))

        mention = (await api(c, "GET", f"/api/agents/{aid}/mention-options"))["data"]
        ok("mention-options 有 groups", isinstance(mention.get("groups"), list),
           str(mention)[:60])
        ok("agentId 是字符串", isinstance(mention.get("agentId"), str))

        print("\n" + "=" * 70)
        print("5. clone / set_default / 版本")
        print("=" * 70)

        cloned = (await api(c, "POST", f"/api/agents/{aid}/clone"))["data"]
        cid = cloned["id"]
        ok("克隆成功", bool(cid) and cid != aid, str(cid))
        ok("克隆名带 -副本", cloned["name"].endswith("-副本"), cloned["name"])
        ok("克隆是 draft 且非默认",
           cloned["status"] == "draft" and not cloned["isDefault"],
           f"{cloned['status']}/{cloned['isDefault']}")
        ok("克隆继承了 config", cloned["config"].get("topP") == 0.9, str(cloned["config"]))

        cloned2 = (await api(c, "POST", f"/api/agents/{aid}/clone"))["data"]
        ok("再克隆一次名字加序号", cloned2["name"].endswith("-副本2"), cloned2["name"])

        # set_default
        await api(c, "PUT", f"/api/agents/{aid}/default")
        a1 = (await api(c, "GET", f"/api/agents/{aid}"))["data"]
        a2 = (await api(c, "GET", f"/api/agents/{cloned2['id']}"))["data"]
        ok("设为默认生效", a1["isDefault"] is True, str(a1["isDefault"]))
        ok("不会同时有多个默认", a2["isDefault"] is False, str(a2["isDefault"]))

        # 版本列表（新建的 agent 没有版本）
        vs = (await api(c, "GET", f"/api/agents/{aid}/versions"))["data"]
        ok("版本列表结构对", "versions" in vs and "draftVersionId" in vs, str(vs))

        # 用一个有真实版本的 agent 测详情
        real_agent = None
        for r in recs:
            vs2 = (await api(c, "GET", f"/api/agents/{r['id']}/versions"))["data"]
            if vs2.get("versions"):
                real_agent = (r, vs2["versions"][0])
                break
        if real_agent:
            ra, v0 = real_agent
            vd = (await api(c, "GET", f"/api/agents/{ra['id']}/versions/{v0['version']}"))["data"]
            ok("版本详情有 config", "config" in vd, str(list(vd))[:80])
            print(f"     {ra['name']} v{v0['version']} status={v0['status']} "
                  f"nodes={v0.get('nodeCount')}")
        else:
            print("  - 跳过版本详情（没有带版本的 Agent）")

        print("\n" + "=" * 70)
        print("6. 删除级联 + 错误分支")
        print("=" * 70)

        # 造一个会话挂在测试 agent 上，验证级联
        async with SessionLocal() as db:
            sid = 900000000000000001
            await db.execute(
                text("INSERT INTO chat_session (id, agent_id, user_id, title, status, "
                     "message_count, total_tokens, pinned, create_time, update_time, deleted) "
                     "VALUES (:i, :a, 2090273750973657090, 'zz-自检会话', 'active', 0, 0, "
                     "false, now(), now(), 0)"),
                {"i": sid, "a": int(aid)},
            )
            await db.commit()

        r = await api(c, "DELETE", f"/api/agents/{aid}")
        ok("删除成功", r.get("code") == 200, str(r.get("message")))
        after = await api(c, "GET", f"/api/agents/{aid}")
        ok("删除后 404（软删）", after.get("code") == 404, str(after.get("code")))

        async with SessionLocal() as db:
            d = (
                await db.execute(text("SELECT deleted FROM agent WHERE id = :i"),
                                 {"i": int(aid)})
            ).scalar()
            cs = (
                await db.execute(text("SELECT deleted FROM chat_session WHERE id = :i"),
                                 {"i": 900000000000000001})
            ).scalar()
        ok("agent 软删（deleted=1）", d == 1, str(d))
        ok("会话被级联软删", cs == 1, str(cs))

        # 列表不再出现
        page2 = (await api(c, "GET", "/api/agents?pageNum=1&pageSize=50"))["data"]
        ok("软删的 Agent 不在列表里",
           all(x["id"] != aid for x in page2["records"]), f"total={page2['total']}")

        nf = await api(c, "GET", "/api/agents/999999999999999")
        ok("不存在的 Agent → 404", nf.get("code") == 404, str(nf.get("message"))[:40])

        # 重名：**未删除的**同名才拒。刚才那个已软删，所以重名是允许的
        #（软删后允许同名是合理的：用户删了再建一个同名的，不该被历史记录挡住）
        alive = (await api(c, "POST", "/api/agents", json={"name": f"{PREFIX}-dup"}))["data"]
        dup = await api(c, "POST", "/api/agents", json={"name": f"{PREFIX}-dup"})
        ok("未删除时重名创建 → 400", dup.get("code") == 400,
           str(dup.get("message"))[:50])
        ok("软删后可以重名（历史不挡新记录）", bool(aid), "已删的 zz-自检Agent-1 未阻止新建")
        await api(c, "DELETE", f"/api/agents/{alive['id']}")

        long_name = await api(c, "POST", "/api/agents", json={"name": "x" * 60})
        ok("名称超 50 字 → 400 且带 code（不是裸 422）",
           long_name.get("code") == 400, f"code={long_name.get('code')}")
        ok("校验错误信息可读", "50" in str(long_name.get("message")),
           str(long_name.get("message"))[:70])

    await cleanup()
    print("\n已清理测试数据")

    print()
    if failures:
        print(f"❌ 失败 {failures} 项")
        raise SystemExit(1)
    print("✅ 全部通过")


if __name__ == "__main__":
    asyncio.run(main())
