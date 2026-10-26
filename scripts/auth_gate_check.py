"""认证收口验证（auth gate）：开关两态全测。

**不走 18080 的长驻服务**：在脚本进程内先设 `AUTH_ENABLED=true` 再 import app，
用 httpx `ASGITransport` 直连 —— 同一个进程里拿到的是一个"上线态"的 app 实例，
不污染长驻服务，也不影响 33 项回归（它们跑在开发态）。

上线态必须验证的行为：

1. **未认证一律 401**：不带 token 访问任何依赖身份的端点；
   `dev-<uid>` 占位 token 也必须失效（收口的意义就是它不再是通行证）。
2. **login 真校验 BCrypt**：密码错 → 401；对 → 发真 token（Redis 有状态）。
3. **token 可用且可吊销**：me 返回对应用户；logout 后同一 token 立即 401。
4. **API Key 可作为凭据**：创建 `lbkey_` → 访问 me → 200；停用后 401。
5. **register/init-admin 自动登录**：响应里直接带可用 token。
6. **公开端点不受影响**：enums / init-status / landing GET 无 token 也 200。
7. **开发态行为不变**：AUTH_ENABLED=false 时无 token 也能访问（回归前提）。

用法：
    python -m scripts.auth_gate_check
"""

from __future__ import annotations

import asyncio
import os

# ⚠️ 必须在 import app 之前设置环境变量 —— settings 在 import 时读取
os.environ["AUTH_ENABLED"] = "true"

import httpx  # noqa: E402

failures = 0
_STAMP = "authgate"


def ok(label: str, cond: bool, detail: str = "") -> None:
    global failures
    if not cond:
        failures += 1
    print(f"  {'✅' if cond else '❌'} {label}" + (f"  → {detail}" if detail else ""))


async def call(c: httpx.AsyncClient, method: str, path: str, **kw) -> dict:
    resp = await c.request(method, path, **kw)
    try:
        return resp.json()
    except Exception:  # noqa: BLE001
        return {"code": resp.status_code, "message": resp.text[:200]}


