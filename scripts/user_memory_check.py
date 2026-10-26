"""用户长期记忆 5 接口验证（对齐前端 `src/api/userMemory.js`）。

重点验证这些**静默失败**：

1. **type/status 三口径**：`preference` / `PREFERENCE` / `用户偏好` 必须都认
   （前端存 code、LLM 抽取存枚举名、手工输入可能是中文），未命中回落 preference。
2. **空 keywords 自动提取**：标点清洗 → 2~20 字 token → 去重 12 个。
   Java 里记忆的"关键词检索"全靠这列，空了搜索就退化为全表。
3. **embedding_vector 真的写入**（原生 SQL，不走 ORM）：
   向量化失败只 log —— 断言"列非空"来确认这条链路通。
4. **15 条裁剪**：新增第 16 条时，停用/归档的先被挤掉，active 高 confidence 的留下。
5. **删除是逻辑删**（deleted=1），列表立刻消失。

用法：
    python -m scripts.user_memory_check
"""

from __future__ import annotations

import asyncio
import time

import httpx
from sqlalchemy import text

from app.core.snowflake import new_id

from app.core.db import SessionLocal

BASE = "http://127.0.0.1:18080"
failures = 0
_STAMP = str(int(time.time()))[-6:]
_UID = 2090273750973657090
_IDS: list[int] = []


def ok(label: str, cond: bool, detail: str = "") -> None:
    global failures
    if not cond:
        failures += 1
    print(f"  {'✅' if cond else '❌'} {label}" + (f"  → {detail}" if detail else ""))


async def api(c: httpx.AsyncClient, method: str, path: str, **kw) -> dict:
    resp = await c.request(method, BASE + path, **kw)
    try:
        return resp.json()
    except Exception:  # noqa: BLE001
        return {"code": resp.status_code, "message": resp.text[:200]}


def data(body: dict) -> dict | list | None:
    return body.get("data")


async def cleanup() -> None:
    async with SessionLocal() as db:
        await db.execute(
            text("DELETE FROM user_memory WHERE user_id = :u AND content LIKE 'zz-%'"),
            {"u": _UID},
        )
        await db.commit()


_LATEST = [0]


async def check_create_and_list(c: httpx.AsyncClient) -> int:
    print("\n[1] 新增 / 列表 / 三口径")

    r = await api(c, "POST", "/api/user/memories", json={
        "content": f"zz-{_STAMP} 老板偏好用中文回复，代码注释要讲清楚为什么这么设计。",
        "memoryType": "preference", "keywords": ["中文回复", "注释"], "confidence": 0.9,
    })
    d = data(r) or {}
    ok("新增成功（type=code）", r.get("code") == 200 and d.get("memoryType") == "preference",
       str(d)[:150])
    ok("id 是字符串 / confidence 回读", isinstance(d.get("id"), str) and d.get("confidence") == 0.9)
    ok("keywords 原样保存", d.get("keywords") == ["中文回复", "注释"], str(d.get("keywords")))
    mid = int(d["id"])
    _IDS.append(mid)

    r = await api(c, "POST", "/api/user/memories", json={
        "content": f"zz-{_STAMP} 项目事实：主库在 localhost。",
        "memoryType": "用户偏好",
    })
    ok("type=中文 desc 也认（三口径）", (data(r) or {}).get("memoryType") == "preference",
       str((data(r) or {}).get("memoryType")))
    _IDS.append(int(data(r)["id"]))

    r = await api(c, "POST", "/api/user/memories", json={
        "content": f"zz-{_STAMP} 无类型无关键词的记忆，验证自动提取。",
    })
    d = data(r) or {}
    ok("type 缺省回落 preference", d.get("memoryType") == "preference")
    kws = d.get("keywords") or []
    ok("空 keywords 自动提取（2~20 字 token）", len(kws) >= 1 and all(2 <= len(k) <= 20 for k in kws),
       str(kws))
    _IDS.append(int(d["id"]))
    _LATEST[0] = int(d["id"])

    r = await api(c, "POST", "/api/user/memories", json={"content": "   "})
    ok("空白内容 400", r.get("code") in (400, 422), str(r)[:100])

    r = await api(c, "GET", "/api/user/memories")
    items = data(r) or []
    # ⚠️ 库里有存量记忆，新增可能触发 15 条裁剪 —— 先建的（confidence/时间最差）会被挤掉，
    # 这是**正确行为**不是 bug。断言只要求"最新的活着 + 总数不超过 15"。
    ok("列表 update_time 倒序且含最新一条",
       items and items[0]["id"] >= items[-1]["id"] and any(int(x["id"]) == _LATEST[0] for x in items),
       f"len={len(items)}")
    ok("总数 ≤15（裁剪生效）", len(items) <= 15, f"len={len(items)}")

    r = await api(c, "GET", f"/api/user/memories?keyword=zz-{_STAMP}")
    ok("keyword 筛 content（存活的新建记录）", len(data(r) or []) >= 1, str(len(data(r) or [])))

    r = await api(c, "GET", "/api/user/memories?status=ARCHIVED")
    ok("status=枚举名也认（三口径，此时应为空）", data(r) == [], str(len(data(r) or [])))
    return mid


