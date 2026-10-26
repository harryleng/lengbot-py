"""Skill 20 接口验证（对齐前端 `src/api/skill.js`）。

重点验证这些**静默失败**：

1. **路由顺序**：`/enabled` `/import/*` `/remote/*` 不能被 `/{skill_id}` 吃掉
   （`enabled` 被当 id 转 int 会直接 404，且 FastAPI 不报路由冲突）。
2. **id 出参必须是字符串**。19 位雪花 ID 用 JSON number 传会被 JS 截断后 3 位，
   前端再拿去改/删就打到空记录上。
3. **内置 Skill 不可删 / 不可改**。放过去用户会以为保存成功，实际下次启动被注册器覆盖。
4. **slug 冲突要 409** 而不是静默改名（手工创建路径；导入路径才自动加 -v2）。
5. **路径穿越 `../` 要 403**。Skill 文件路径直接进 MinIO key，`..` 能读到整个 bucket。
6. **二进制出参是 base64**（Java `Result<byte[]>` 的行为），改裸字节前端 `atob` 会炸。
7. **ZIP 顶层目录要被整体剥离**：`zz-x/SKILL.md` + `zz-x/scripts/a.py` 导入后
   必须是 `SKILL.md` + `scripts/a.py`，不能一个在根一个在子目录（Java 的 bug）。

用法：
    python -m scripts.skill_check
"""

from __future__ import annotations

import asyncio
import base64
import io
import sys
import time
import zipfile

import httpx
from sqlalchemy import text

from app.core.db import SessionLocal

BASE = "http://127.0.0.1:18080"
failures = 0
_SKILL_IDS: list[int] = []
_SLUGS: list[str] = []
_STAMP = str(int(time.time()))[-6:]


def ok(label: str, cond: bool, detail: str = "") -> None:
    global failures
    if not cond:
        failures += 1
    print(f"  {'✅' if cond else '❌'} {label}" + (f"  → {detail}" if detail else ""))


async def api(c: httpx.AsyncClient, method: str, path: str, **kw) -> tuple[int, dict]:
    resp = await c.request(method, BASE + path, **kw)
    try:
        return resp.status_code, resp.json()
    except Exception:  # noqa: BLE001
        return resp.status_code, {"code": resp.status_code, "message": resp.text[:200]}


def _skill_zip(slug: str = "zz-imported", with_subdir: bool = True) -> bytes:
    """构造一个带顶层目录、且含子目录的 Skill ZIP。

    子目录是必须的：Java 的 `normalizeZipEntryName` 只剥"一层"的条目，
    顶层目录 + 子目录的 ZIP 会剥一半留一半，文件树散架。这里专门测这个。
    """
    md = (
        "---\n"
        f"name: {slug}\n"
        f"slug: {slug}\n"
        "description: 导入验证用技能\n"
        "version: 1.2.0\n"
        "skill_dependencies: []\n"
        "---\n\n"
        "这是导入验证用的提示词模板正文。\n"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{slug}/SKILL.md", md)
        if with_subdir:
            zf.writestr(f"{slug}/scripts/run.py", "print('hello')\n")
    return buf.getvalue()


async def cleanup() -> None:
    """按 slug 前缀 + 记录 id 双保险清理，防孤儿。"""
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                text("SELECT id, slug FROM skill WHERE slug LIKE 'zz-%' OR name LIKE 'zz-%'")
            )
        ).fetchall()
        for rid, slug in rows:
            _SKILL_IDS.append(rid)
            if slug:
                _SLUGS.append(slug)
        ids = sorted(set(_SKILL_IDS))
        if ids:
            await db.execute(text("DELETE FROM skill WHERE id = ANY(:ids)"), {"ids": ids})
            await db.commit()

    from app.services import minio_client

    for slug in set(_SLUGS):
        await minio_client.list_objects(f"skills/{slug}/")  # 只列不删，下面按前缀删
        for obj in await minio_client.list_objects(f"skills/{slug}/"):
            await minio_client.delete(obj)