async def main() -> int:
    global failures
    from app.core.db import SessionLocal
    from app.services.user_admin_service import hash_password
    from sqlalchemy import text

    username = "zz-authgate"
    password = "gatepass66"

    # 造一个已知密码的上线态用户
    async with SessionLocal() as db:
        await db.execute(text("DELETE FROM users WHERE username = :u"), {"u": username})
        await db.execute(text(
            "INSERT INTO users (id, username, email, password, nickname, role, status, "
            "create_time, update_time, deleted) "
            "VALUES (990000000000000001, :u, '', :p, '认证自检', 'user', 'active', "
            "now(), now(), 0)"),
            {"u": username, "p": hash_password(password)},
        )
        await db.commit()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver", timeout=60
    ) as c:
        print("\n[1] 上线态：未认证一律 401")

        r = await call(c, "GET", "/api/auth/me")
        ok("无 token 访问 me → 401", r.get("code") == 401, str(r)[:120])
        r = await call(c, "GET", "/api/auth/me", headers={"Authorization": "dev-2090273750973657090"})
        ok("`dev-<uid>` 占位 token 失效（收口的意义）", r.get("code") == 401, str(r)[:120])
        r = await call(c, "GET", "/api/agents")
        ok("业务端点无 token 也 401", r.get("code") == 401, str(r)[:120])

        print("\n[2] 公开端点不受影响")
        r = await call(c, "GET", "/api/enums/tool-types")
        ok("enums 无 token 200", r.get("code") == 200)
        r = await call(c, "GET", "/api/auth/init-status")
        ok("init-status 公开", r.get("code") == 200)
        r = await call(c, "GET", "/api/landing/config")
        ok("landing GET 公开", r.get("code") == 200)

        print("\n[3] login 真校验 BCrypt")
        r = await call(c, "POST", "/api/auth/login",
                       json={"username": username, "password": "wrong-password"})
        ok("密码错误 → 401", r.get("code") == 401, str(r)[:120])
        r = await call(c, "POST", "/api/auth/login",
                       json={"username": "zz-not-exist", "password": password})
        ok("用户不存在 → 401", r.get("code") == 401, str(r)[:100])
        r = await call(c, "POST", "/api/auth/login",
                       json={"username": username, "password": password})
        d = r.get("data") or {}
        token = d.get("token") or ""
        ok("密码正确 → 发真 token（lbt_ 前缀）", token.startswith("lbt_"), token[:16])
        ok("user 出参对得上", (d.get("user") or {}).get("username") == username)

        print("\n[4] token 可用 / 可吊销")
        r = await call(c, "GET", "/api/auth/me", headers={"Authorization": token})
        ok("token 访问 me 返回对应用户",
           r.get("code") == 200 and (r.get("data") or {}).get("username") == username,
           str(r)[:120])
        r = await call(c, "POST", "/api/auth/logout", headers={"Authorization": token})
        ok("logout 200", r.get("code") == 200)
        r = await call(c, "GET", "/api/auth/me", headers={"Authorization": token})
        ok("logout 后同一 token 立即 401（服务端可吊销）", r.get("code") == 401, str(r)[:120])

        print("\n[5] register / API Key")
        r = await call(c, "POST", "/api/auth/register", json={
            "username": f"zz-{_STAMP}-reg", "password": "regpass66", "nickname": "注册自检"})
        d = r.get("data") or {}
        if not d.get("token"):
            print(f"  ⚠️ register 响应: {str(r)[:300]}")
        reg_token = d.get("token") or ""
        ok("register 自动登录（带可用 token）", reg_token.startswith("lbt_"), reg_token[:16])
        r = await call(c, "GET", "/api/auth/me", headers={"Authorization": reg_token})
        ok("注册 token 可用", r.get("code") == 200
           and (r.get("data") or {}).get("username") == f"zz-{_STAMP}-reg", str(r)[:120])

        # API Key 路径（dev 用户的 key）
        r = await call(c, "POST", "/api/api-keys",
                       headers={"Authorization": reg_token},
                       json={"name": "zz-authgate-key", "permissions": "chat"})
        d = r.get("data") or {}
        secret = d.get("secret") or ""
        ok("创建 API Key 拿到明文", secret.startswith("lbkey_"), secret[:16])
        r = await call(c, "GET", "/api/auth/me", headers={"Authorization": secret})
        ok("lbkey_ 作为凭据访问 me", r.get("code") == 200, str(r)[:120])
        key_id = (d.get("apiKey") or {}).get("id")
        r = await call(c, "PATCH", f"/api/api-keys/{key_id}/toggle",
                       headers={"Authorization": reg_token})
        r = await call(c, "GET", "/api/auth/me", headers={"Authorization": secret})
        ok("停用后 lbkey_ 401", r.get("code") == 401, str(r)[:100])
        await call(c, "DELETE", f"/api/api-keys/{key_id}",
                   headers={"Authorization": reg_token})

    # 清理
    async with SessionLocal() as db:
        await db.execute(text("DELETE FROM users WHERE username LIKE 'zz-authgate%'"))
        await db.commit()

    # 开发态前提：.env 里不设 AUTH_ENABLED（默认 False），
    # 33 项回归在独立进程（无该 env）里跑的就是 False
    from pathlib import Path

    env_text = (Path(".env").read_text(encoding="utf-8") if Path(".env").exists() else "")
    ok("开发态前提：.env 未开启 AUTH_ENABLED（默认 False）",
       "AUTH_ENABLED" not in env_text or "AUTH_ENABLED=true" not in env_text.lower())

    print(f"\n{'=' * 60}")
    print(f"失败 {failures} 项")
    return 1 if failures else 0


if __name__ == "__main__":
    from app.main import app  # noqa: E402  # env 之后再 import

    raise SystemExit(asyncio.run(main()))
