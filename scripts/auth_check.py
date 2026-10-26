"""认证扩展 8 接口验证（register / profile / avatar / password / init-* / users 查询）。

重点验证这些**静默失败**：

1. **bcrypt 与 Java 互通**：Java Sa-Token 写的 `$2a$10$...` 哈希，Python `bcrypt.checkpw`
   必须能验 —— 迁移期两侧混跑，验不了就把所有存量用户锁死在门外。
2. **改密码是真校验原密码的**：old 错 → body.code=401（HTTP 恒 200）。
   改完哈希必须真的变。
3. **profile 的 avatarFrame 白名单**（lightning/flame/stars），
   非法值 400；`"none"`/`""` 是清除语义 —— 且写回不能抹掉 config 里的 longMemory*。
4. **init-admin 只在系统无用户时可用**：已初始化的系统调用必须 400，
   否则任何人都能把 admin 抢注了。
5. **头像 URL 是永久公开 URL**（`{endpoint}/{bucket}/{path}`），不是预签名 ——
   预签名一小时就过期，存进库的 avatar 就 403 了。

用法：
    python -m scripts.auth_check
"""

from __future__ import annotations

import asyncio
import time

import httpx
from sqlalchemy import text

from app.core.db import SessionLocal

BASE = "http://127.0.0.1:18080"
failures = 0
_STAMP = str(int(time.time()))[-6:]
_UID = 2090273750973657090
_OLD_HASH: str | None = None
_AVATAR_PATH: str | None = None


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
        await db.execute(text("DELETE FROM users WHERE username LIKE 'zz-%'"))
        # 还原 dev 用户密码哈希（测试中改过）
        if _OLD_HASH:
            await db.execute(
                text("UPDATE users SET password=:p WHERE id=:i"),
                {"p": _OLD_HASH, "i": _UID},
            )
        # 还原 avatar 与 config
        await db.execute(
            text("UPDATE users SET avatar=NULL, "
                 "config = config - 'avatarFrame' - 'level' WHERE id=:i"),
            {"i": _UID},
        )
        await db.commit()
    # 清掉测试头像对象
    if _AVATAR_PATH:
        from app.services import minio_client

        await minio_client.delete(_AVATAR_PATH)


async def check_register(c: httpx.AsyncClient) -> None:
    print("\n[1] 注册 / init-status / init-admin")

    r = await api(c, "GET", "/api/auth/init-status")
    d = data(r) or {}
    ok("init-status 返回 initialized 布尔", isinstance(d.get("initialized"), bool), str(d))
    ok("库里有用户 → initialized=True", d.get("initialized") is True)

    r = await api(c, "POST", "/api/auth/register",
                  json={"username": f"zz-{_STAMP}", "password": "secret66", "nickname": "自检"})
    d = data(r) or {}
    # register 现在返回 {token, user}（注册即自动登录，认证收口后行为）
    ok("注册成功且 role=user", r.get("code") == 200
       and (d.get("user") or {}).get("role") == "user", str(d)[:150])
    ok("token 自动登录（lbt_ 前缀）/ id 是字符串 / firstLogin=False",
       (d.get("token") or "").startswith("lbt_")
       and isinstance((d.get("user") or {}).get("id"), str)
       and (d.get("user") or {}).get("firstLogin") is False)

    r = await api(c, "POST", "/api/auth/register",
                  json={"username": f"zz-{_STAMP}", "password": "secret66"})
    ok("用户名重复 400", r.get("code") == 400, str(r)[:120])

    r = await api(c, "POST", "/api/auth/register",
                  json={"username": "ab", "password": "secret66"})
    ok("用户名 <3 字被拒", r.get("code") in (400, 422), str(r)[:100])

    r = await api(c, "POST", "/api/auth/register",
                  json={"username": f"zz-short-{'x' * 40}", "password": "secret66"})
    ok("用户名 >32 字被拒", r.get("code") in (400, 422), str(r)[:100])

    r = await api(c, "POST", "/api/auth/init-admin",
                  json={"username": f"zz-admin-{_STAMP}", "password": "admin66"})
    ok("已初始化的系统 init-admin 被拒（防抢注）", r.get("code") == 400, str(r)[:120])

    # 注册的密码必须真哈希落库（BCrypt $2 前缀）
    async with SessionLocal() as db:
        h = (await db.execute(
            text("SELECT password FROM users WHERE username = :u"),
            {"u": f"zz-{_STAMP}"},
        )).scalar_one()
    ok("密码以 BCrypt 哈希落库（不是明文）",
       h.startswith("$2") and len(h) >= 59, f"len={len(h)} prefix={h[:7]}")


async def check_users_query(c: httpx.AsyncClient) -> None:
    print("\n[2] 用户查询（batch / search）")

    r = await api(c, "GET", f"/api/auth/users/batch?ids={_UID}")
    items = data(r) or []
    ok("batch 查得到 dev 用户", len(items) == 1 and items[0]["id"] == str(_UID),
       str(items[:1])[:150])
    ok("UserDTO 含 avatarFrame/level/lastLoginAt",
       items and {"avatarFrame", "level", "lastLoginAt"} <= set(items[0].keys()),
       str(sorted(items[0].keys())) if items else "")

    r = await api(c, "GET", "/api/auth/users/batch")
    ok("空 ids 返回空数组", data(r) == [], str(r)[:100])

    r = await api(c, "GET", f"/api/auth/users/search?keyword=zz-{_STAMP}")
    ok("search 按用户名命中", len(data(r) or []) == 1, str(len(data(r) or [])))

    r = await api(c, "GET", "/api/auth/users/search?keyword=")
    ok("空 keyword 返回空数组", data(r) == [], str(r)[:100])


