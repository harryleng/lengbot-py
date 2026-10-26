"""MCP 服务器 10 个接口验证（7 个实现 + 3 个明确报错）。

重点：
1. `isBuiltin` 是数字（前端 `Number(x) === 1`）
2. `deployConfig` / `disabledTools` 是 JSON 字符串
3. 内置 server **不可删**
4. `toggle` 只改数组、不需要连接
5. 三个需连接的接口**明确 400 并说明原因**（不能返回空数组装没事）

用法：python -m scripts.mcp_api_check
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


def ok(label: str, cond: bool, detail: str = "") -> None:
    global failures
    if not cond:
        failures += 1
    print(f"  {'✅' if cond else '❌'} {label}" + (f"  → {detail}" if detail else ""))


async def api(c: httpx.AsyncClient, method: str, path: str, **kw) -> dict:
    return (await c.request(method, BASE + path, **kw)).json()


async def main() -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=BASE, timeout=30) as c:
        print("=" * 70)
        print("1. 列表 + 序列化")
        print("=" * 70)

        body = await api(c, "GET", "/api/mcp-servers?pageNum=1&pageSize=50")
        page = body["data"]
        ok("列表返回", body.get("code") == 200, f"total={page.get('total')}")
        recs = page["records"]
        ok("库里有 2 个未删除的 server", page["total"] == 2, f"{page['total']}")

        if recs:
            s = recs[0]
            ok("id 是字符串", isinstance(s["id"], str), type(s["id"]).__name__)
            ok("isBuiltin 是数字 1（前端 Number(x)===1）",
               isinstance(s["isBuiltin"], int) and s["isBuiltin"] == 1,
               f"{s['isBuiltin']} ({type(s['isBuiltin']).__name__})")
            ok("deployConfig 是字符串", isinstance(s["deployConfig"], str),
               type(s["deployConfig"]).__name__)
            ok("disabledTools 是字符串（Java 同）",
               isinstance(s["disabledTools"], str), type(s["disabledTools"]).__name__)
            ok("disabledTools 能被 JSON.parse",
               isinstance(json.loads(s["disabledTools"]), list), s["disabledTools"][:40])
            print(f"     样例: {s['name']} transport={s['transport']} "
                  f"status={s['status']}")

        builtin = next((x for x in recs if x["isBuiltin"] == 1), None)
        ok("存在内置 server（用于测删除保护）", builtin is not None)

        print("\n" + "=" * 70)
        print("2. 创建 → 读取 → 更新 → toggle → 启停 → 删除")
        print("=" * 70)

        created = (await api(c, "POST", "/api/mcp-servers", json={
            "name": "zz-selfcheck-mcp",
            "description": "自检临时 MCP server",
            "icon": "ApiOutlined",
            "installType": "npx",
            "deployConfig": json.dumps({"packageName": "@test/mcp", "args": ["--x"]}),
            "host": None,
            "transport": "stdio",
            "headers": "{}",
            "disabledTools": "[]",
        }))["data"]
        sid = created["id"]
        ok("创建成功", bool(sid), str(sid))
        ok("新建的一律非内置", created["isBuiltin"] == 0, str(created["isBuiltin"]))
        ok("创建后状态 active", created["status"] == "active", str(created["status"]))
        ok("deployConfig 落库后能读回字符串",
           "packageName" in created["deployConfig"], created["deployConfig"][:60])

        got = (await api(c, "GET", f"/api/mcp-servers/{sid}"))["data"]
        ok("按 id 读回", got["name"] == "zz-selfcheck-mcp", str(got.get("name")))

        upd = (await api(c, "PUT", "/api/mcp-servers", json={
            "id": sid,
            "name": "zz-selfcheck-mcp",
            "description": "改后的描述",
            "installType": "sse",
            "transport": "sse",
            "host": "https://example.com/mcp",
            "deployConfig": "{}",
            "headers": json.dumps({"Authorization": "Bearer x"}),
        }))["data"]
        ok("更新生效", upd["description"] == "改后的描述", str(upd["description"]))
        ok("transport 更新", upd["transport"] == "sse", str(upd["transport"]))
        ok("host 更新", upd["host"] == "https://example.com/mcp", str(upd["host"]))

        # toggle：加一次（禁用）→ 再加一次（启用）
        await api(c, "PUT", f"/api/mcp-servers/{sid}/tools/foo/toggle")
        t1 = (await api(c, "GET", f"/api/mcp-servers/{sid}"))["data"]
        ok("toggle 第一次 → 进黑名单",
           json.loads(t1["disabledTools"]) == ["foo"], t1["disabledTools"])

        await api(c, "PUT", f"/api/mcp-servers/{sid}/tools/foo/toggle")
        t2 = (await api(c, "GET", f"/api/mcp-servers/{sid}"))["data"]
        ok("toggle 第二次 → 出黑名单（幂等切换）",
           json.loads(t2["disabledTools"]) == [], t2["disabledTools"])

        await api(c, "PUT", f"/api/mcp-servers/{sid}/enabled?enabled=false")
        ok("禁用生效",
           (await api(c, "GET", f"/api/mcp-servers/{sid}"))["data"]["status"] == "disabled")

        await api(c, "DELETE", f"/api/mcp-servers/{sid}")
        after = await api(c, "GET", f"/api/mcp-servers/{sid}")
        ok("删除后 404（软删）", after.get("code") == 404, str(after.get("code")))

        print("\n" + "=" * 70)
        print("3. 内置不可删 + 三个未实现接口明确报错")
        print("=" * 70)

        if builtin:
            r = await api(c, "DELETE", f"/api/mcp-servers/{builtin['id']}")
            ok("内置 server 删除被拒", r.get("code") == 400, str(r.get("message"))[:60])
            ok("错误信息说明了原因", "内置" in str(r.get("message")), str(r.get("message"))[:50])

            for method, path, label in [
                ("POST", f"/api/mcp-servers/{builtin['id']}/test", "test 连接"),
                ("GET", f"/api/mcp-servers/{builtin['id']}/tools", "列出工具"),
                ("POST", f"/api/mcp-servers/{builtin['id']}/tools/refresh", "刷新工具"),
            ]:
                r = await api(c, method, path)
                ok(f"{label} → 400 并说明未实现",
                   r.get("code") == 400 and "MCP 客户端" in str(r.get("message")),
                   str(r.get("message"))[:70])

        print("\n" + "=" * 70)
        print("4. 错误分支")
        print("=" * 70)

        nf = await api(c, "GET", "/api/mcp-servers/999999999999999")
        ok("不存在的 server → 404", nf.get("code") == 404, str(nf.get("message"))[:40])

        dup = await api(c, "POST", "/api/mcp-servers", json={"name": "mcp-server-chart"})
        ok("重名创建 → 400", dup.get("code") == 400, str(dup.get("message"))[:60])

        # 清理
        async with SessionLocal() as db:
            await db.execute(text("DELETE FROM mcp_server WHERE name LIKE 'zz-selfcheck%'"))
            await db.commit()
        ok("已清理测试数据", True)

    print()
    if failures:
        print(f"❌ 失败 {failures} 项")
        raise SystemExit(1)
    print("✅ 全部通过")


if __name__ == "__main__":
    asyncio.run(main())
