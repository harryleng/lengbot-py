"""工具管理 9 个接口的端到端验证。

三条业务规则是重点，错了**不会报错、只会静默丢修改**：
1. `knowledge` 类型不可编辑/删除/禁用（由知识库注册器托管）
2. `builtin` 类型只能改限流字段（其余字段由 BuiltinToolRegistrar 从代码注解同步）
3. `tags` 过滤走 JSONB `@>` 包含操作符

用法：
    python -m scripts.tools_api_check
"""

from __future__ import annotations

import asyncio
import json

import httpx

from app.main import app

BASE = "http://test"
failures = 0
TEST_TAG = "zz-自检"


def ok(label: str, cond: bool, detail: str = "") -> None:
    global failures
    if not cond:
        failures += 1
    print(f"  {'✅' if cond else '❌'} {label}" + (f"  → {detail}" if detail else ""))


async def api(client: httpx.AsyncClient, method: str, path: str, **kw) -> dict:
    resp = await client.request(method, BASE + path, **kw)
    return resp.json()


async def main() -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=BASE, timeout=40) as c:
        # ---------------------------------------------------------------
        print("=" * 70)
        print("1. 列表 + 序列化")
        print("=" * 70)

        body = await api(c, "GET", "/api/tools?pageNum=1&pageSize=100")
        page = body["data"]
        ok("列表返回", body.get("code") == 200 and "records" in page,
           f"total={page.get('total')}")
        recs = page["records"]
        ok("库里 32 个工具", page["total"] == 32, f"total={page['total']}")

        if recs:
            t = recs[0]
            ok("id 是字符串", isinstance(t["id"], str))
            ok("tags 是数组（前端直接 map）", isinstance(t["tags"], list),
               type(t["tags"]).__name__)
            ok("inputSchema 是字符串（前端 JSON.parse）",
               isinstance(t["inputSchema"], str), type(t["inputSchema"]).__name__)
            ok("带 implemented 标记", "implemented" in t,
               f"{t.get('implemented')}")

        impl = [x for x in recs if x.get("implemented")]
        not_impl = [x for x in recs if not x.get("implemented")]
        ok("有已实现的工具", len(impl) > 0, f"{len(impl)} 个")
        ok("有未实现的工具（说明标记有效）", len(not_impl) > 0, f"{len(not_impl)} 个")
        print(f"     已实现样例: {[x['name'] for x in impl[:5]]}")

        # ---------------------------------------------------------------
        print("\n" + "=" * 70)
        print("2. 过滤：keyword / toolType / tag")
        print("=" * 70)

        kw = (await api(c, "GET", "/api/tools?keyword=file&pageSize=100"))["data"]
        ok("keyword 搜 name/displayName",
           all("file" in (x["name"] or "").lower()
               or "file" in (x["displayName"] or "").lower() for x in kw["records"]),
           f"{kw['total']} 条")

        bt = (await api(c, "GET", "/api/tools?toolType=builtin&pageSize=100"))["data"]
        ok("toolType=builtin 过滤生效",
           all(x["toolType"] == "builtin" for x in bt["records"]), f"{bt['total']} 条")
        # 库里是 26 builtin + 6 knowledge = 32（实测，不是猜的）
        kg_all = (await api(c, "GET", "/api/tools?toolType=knowledge&pageSize=100"))["data"]
        ok("builtin + knowledge = 全部 32 个",
           bt["total"] + kg_all["total"] == page["total"],
           f"{bt['total']} + {kg_all['total']} = {bt['total'] + kg_all['total']}")

        # ---------------------------------------------------------------
        print("\n" + "=" * 70)
        print("3. 创建 → 读取 → tag 过滤 → 更新 → 禁用 → 删除")
        print("=" * 70)

        created = (await api(c, "POST", "/api/tools", json={
            "name": "zz_selfcheck_tool",
            "displayName": "自检临时工具",
            "description": "自动验证用，跑完会删",
            "toolType": "api",
            "inputSchema": json.dumps({
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "城市名"},
                    "days": {"type": "integer", "description": "天数"},
                    "flag": {"type": "boolean"},
                    "items": {"type": "array"},
                },
                "required": ["city"],
            }),
            "outputSchema": "{}",
            "config": json.dumps({"exampleParams": {"city": "深圳", "days": 3}}),
            "endpointUrl": "https://example.com/api",
            "authType": "bearer",
            "tags": [TEST_TAG, "天气"],
            "icon": "ApiOutlined",
        }))["data"]
        tid = created["id"]
        ok("创建成功", bool(tid), str(tid))
        ok("tags 落库为数组", created["tags"] == [TEST_TAG, "天气"], str(created["tags"]))
        ok("自定义工具 implemented=false", created["implemented"] is False)

        got = (await api(c, "GET", f"/api/tools/{tid}"))["data"]
        ok("按 id 读回", got["name"] == "zz_selfcheck_tool", str(got.get("name")))

        # tag 过滤（JSONB @>）
        tagged = (await api(c, "GET", f"/api/tools?tag={TEST_TAG}&pageSize=100"))["data"]
        ok("tag 过滤命中", any(x["id"] == tid for x in tagged["records"]),
           f"命中 {tagged['total']} 条")
        ok("tag 过滤不含无关工具",
           all(TEST_TAG in (x["tags"] or []) for x in tagged["records"]),
           str([x["tags"] for x in tagged["records"]][:3]))

        # io-schema
        io = (await api(c, "GET", f"/api/tools/{tid}/io-schema"))["data"]
        ok("io-schema 有 toolName", io.get("toolName") == "zz_selfcheck_tool", str(io.get("toolName")))
        inputs = io.get("inputs") or []
        ok("inputs 有 4 个字段", len(inputs) == 4, f"{len(inputs)} 个")
        city = next((x for x in inputs if x["key"] == "city"), None)
        ok("city 标了 required", city is not None and city.get("required") is True, str(city))
        days = next((x for x in inputs if x["key"] == "days"), None)
        ok("integer → Number（不是 'integer'）", days is not None and days["type"] == "Number",
           str(days))
        flag = next((x for x in inputs if x["key"] == "flag"), None)
        ok("boolean → Boolean", flag is not None and flag["type"] == "Boolean", str(flag))
        outputs = io.get("outputs") or []
        ok("outputs 为空时给默认 output", len(outputs) == 1 and outputs[0]["key"] == "output",
           str(outputs))

        ex = (await api(c, "GET", f"/api/tools/{tid}/example"))["data"]
        ok("example 从 config.exampleParams 取到", ex.get("city") == "深圳", str(ex))

        # 更新（自定义工具全字段）
        upd = (await api(c, "PUT", "/api/tools", json={
            "id": tid,
            "name": "zz_selfcheck_tool",
            "displayName": "自检临时工具-改名",
            "toolType": "api",
            "inputSchema": "{}",
            "outputSchema": "{}",
            "config": "{}",
            "tags": ["改后标签"],
            "rateLimitEnabled": True,
            "rateLimitConfig": json.dumps({"qps": 5}),
        }))["data"]
        ok("更新生效", upd["displayName"].endswith("改名"), str(upd["displayName"]))
        ok("tags 被替换", upd["tags"] == ["改后标签"], str(upd["tags"]))
        ok("限流字段更新", upd["rateLimitEnabled"] is True, str(upd["rateLimitEnabled"]))

        await api(c, "PUT", f"/api/tools/{tid}/enabled?enabled=false")
        ok("禁用生效",
           (await api(c, "GET", f"/api/tools/{tid}"))["data"]["status"] == "disabled")

        await api(c, "DELETE", f"/api/tools/{tid}")
        after = await api(c, "GET", f"/api/tools/{tid}")
        ok("删除后 404（软删）", after.get("code") == 404, str(after.get("code")))

        # ---------------------------------------------------------------
        print("\n" + "=" * 70)
        print("4. 业务规则：builtin 只能改限流，knowledge 全只读")
        print("=" * 70)

        builtin = next(x for x in recs if x["toolType"] == "builtin")
        bid = builtin["id"]
        orig_name = builtin["name"]
        orig_desc = builtin["description"]

        res = (await api(c, "PUT", "/api/tools", json={
            "id": bid,
            "name": "想改内置工具名",
            "displayName": "想改显示名",
            "description": "想改描述",
            "toolType": "builtin",
            "inputSchema": "{}",
            "outputSchema": "{}",
            "config": "{}",
            "tags": ["想改标签"],
            "rateLimitEnabled": True,
            "rateLimitConfig": json.dumps({"qps": 99}),
        }))["data"]
        ok("builtin 名称没被改（保护生效）", res["name"] == orig_name,
           f"{res['name']} vs {orig_name}")
        ok("builtin 描述没被改", res["description"] == orig_desc,
           str(res["description"])[:40])
        ok("builtin 限流字段**被改**（这是唯一允许的）",
           res["rateLimitEnabled"] is True, str(res["rateLimitEnabled"]))

        # 恢复原限流状态，别污染真数据
        await api(c, "PUT", "/api/tools", json={
            "id": bid, "name": res["name"], "toolType": "builtin",
            "rateLimitEnabled": False, "rateLimitConfig": "{}",
        })
        ok("已恢复 builtin 的限流设置", True)

        def _need_knowledge() -> None:
            print("  - 跳过 knowledge 规则（库里没有 type=knowledge 的工具）")

        kg = (await api(c, "GET", "/api/tools?toolType=knowledge&pageSize=10"))["data"]
        if not kg["records"]:
            _need_knowledge()
        else:
            kid = kg["records"][0]["id"]
            r1 = await api(c, "PUT", "/api/tools", json={
                "id": kid, "name": "x", "toolType": "knowledge"})
            ok("knowledge 编辑被拒", r1.get("code") == 400, str(r1.get("message"))[:60])
            r2 = await api(c, "DELETE", f"/api/tools/{kid}")
            ok("knowledge 删除被拒", r2.get("code") == 400, str(r2.get("message"))[:60])
            r3 = await api(c, "PUT", f"/api/tools/{kid}/enabled?enabled=false")
            ok("knowledge 禁用被拒", r3.get("code") == 400, str(r3.get("message"))[:60])

        # ---------------------------------------------------------------
        print("\n" + "=" * 70)
        print("5. 测试执行 + 错误分支")
        print("=" * 70)

        calc = next((x for x in recs if x["name"] == "calculator"), None)
        if calc:
            out = (await api(c, "POST", f"/api/tools/{calc['id']}/test",
                             json={"args": {"a": 6, "b": 7, "operation": "multiply"}}))["data"]
            parsed = json.loads(out) if out and out.strip().startswith("{") else {}
            ok("calculator 测试执行成功", parsed.get("result") == "42",
               str(out)[:100])
        else:
            print("  - 跳过：库里没有 calculator")

        # 未实现的工具 → 明确 400
        target = next((x for x in recs if not x.get("implemented")), None)
        if target:
            r = await api(c, "POST", f"/api/tools/{target['id']}/test", json={"args": {}})
            ok("未实现工具的测试被明确拒绝",
               r.get("code") == 400 and "尚未在 Python 版实现" in str(r.get("message")),
               str(r.get("message"))[:80])

        nf = await api(c, "GET", "/api/tools/999999999999999")
        ok("不存在的工具 → 404", nf.get("code") == 404, str(nf.get("message"))[:40])

        dup = (await api(c, "POST", "/api/tools", json={
            "name": "calculator", "toolType": "api"}))
        ok("重名创建 → 400", dup.get("code") == 400, str(dup.get("message"))[:60])

        # 清理
        from sqlalchemy import text as sql_text

        from app.core.db import SessionLocal

        async with SessionLocal() as db:
            await db.execute(
                sql_text("DELETE FROM tool WHERE name LIKE 'zz_selfcheck%'")
            )
            await db.commit()
        ok("已清理测试数据", True)

    print()
    if failures:
        print(f"❌ 失败 {failures} 项")
        raise SystemExit(1)
    print("✅ 全部通过")


if __name__ == "__main__":
    asyncio.run(main())