async def check_update_status_delete(c: httpx.AsyncClient, mid: int) -> None:
    print("\n[2] 更新 / 状态 / 删除")

    mid = _LATEST[0]
    r = await api(c, "PUT", f"/api/user/memories/{mid}", json={
        "content": f"zz-{_STAMP} 更新后的记忆内容，验证向量刷新。", "memoryType": "INSTRUCTION",
        "confidence": 0.8,
    })
    d = data(r) or {}
    ok("更新成功且 type=枚举名也认", d.get("memoryType") == "instruction"
       and d.get("confidence") == 0.8, str(d)[:150])

    async with SessionLocal() as db:
        vec = (await db.execute(
            text("SELECT embedding_vector IS NOT NULL FROM user_memory WHERE id=:i"),
            {"i": mid},
        )).scalar_one()
    ok("embedding_vector 已写入（原生 SQL 链路）", vec is True, str(vec))

    r = await api(c, "PUT", f"/api/user/memories/{mid}/status", json={"status": "disabled"})
    ok("停用成功", (data(r) or {}).get("status") == "disabled", str(r)[:120])

    r = await api(c, "GET", "/api/user/memories?status=disabled")
    ok("按 status 筛到停用条目", any(int(x["id"]) == mid for x in (data(r) or [])))

    r = await api(c, "PUT", f"/api/user/memories/{mid}/status", json={"status": "启用"})
    ok("status=中文 desc 也认", (data(r) or {}).get("status") == "active")

    r = await api(c, "DELETE", f"/api/user/memories/{mid}")
    ok("删除成功", r.get("code") == 200, str(r)[:100])
    r = await api(c, "GET", "/api/user/memories")
    ok("删后列表消失", not any(int(x["id"]) == mid for x in (data(r) or [])))
    r = await api(c, "DELETE", f"/api/user/memories/{mid}")
    ok("重复删除 404（归属校验）", r.get("code") == 404, str(r)[:100])


async def check_prune(c: httpx.AsyncClient) -> None:
    print("\n[3] 15 条裁剪（status 权重优先挤掉）")

    # 先造 14 条 active（低 confidence）+ 2 条 archived → 新增第 17 条触发裁剪
    async with SessionLocal() as db:
        for i in range(14):
            await db.execute(text(
                "INSERT INTO user_memory (id, user_id, memory_type, content, keywords, "
                "confidence, status, create_time, update_time, deleted) "
                "VALUES (:id, :u, 'preference', :c, '[]'::jsonb, 0.3, 'active', "
                "now(), now(), 0)"),
                {"id": new_id(), "u": _UID, "c": f"zz-填充-{i}"},
            )
        await db.execute(text(
            "INSERT INTO user_memory (id, user_id, memory_type, content, keywords, "
            "confidence, status, create_time, update_time, deleted) "
            "VALUES (:id, :u, 'preference', :c, '[]'::jsonb, 0.9, 'archived', "
            "now(), now(), 0)"),
            {"id": new_id(), "u": _UID, "c": f"zz-归档- {_STAMP}"},
        )
        await db.commit()

    r = await api(c, "POST", "/api/user/memories",
                  json={"content": f"zz-{_STAMP} 触发裁剪的新记忆", "confidence": 0.99})
    ok("第 17 条新增成功", r.get("code") == 200, str(r)[:120])

    async with SessionLocal() as db:
        total = (await db.execute(
            text("SELECT count(*) FROM user_memory WHERE user_id=:u AND deleted=0 "
                 "AND content LIKE 'zz-%'"),
            {"u": _UID},
        )).scalar_one()
        archived_left = (await db.execute(
            text("SELECT count(*) FROM user_memory WHERE user_id=:u AND deleted=0 "
                 "AND status='archived'"),
            {"u": _UID},
        )).scalar_one()
    ok("总数被压回 15 以内", total <= 15, str(total))
    ok("archived 的先被挤掉（statusWeight 优先）", archived_left == 0, str(archived_left))


async def main() -> int:
    await cleanup()
    async with httpx.AsyncClient(timeout=60, trust_env=False) as c:
        mid = await check_create_and_list(c)
        await check_update_status_delete(c, mid)
        await check_prune(c)
    await cleanup()

    print(f"\n{'=' * 60}")
    print(f"失败 {failures} 项")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