async def main() -> None:
    await cleanup()
    async with httpx.AsyncClient(timeout=60, trust_env=False) as c:
        print("\n【1】列表 / 启用列表 / by-agent（路由顺序）")
        code, body = await api(c, "GET", "/api/skills", params={"pageNum": 1, "pageSize": 50})
        ok("GET /skills 200", code == 200 and body.get("code") == 200, str(body.get("message")))
        records = (body.get("data") or {}).get("records", [])
        ok("列表非空且内置排最前", len(records) > 0 and records[0].get("isBuiltin") == 1,
           f"{len(records)} 条，首条 isBuiltin={records[0].get('isBuiltin') if records else None}")
        ok("id 是字符串", all(isinstance(r.get("id"), str) for r in records),
           str(records[0].get("id")) if records else "")

        code, body = await api(c, "GET", "/api/skills/enabled")
        ok("GET /skills/enabled 200（没被 /{id} 吃掉）",
           code == 200 and body.get("code") == 200 and isinstance(body.get("data"), list),
           str(body.get("message")))

        code, body = await api(c, "GET", "/api/skills/by-agent/2090273750973657090")
        ok("GET /skills/by-agent/{id} 200", code == 200 and isinstance(body.get("data"), list))

        print("\n【2】创建 / 更新 / 启用 / 删除")
        slug = f"zz-{_STAMP}"
        name = f"zz-skill-{_STAMP}"
        code, body = await api(c, "POST", "/api/skills", json={
            "slug": slug, "name": name, "displayName": "导入验证技能",
            "description": "用于 Skill 接口验证", "promptTemplate": "你是助手",
            "scope": "global", "sourceType": "upload",
        })
        ok("POST /skills 200", code == 200 and body.get("code") == 200, str(body.get("message")))
        data = body.get("data") or {}
        sid = data.get("id")
        ok("新建 id 是字符串", isinstance(sid, str), str(sid))
        ok(
            "新建默认 version=1.0.0 / isBuiltin=0",
            data.get("version") == "1.0.0" and data.get("isBuiltin") == 0,
        )
        ok("objectPrefix = skills/{slug}/", data.get("objectPrefix") == f"skills/{slug}/",
           str(data.get("objectPrefix")))
        if sid:
            _SKILL_IDS.append(int(sid))
            _SLUGS.append(slug)

        code, body = await api(c, "POST", "/api/skills", json={
            "slug": slug, "name": name, "scope": "global",
        })
        ok("同名创建 → 409", body.get("code") == 409, f"code={body.get('code')}")

        code, body = await api(c, "POST", "/api/skills", json={
            "slug": "Bad_Slug", "name": f"zz-bad-{_STAMP}", "scope": "global",
        })
        ok("非法 slug（大写/下划线）→ 400", body.get("code") == 400, f"code={body.get('code')}")

        code, body = await api(c, "PUT", "/api/skills", json={
            "id": sid, "slug": slug, "name": name, "displayName": "改过的显示名",
            "promptTemplate": "你是另一个助手", "scope": "global",
        })
        ok("PUT /skills 200", code == 200 and (body.get("data") or {}).get("displayName") == "改过的显示名",
           str(body.get("message")))

        code, _ = await api(c, "PUT", f"/api/skills/{sid}/enabled", params={"enabled": False})
        ok("PUT /{id}/enabled?enabled=false 200", code == 200)
        code, body = await api(c, "GET", "/api/skills/enabled")
        ok("禁用后不出现在 enabled 列表",
           all(r.get("id") != sid for r in (body.get("data") or [])))
        await api(c, "PUT", f"/api/skills/{sid}/enabled", params={"enabled": True})

        print("\n【3】内置 Skill 保护")
        code, body = await api(c, "GET", "/api/skills", params={"pageSize": 50})
        builtin = next((r for r in (body.get("data") or {}).get("records", [])
                        if r.get("isBuiltin") == 1), None)
        if builtin:
            code, body = await api(c, "DELETE", f"/api/skills/{builtin['id']}")
            ok("删内置 → 400", body.get("code") == 400, str(body.get("message")))
            code, body = await api(c, "PUT", "/api/skills", json={
                "id": builtin["id"], "name": "hacked", "scope": "global",
            })
            ok("改内置 → 400", body.get("code") == 400, str(body.get("message")))
        else:
            ok("存在内置 Skill 可供测试", False, "库里没有 is_builtin=1 的记录")

        print("\n【4】ZIP 导入（两阶段）+ 顶层目录剥离")
        code, body = await api(
            c, "POST", "/api/skills/import/preview",
            files={"file": ("skill.zip", _skill_zip("zz-imported"), "application/zip")},
        )
        ok("POST /import/preview 200", code == 200 and body.get("code") == 200, str(body.get("message")))
        preview = body.get("data") or {}
        draft_id = preview.get("draftId")
        ok("预览返回 draftId / slug / fileNames",
           bool(draft_id) and preview.get("slug") == "zz-imported"
           and "SKILL.md" in (preview.get("fileNames") or []),
           str(preview.get("fileNames")))
        ok("预览解析出 version=1.2.0", preview.get("version") == "1.2.0", str(preview.get("version")))

        code, body = await api(c, "POST", "/api/skills/import/commit",
                               params={"draftId": draft_id, "targetSlug": "zz-imported"})
        ok("POST /import/commit 200", code == 200 and body.get("code") == 200, str(body.get("message")))
        imported = body.get("data") or {}
        imp_id = imported.get("id")
        ok("导入落库 sourceType=upload", imported.get("sourceType") == "upload")
        if imp_id:
            _SKILL_IDS.append(int(imp_id))
            _SLUGS.append("zz-imported")

        print("\n【5】文件树 / 读写 / 导出")
        code, body = await api(c, "GET", f"/api/skills/{imp_id}/files")
        tree = body.get("data") or []
        names = [n.get("name") for n in tree]
        ok("文件树根节点只有 SKILL.md 和 scripts/（顶层目录已被剥离）",
           set(names) == {"SKILL.md", "scripts"}, str(names))
        scripts_node = next((n for n in tree if n.get("name") == "scripts"), None)
        ok("scripts 是目录且有子节点",
           scripts_node is not None and scripts_node.get("isDir") is True
           and len(scripts_node.get("children") or []) == 1,
           str(scripts_node))

        code, body = await api(c, "GET", f"/api/skills/{imp_id}/file", params={"path": "SKILL.md"})
        ok("GET /file 200 且能 base64 解出 frontmatter",
           code == 200 and b"slug: zz-imported" in base64.b64decode(body.get("data") or ""),
           str(body.get("message")))

        code, body = await api(c, "GET", f"/api/skills/{imp_id}/export")
        raw = base64.b64decode(body.get("data") or "")
        ok("GET /export 返回合法 ZIP", code == 200 and raw[:2] == b"PK", str(body.get("message")))
        if raw[:2] == b"PK":
            with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                ok("ZIP 内路径以 slug/ 开头且含 scripts/run.py",
                   "zz-imported/SKILL.md" in zf.namelist()
                   and "zz-imported/scripts/run.py" in zf.namelist(),
                   str(zf.namelist()))

        code, body = await api(c, "POST", f"/api/skills/{imp_id}/file",
                               json={"path": "notes/todo.txt", "content": "待办", "dir": False})
        ok("POST /file 新建带目录的文件 200", code == 200 and body.get("code") == 200)
        code, body = await api(c, "GET", f"/api/skills/{imp_id}/files")
        names = [n.get("name") for n in (body.get("data") or [])]
        ok("新建后文件树多出 notes/", "notes" in names, str(names))

        code, _ = await api(c, "PUT", f"/api/skills/{imp_id}/file",
                            json={"path": "notes/todo.txt", "content": "改过了"})
        code, body = await api(c, "GET", f"/api/skills/{imp_id}/file", params={"path": "notes/todo.txt"})
        ok("PUT /file 后内容已更新",
           code == 200 and base64.b64decode(body.get("data") or b"").decode("utf-8") == "改过了")

        code, _ = await api(c, "DELETE", f"/api/skills/{imp_id}/file", params={"path": "notes/todo.txt"})
        code, body = await api(c, "GET", f"/api/skills/{imp_id}/files")
        names = [n.get("name") for n in (body.get("data") or [])]
        ok("DELETE /file 后 notes/ 消失", "notes" not in names, str(names))

        print("\n【6】路径穿越 / 空路径")
        code, body = await api(c, "GET", f"/api/skills/{imp_id}/file", params={"path": "../other/SKILL.md"})
        ok("`..` 路径 → 403", code == 403 or body.get("code") == 403, f"http={code} code={body.get('code')}")
        code, body = await api(c, "GET", f"/api/skills/{imp_id}/file", params={"path": "/etc/passwd"})
        ok("绝对路径 → 403", code == 403 or body.get("code") == 403, f"http={code} code={body.get('code')}")

        print("\n【7】远程安装（错误路径，不依赖外网）")
        code, body = await api(c, "POST", "/api/skills/remote/list", json={"source": "  "})
        ok("空 source → 400", body.get("code") == 400, f"code={body.get('code')}")
        code, body = await api(c, "POST", "/api/skills/remote/search", json={"source": ""})
        ok("空关键字搜索 → 200 空列表", code == 200 and body.get("data") == [],
           str(body.get("data"))[:80])
        code, body = await api(c, "POST", "/api/skills/remote/cleanup", params={"draftId": "not-exist"})
        ok("清理不存在的草稿 → 200（幂等）", code == 200 and body.get("code") == 200)

        print("\n【8】删除（软删 + MinIO 目录）")
        code, body = await api(c, "GET", f"/api/skills/{imp_id}/export")
        ok("删除前 export 可用", code == 200)
        code, _ = await api(c, "DELETE", f"/api/skills/{imp_id}")
        code, body = await api(c, "GET", "/api/skills", params={"pageSize": 50, "keyword": "zz-imported"})
        recs = (body.get("data") or {}).get("records", [])
        ok("删除后列表查不到（软删）", all(r.get("id") != imp_id for r in recs), f"{len(recs)} 条")

    print("\n【9】清理测试数据")
    await cleanup()
    print(f"\n{'=' * 60}\nSkill 接口验证：失败 {failures} 项\n{'=' * 60}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        sys.exit(1 if failures else 0)