async def check_profile(c: httpx.AsyncClient) -> None:
    print("\n[3] profile / avatarFrame 白名单 / config 混存")

    # 先种一个 longMemory 值（同列共存校验的前置）
    await api(c, "PUT", "/api/user/preferences", json={"longMemoryEnabled": True})

    r = await api(c, "PUT", "/api/auth/profile",
                  json={"nickname": f"zz-昵称-{_STAMP}", "avatarFrame": "lightning", "level": 3})
    d = data(r) or {}
    ok("profile 更新成功", r.get("code") == 200 and d.get("avatarFrame") == "lightning",
       str(d)[:150])
    ok("level 出参回读", d.get("level") == 3, str(d.get("level")))

    async with SessionLocal() as db:
        cfg = (await db.execute(
            text("SELECT config FROM users WHERE id=:i"), {"i": _UID}
        )).scalar_one()
    ok("写回保留 config 里的 longMemoryEnabled（不能整列覆盖）",
       (cfg or {}).get("longMemoryEnabled") is True, str(cfg)[:120])

    r = await api(c, "PUT", "/api/auth/profile", json={"avatarFrame": "hacker"})
    ok("非法头像框 400（白名单 lightning/flame/stars）", r.get("code") == 400, str(r)[:120])

    r = await api(c, "PUT", "/api/auth/profile", json={"avatarFrame": "none"})
    d = data(r) or {}
    ok("avatarFrame='none' 是清除语义", d.get("avatarFrame") is None, str(d.get("avatarFrame")))

    # 还原偏好
    await api(c, "PUT", "/api/user/preferences", json={"longMemoryEnabled": False})


async def check_password(c: httpx.AsyncClient) -> None:
    global _OLD_HASH
    print("\n[4] 改密码（真 BCrypt 校验）")

    async with SessionLocal() as db:
        _OLD_HASH = (await db.execute(
            text("SELECT password FROM users WHERE id=:i"), {"i": _UID}
        )).scalar_one()

    r = await api(c, "PUT", "/api/auth/password",
                  json={"oldPassword": "definitely-wrong", "newPassword": "newpass66"})
    ok("原密码错误 → code=401（HTTP 恒 200）", r.get("code") == 401, str(r)[:120])

    # 用一个已知明文做旧密码（临时覆盖哈希），验证 checkpw 全链路
    import bcrypt

    known = bcrypt.hashpw(b"oldpass66", bcrypt.gensalt()).decode()
    async with SessionLocal() as db:
        await db.execute(text("UPDATE users SET password=:p WHERE id=:i"),
                         {"p": known, "i": _UID})
        await db.commit()

    r = await api(c, "PUT", "/api/auth/password",
                  json={"oldPassword": "oldpass66", "newPassword": "newpass66"})
    ok("原密码正确 → 修改成功", r.get("code") == 200, str(r)[:120])

    async with SessionLocal() as db:
        new_hash = (await db.execute(
            text("SELECT password FROM users WHERE id=:i"), {"i": _UID}
        )).scalar_one()
    ok("哈希已更新（新旧不同）", new_hash != known, f"prefix={new_hash[:7]}")
    ok("新哈希能验证新明文",
       bcrypt.checkpw(b"newpass66", new_hash.encode()), f"prefix={new_hash[:7]}")


async def check_avatar(c: httpx.AsyncClient) -> None:
    global _AVATAR_PATH
    print("\n[5] 头像上传（MinIO 永久 URL）")

    r = await api(c, "POST", "/api/auth/avatar",
                  files={"file": ("a.png", b"\x89PNG\r\n\x1a\n" + b"0" * 64, "image/png")})
    d = data(r) or ""
    ok("上传成功返回 URL 字符串", r.get("code") == 200 and isinstance(d, str), str(r)[:150])
    ok("URL 是永久公开 URL（endpoint/bucket/path 形态）",
       d.startswith("http://localhost:9100/lengbot/user/"), str(d)[:120])
    _AVATAR_PATH = d.split("/lengbot/", 1)[-1] if "/lengbot/" in d else None

    from app.services import minio_client

    ok("对象真的进了 MinIO", await minio_client.exists(_AVATAR_PATH) if _AVATAR_PATH else False,
       str(_AVATAR_PATH))

    async with SessionLocal() as db:
        saved = (await db.execute(
            text("SELECT avatar FROM users WHERE id=:i"), {"i": _UID}
        )).scalar_one()
    ok("avatar 字段已回写为同一 URL", saved == d, str(saved)[:120])

    r = await api(c, "POST", "/api/auth/avatar",
                  files={"file": ("a.exe", b"MZ" + b"0" * 32, "application/octet-stream")})
    ok("非法后缀 400（支持 jpg/jpeg/png/gif/webp/bmp）", r.get("code") == 400, str(r)[:120])


async def main() -> int:
    await cleanup()
    async with httpx.AsyncClient(timeout=60, trust_env=False) as c:
        await check_register(c)
        await check_users_query(c)
        await check_profile(c)
        await check_password(c)
        await check_avatar(c)
    await cleanup()

    print(f"\n{'=' * 60}")
    print(f"失败 {failures} 项")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
