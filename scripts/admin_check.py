"""管理员 6 接口验证（对齐前端 `src/api/admin.js`）。

重点验证这些**静默失败**：

1. **出参不带 password**：Java 的 `Page<User>` 实体直出把 BCrypt 哈希发给前端
   （无 @JsonIgnore，纯泄露）。Python 有意修掉 —— 断言里专门验证列表/详情都没有。
2. **不能改自己的角色 / 不能删自己**：管理界面的两条自我保护，
   放开就是"误操作把自己降级然后没人能再管系统"。
3. **非管理员 403**：dev 用户是 admin，用伪造 user_id 的场景难造 ——
   这里用「普通注册用户走 admin 接口」没法测（deps 固定 dev_user_id），
   所以只验证 admin 用户全通过 + 目标不存在 404。
4. **删除是软删**（deleted=1 + status=disabled）：
   用户名有"deleted=0 才唯一"的部分索引，软删后同名可重新注册。

用法：
    python -m scripts.admin_check
"""

from __future__ import annotations

import asyncio
import time

import httpx
from sqlalchemy import text

from app.core.db import SessionLocal
from app.core.snowflake import new_id

BASE = "http://127.0.0.1:18080"
failures = 0
_STAMP = str(int(time.time()))[-6:]
_UID = 2090273750973657090
_TARGET: int | None = None


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


async def seed_user(role: str = "user") -> int:
    uid = new_id()
    from datetime import datetime

    now_str = datetime.now()
    async with SessionLocal() as db:
        await db.execute(
            text("INSERT INTO users (id, username, email, password, nickname, role, status, "
                 "create_time, update_time, deleted) "
                 "VALUES (:id, :u, '', :p, :n, :r, 'active', :t, :t, 0)"),
            {"id": uid, "u": f"zz-admin-{_STAMP}-{uid % 10000}", "p": "x" * 60,
             "n": f"zz昵称{_STAMP}", "r": role, "t": now_str},
        )
        await db.commit()
    return uid


async def cleanup() -> None:
    async with SessionLocal() as db:
        await db.execute(text("DELETE FROM users WHERE username LIKE 'zz-admin-%'"))
        # 还原 dev 用户可能被改动的字段
        await db.execute(
            text("UPDATE users SET role='admin', status='active', nickname='admin' WHERE id=:i"),
            {"i": _UID},
        )
        await db.commit()


async def main() -> int:
    global _TARGET
    await cleanup()
    _TARGET = await seed_user()

    async with httpx.AsyncClient(timeout=60, trust_env=False) as c:
        print("\n[1] 用户列表 / 详情")

        r = await api(c, "GET", "/api/admin/users?pageNum=1&pageSize=5")
        d = data(r) or {}
        ok("列表返回 records/total/pageNum/pageSize",
           {"records", "total", "pageNum", "pageSize"} <= set(d.keys()))
        # 库里用户总数可能不足 pageSize，断言上限即可
        ok("分页生效（不超过 pageSize）",
           len(d.get("records") or []) <= 5, f"len={len(d.get('records') or [])}")
        ok("total 只算自己可见的", d.get("total", 0) >= 2, str(d.get("total")))
        recs = d.get("records") or []
        ok("id 是字符串", all(isinstance(x["id"], str) for x in recs))
        ok("**出参不含 password**（Java 实体直出带哈希，Python 有意修掉）",
           all("password" not in x for x in recs),
           str([k for k in (recs[0].keys() if recs else [])]))

        r = await api(c, "GET", "/api/admin/users?keyword=zz-admin")
        d = data(r) or {}
        ok("keyword 模糊筛选（用户名/昵称）", d.get("total", 0) >= 1, str(d.get("total")))

        r = await api(c, "GET", f"/api/admin/users/{_TARGET}")
        d = data(r) or {}
        ok("详情命中且无 password", d.get("id") == str(_TARGET) and "password" not in d,
           str(d)[:120])

        r = await api(c, "GET", "/api/admin/users/999999999999999999")
        ok("不存在的用户 → code!=200（Java 返回 fail）", r.get("code") != 200, str(r)[:100])

        print("\n[2] 更新 / 自我保护")

        r = await api(c, "PUT", f"/api/admin/users/{_TARGET}",
                      json={"nickname": f"zz改名{_STAMP}", "role": "user", "status": "active"})
        ok("更新 nickname/role/status 成功", r.get("code") == 200, str(r)[:120])
        async with SessionLocal() as db:
            row = (await db.execute(
                text("SELECT nickname, role, status FROM users WHERE id=:i"), {"i": _TARGET}
            )).one()
        ok("nickname 落库", row.nickname == f"zz改名{_STAMP}", str(row))

        r = await api(c, "PUT", f"/api/admin/users/{_UID}", json={"role": "user"})
        ok("**不能修改自己的角色**（防误降级）", r.get("code") == 400, str(r)[:120])

        r = await api(c, "PUT", f"/api/admin/users/{_UID}", json={"nickname": "admin-self"})
        ok("改自己的 nickname 允许（只有角色被拦）", r.get("code") == 200, str(r)[:100])

        print("\n[3] 资源视图 / 删除")

        r = await api(c, "GET", f"/api/admin/users/{_UID}/agents")
        ok("查自己的 Agent 列表（数组）", isinstance(data(r), list), str(r)[:120])

        r = await api(c, "GET", f"/api/admin/users/{_UID}/knowledges")
        ok("查自己的知识库列表（member 反查）", isinstance(data(r), list), str(r)[:120])

        r = await api(c, "DELETE", f"/api/admin/users/{_UID}")
        ok("**不能删除自己**", r.get("code") == 400, str(r)[:120])

        r = await api(c, "DELETE", f"/api/admin/users/{_TARGET}")
        ok("删除其他用户成功", r.get("code") == 200, str(r)[:100])
        async with SessionLocal() as db:
            row = (await db.execute(
                text("SELECT deleted, status FROM users WHERE id=:i"), {"i": _TARGET}
            )).one()
        ok("软删生效（deleted=1 且 status=disabled）",
           row.deleted == 1 and row.status == "disabled", str(row))

        r = await api(c, "GET", f"/api/admin/users/{_TARGET}")
        ok("删后详情查不到", r.get("code") != 200, str(r)[:100])

    await cleanup()
    print(f"\n{'=' * 60}")
    print(f"失败 {failures} 项")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
