"""Prompt 管理（13 接口）+ SubAgent（16 接口）验证。

重点在三类容易静默出错的地方：

1. **序列化形态**：`modelConfig` / `toolIds` 出入参不是同一种东西
   （入参数组出参字符串、或出入参都是 JSON 字符串）。错了前端 `JSON.parse` 崩。
2. **命名风格并存**：`GET /subagents/runs` 是驼峰（实体分页），
   而 `GET /subagents/runs/{taskId}` 是 snake_case（Java 手拼 Map）。
3. **业务规则**：prompt 版本四步、删除级联、subagent 内置保护、toolIds 清洗。

⚠️ 会写库，测试数据用 `zz-` 前缀，跑完物理删除。

用法：
    python -m scripts.prompt_subagent_check
    python -m scripts.prompt_subagent_check --offline   # 只跑静态断言
"""

from __future__ import annotations

import asyncio
import json
import sys

import httpx
from sqlalchemy import text

from app.core.config import settings
from app.core.db import SessionLocal
from app.main import app

BASE = "http://test"
PREFIX = "zz-自检"
failures = 0


def ok(label: str, cond: bool, detail: str = "") -> None:
    global failures
    if not cond:
        failures += 1
    print(f"  {'✅' if cond else '❌'} {label}" + (f"  → {detail}" if detail else ""))


async def api(c: httpx.AsyncClient, method: str, path: str, **kw) -> dict:
    return (await c.request(method, BASE + path, **kw)).json()


async def cleanup() -> None:
    async with SessionLocal() as db:
        await db.execute(
            text("DELETE FROM prompt_version WHERE prompt_key LIKE :p"), {"p": f"{PREFIX}%"}
        )
        await db.execute(text("DELETE FROM prompt WHERE prompt_key LIKE :p"), {"p": f"{PREFIX}%"})
        await db.execute(
            text("DELETE FROM prompt_build_template WHERE prompt_template_key LIKE :p"),
            {"p": f"{PREFIX}%"},
        )
        await db.execute(text("DELETE FROM subagent WHERE name LIKE :p"), {"p": f"{PREFIX}%"})
        await db.commit()


# ----------------------------------------------------------------------
# Prompt
# ----------------------------------------------------------------------
async def check_prompts(c: httpx.AsyncClient) -> None:
    print("=" * 70)
    print("1. Prompt 列表 + 序列化")
    print("=" * 70)

    body = await api(c, "GET", "/api/prompts?pageNum=1&pageSize=50")
    page = body["data"]
    ok("列表返回", body.get("code") == 200 and "records" in page, f"total={page.get('total')}")
    ok("有真实数据（库里 1 条未删）", page["total"] >= 1, f"{page['total']} 条")

    if page["records"]:
        r = page["records"][0]
        ok("id 是字符串", isinstance(r["id"], str), type(r["id"]).__name__)
        ok("promptKey 是驼峰键", "promptKey" in r, str(list(r))[:100])
        print(f"     样例: {r['promptKey']} latest={r.get('latestVersion')}")

    print("\n" + "=" * 70)
    print("2. 创建 → 详情 → 更新（id 在 query）→ 重名拒绝")
    print("=" * 70)

    created = (await api(c, "POST", "/api/prompts", json={
        "promptKey": f"{PREFIX}_prompt",
        "description": "自检用",
        "tags": "自检,临时",
    }))["data"]
    pid = created["id"]
    ok("创建成功", bool(pid), str(pid))
    ok("新建时 latestVersion 为 null（不是空串）",
       created["latestVersion"] is None, repr(created["latestVersion"]))

    got = (await api(c, "GET", f"/api/prompts/{pid}"))["data"]
    ok("详情读回", got["promptKey"] == f"{PREFIX}_prompt", got["promptKey"])

    r = await api(c, "PUT", f"/api/prompts?id={pid}", json={
        "promptKey": f"{PREFIX}_prompt", "description": "改后的描述", "tags": "改后",
    })
    ok("更新成功（id 在 query）", r.get("code") == 200, str(r.get("message")))
    got2 = (await api(c, "GET", f"/api/prompts/{pid}"))["data"]
    ok("描述已更新", got2["description"] == "改后的描述", str(got2["description"]))
    ok("promptKey 没被改（这是对的，版本靠 key 关联）",
       got2["promptKey"] == f"{PREFIX}_prompt", got2["promptKey"])

    dup = await api(c, "POST", "/api/prompts", json={
        "promptKey": f"{PREFIX}_prompt", "description": "x",
    })
    ok("重名创建 → 400", dup.get("code") == 400, str(dup.get("message"))[:60])

    print("\n" + "=" * 70)
    print("3. 版本四步规则")
    print("=" * 70)

    # ① pre 版本可以创建
    v1 = (await api(c, "POST", "/api/prompts/versions", json={
        "promptKey": f"{PREFIX}_prompt",
        "version": "v1",
        "versionDesc": "初版",
        "template": "你好 {{name}}",
        "variables": json.dumps([{"key": "name", "label": "名字"}]),
        "modelConfig": json.dumps({"temperature": 0.3}),
        "toolConfig": "{}",
        "status": "pre",
    }))["data"]
    ok("pre 版本创建成功", v1["version"] == "v1", v1.get("version"))
    ok("status 是 pre", v1["status"] == "pre", v1["status"])
    ok("variables 出参是 JSON 字符串", isinstance(v1["variables"], str),
       f"{type(v1['variables']).__name__}={v1['variables'][:50]}")
    ok("variables 是数组（解析后）", isinstance(json.loads(v1["variables"]), list),
       v1["variables"][:60])
    ok("modelConfig 出参是 JSON 字符串且能解析",
       isinstance(v1["modelConfig"], str) and json.loads(v1["modelConfig"])["temperature"] == 0.3,
       v1["modelConfig"])
    ok("id 是字符串", isinstance(v1["id"], str), type(v1["id"]).__name__)

    # ② latest_version 被同步
    p = (await api(c, "GET", f"/api/prompts/{pid}"))["data"]
    ok("prompt.latestVersion 同步成 v1", p["latestVersion"] == "v1", str(p["latestVersion"]))

    # ③ 同 key+version 的 pre 可覆盖（旧记录被软删）
    await api(c, "POST", "/api/prompts/versions", json={
        "promptKey": f"{PREFIX}_prompt", "version": "v1", "template": "改过的模板",
        "status": "pre",
    })
    vs = (await api(c, "GET", f"/api/prompts/{PREFIX}_prompt/versions"))["data"]
    same_ver = [x for x in vs if x["version"] == "v1"]
    ok("同 key+version 的 pre 被覆盖（列表只剩 1 条）", len(same_ver) == 1,
       f"{len(same_ver)} 条")
    ok("覆盖后是最新内容", same_ver[0]["template"] == "改过的模板",
       same_ver[0]["template"])

    # ④ 发正式版
    rel = (await api(c, "POST", "/api/prompts/versions", json={
        "promptKey": f"{PREFIX}_prompt", "version": "v1", "template": "正式版内容",
        "status": "release",
    }))["data"]
    ok("release 版本创建成功", rel["status"] == "release", rel["status"])

    # ⑤ 同版本再发 release → 拒绝
    dup_rel = await api(c, "POST", "/api/prompts/versions", json={
        "promptKey": f"{PREFIX}_prompt", "version": "v1", "template": "x",
        "status": "release",
    })
    ok("重复发正式版 → 400", dup_rel.get("code") == 400, str(dup_rel.get("message"))[:60])

    # ⑥ 版本详情
    vd = (await api(
        c, "GET", f"/api/prompts/versions/detail?promptKey={PREFIX}_prompt&version=v1"
    ))["data"]
    ok("版本详情读回", vd["version"] == "v1", vd.get("version"))
    ok("详情里 release 优先（status=release）", vd["status"] == "release", vd["status"])

    # ⑦ 不存在的 prompt 建版本
    nf = await api(c, "POST", "/api/prompts/versions", json={
        "promptKey": "no_such_key_zz", "version": "v1", "template": "x",
    })
    ok("给不存在的 prompt 建版本 → 400", nf.get("code") == 400, str(nf.get("message"))[:60])

    # ⑧ 非法 JSON
    bad = await api(c, "POST", "/api/prompts/versions", json={
        "promptKey": f"{PREFIX}_prompt", "version": "v2", "template": "x",
        "modelConfig": "{not json",
    })
    ok("非法 JSON 的 modelConfig → 400 且说明原因",
       bad.get("code") == 400 and "JSON" in str(bad.get("message")),
       str(bad.get("message"))[:80])

    print("\n" + "=" * 70)
    print("4. 删除级联版本")
    print("=" * 70)

    r = await api(c, "DELETE", f"/api/prompts/{pid}")
    ok("删除成功", r.get("code") == 200, str(r.get("message")))
    ok("返回级联条数", "级联" in str(r.get("data")), str(r.get("data")))

    after = await api(c, "GET", f"/api/prompts/{pid}")
    ok("删除后详情 404", after.get("code") == 404, str(after.get("code")))

    vs_after = (await api(c, "GET", f"/api/prompts/{PREFIX}_prompt/versions"))["data"]
    ok("版本也被软删（列表为空）", vs_after == [], f"{len(vs_after)} 条")

    async with SessionLocal() as db:
        raw = (
            await db.execute(
                text("SELECT count(*) FROM prompt_version "
                     "WHERE prompt_key = :k AND deleted = 0"),
                {"k": f"{PREFIX}_prompt"},
            )
        ).scalar()
    ok("库里确实没有未删版本", raw == 0, str(raw))

    print("\n" + "=" * 70)
    print("5. 构建模板")
    print("=" * 70)

    tpls = (await api(c, "GET", "/api/prompts/templates"))["data"]
    ok("模板列表（库里 17 条）", len(tpls) == 17, f"{len(tpls)} 条")
    if tpls:
        ok("id 是字符串", isinstance(tpls[0]["id"], str))
        ok("modelConfig 是字符串", isinstance(tpls[0]["modelConfig"], str),
           type(tpls[0]["modelConfig"]).__name__)
        key0 = tpls[0]["promptTemplateKey"]
        one = (await api(c, "GET", f"/api/prompts/templates/{key0}"))["data"]
        ok("按 key 取模板", one["promptTemplateKey"] == key0, one["promptTemplateKey"])

    nt = (await api(c, "POST", "/api/prompts/templates", json={
        "promptTemplateKey": f"{PREFIX}_tpl",
        "templateDesc": "自检模板",
        "template": "模板内容 {{x}}",
        "variables": '[{"key":"x"}]',
        "modelConfig": json.dumps({"topP": 0.9}),
        "toolConfig": "{}",
        "tags": "自检",
    }))["data"]
    ok("模板创建成功", nt["promptTemplateKey"] == f"{PREFIX}_tpl", nt.get("promptTemplateKey"))
    ok("variables 原样字符串（这列是 varchar 不是 jsonb）",
       nt["variables"] == '[{"key":"x"}]', nt["variables"])

    dup_t = await api(c, "POST", "/api/prompts/templates", json={
        "promptTemplateKey": f"{PREFIX}_tpl", "template": "x",
    })
    ok("模板重名 → 400", dup_t.get("code") == 400, str(dup_t.get("message"))[:50])

    r = await api(c, "PUT", f"/api/prompts/templates?id={nt['id']}", json={
        "promptTemplateKey": f"{PREFIX}_tpl",
        "templateDesc": "改过",
        "template": "改过的模板",
        "modelConfig": json.dumps({"topP": 0.5}),
        "toolConfig": "{}",
    })
    ok("模板更新成功（id 在 query）", r.get("code") == 200, str(r.get("message")))
    got_t = (await api(c, "GET", f"/api/prompts/templates/{PREFIX}_tpl"))["data"]
    ok("模板内容已更新", got_t["template"] == "改过的模板", got_t["template"])
    ok("modelConfig 已更新", json.loads(got_t["modelConfig"])["topP"] == 0.5,
       got_t["modelConfig"])

    r = await api(c, "DELETE", f"/api/prompts/templates/{nt['id']}")
    ok("模板删除成功", r.get("code") == 200, str(r.get("message")))
    gone = await api(c, "GET", f"/api/prompts/templates/{PREFIX}_tpl")
    ok("删除后 404", gone.get("code") == 404, str(gone.get("code")))


# ----------------------------------------------------------------------
# SubAgent
# ----------------------------------------------------------------------
async def check_subagents(c: httpx.AsyncClient) -> None:
    print("\n" + "=" * 70)
    print("6. SubAgent 列表 + 序列化（toolIds 出入参不对称）")
    print("=" * 70)

    page = (await api(c, "GET", "/api/subagents?pageNum=1&pageSize=100"))["data"]
    ok("列表返回", "records" in page, f"total={page.get('total')}")
    ok("库里有 4 个", page["total"] >= 4, f"{page['total']} 个")
    recs = page["records"]
    if recs:
        s = recs[0]
        ok("id 是字符串", isinstance(s["id"], str))
        ok("toolIds 出参是**字符串**（前端 JSON.parse）",
           isinstance(s["toolIds"], str), type(s["toolIds"]).__name__)
        ok("toolIds 能被 JSON.parse", isinstance(json.loads(s["toolIds"]), list),
           s["toolIds"][:60])
        ok("enabled 是数字（不是 bool）", isinstance(s["enabled"], int),
           f"{s['enabled']} ({type(s['enabled']).__name__})")
        ok("isBuiltin 是数字", isinstance(s["isBuiltin"], int), str(s["isBuiltin"]))
        ok("字段是驼峰", "displayName" in s and "systemPrompt" in s, str(list(s))[:100])

    enabled = (await api(c, "GET", "/api/subagents/enabled"))["data"]
    ok("enabled 只返回启用中的", all(x["enabled"] == 1 for x in enabled), f"{len(enabled)} 个")
    ok("/enabled 没被 /{id} 吃掉（不 422）", isinstance(enabled, list))

    print("\n" + "=" * 70)
    print("7. 创建 → 更新 → 启停 → 删除")
    print("=" * 70)

    # 借一个真实工具 id 验证 toolIds 清洗
    tools = (await api(c, "GET", "/api/tools?pageSize=2"))["data"]["records"]
    real_tool = tools[0]["id"] if tools else None

    created = (await api(c, "POST", "/api/subagents", json={
        "name": f"{PREFIX}_sa",
        "displayName": "自检子代理",
        "description": "自检用",
        "systemPrompt": "你是一个测试子代理",
        "toolIds": [real_tool, "999999999999999999"] if real_tool else [],
        "modelId": None,
        "enabled": True,
    }))["data"]
    sid = created["id"]
    ok("创建成功", bool(sid), str(sid))
    ok("新建的一律非内置", created["isBuiltin"] == 0, str(created["isBuiltin"]))
    ok("enabled=1", created["enabled"] == 1, str(created["enabled"]))

    ids = json.loads(created["toolIds"])
    ok("悬空 toolId 被剔除（只剩真实工具）",
       real_tool in ids and "999999999999999999" not in ids, str(ids))

    upd = (await api(c, "PUT", "/api/subagents", json={
        "id": sid,
        "name": f"{PREFIX}_sa",
        "displayName": "自检子代理-改名",
        "description": "改过",
        "systemPrompt": "改过的提示词",
        "toolIds": [],
        "enabled": False,
    }))["data"]
    ok("更新生效", upd["displayName"].endswith("改名"), upd["displayName"])
    ok("toolIds 清空后是 []", json.loads(upd["toolIds"]) == [], upd["toolIds"])
    ok("enabled=false 落库为 0", upd["enabled"] == 0, str(upd["enabled"]))

    await api(c, "PUT", f"/api/subagents/{sid}/enabled?enabled=true")
    ok("启停接口生效",
       (await api(c, "GET", f"/api/subagents/{sid}"))["data"]["enabled"] == 1)

    # 重名
    dup = await api(c, "POST", "/api/subagents", json={
        "name": f"{PREFIX}_sa", "displayName": "x", "description": "x",
        "systemPrompt": "x",
    })
    ok("重名创建 → 400", dup.get("code") == 400, str(dup.get("message"))[:60])

    # 内置保护
    builtin = next((x for x in recs if x["isBuiltin"] == 1), None)
    if builtin:
        r = await api(c, "DELETE", f"/api/subagents/{builtin['id']}")
        ok("内置不可删", r.get("code") == 400, str(r.get("message"))[:40])

        # 内置只能改模型配置：发一个改名字的请求，名字应该不变
        before = builtin["name"]
        r = await api(c, "PUT", "/api/subagents", json={
            "id": builtin["id"],
            "name": "想改内置名字",
            "displayName": "想改",
            "description": "想改",
            "systemPrompt": "想改",
            "toolIds": [],
        })
        ok("内置更新不报错", r.get("code") == 200, str(r.get("message"))[:40])
        after = (await api(c, "GET", f"/api/subagents/{builtin['id']}"))["data"]
        ok("内置的名称/提示词没被改（只有模型可改）",
           after["name"] == before and after["systemPrompt"] == builtin["systemPrompt"],
           f"{after['name']} / {after['systemPrompt'][:20]}")
    else:
        print("  - 跳过内置保护（库里没有 is_builtin=1 的）")

    r = await api(c, "DELETE", f"/api/subagents/{sid}")
    ok("删除成功", r.get("code") == 200, str(r.get("message")))
    gone = await api(c, "GET", f"/api/subagents/{sid}")
    ok("删除后 404", gone.get("code") == 404, str(gone.get("code")))

    print("\n" + "=" * 70)
    print("8. 运行记录（snake_case，taskId = request_id）")
    print("=" * 70)

    # 找一条属于 dev 用户的会话（有子代理任务历史）
    async with SessionLocal() as db:
        row = (
            await db.execute(
                text(
                    "SELECT r.parent_session_id, r.request_id, r.batch_id "
                    "FROM subagent_run r JOIN chat_session cs ON cs.id = r.parent_session_id "
                    "WHERE cs.user_id = :u AND cs.deleted = 0 AND r.request_id IS NOT NULL "
                    "ORDER BY r.id DESC LIMIT 1"
                ),
                {"u": settings.dev_user_id},
            )
        ).mappings().first()

    if row is None:
        print("  - 跳过：dev 用户没有子代理运行记录")
        return

    sess = int(row["parent_session_id"])
    task_id = row["request_id"]
    batch_id = row["batch_id"]

    runs = (await api(c, "GET", f"/api/subagents/runs?sessionId={sess}&pageNum=1&pageSize=5"))["data"]
    ok("/runs 返回分页", "records" in runs, f"total={runs.get('total')}")
    if runs["records"]:
        rr = runs["records"][0]
        ok("/runs 是**驼峰**（实体分页）", "requestId" in rr and "taskId" not in rr,
           str(list(rr))[:90])
        ok("/runs 的 id 是字符串", isinstance(rr["id"], str))

    summary = (await api(c, "GET", f"/api/subagents/runs/summary?sessionId={sess}&limit=5"))["data"]
    ok("/runs/summary 没被 /runs/{taskId} 吃掉", isinstance(summary, list), str(type(summary)))
    if summary:
        s0 = summary[0]
        ok("summary 是 **snake_case**", "task_id" in s0 and "status_label" in s0,
           str(list(s0))[:90])
        ok("summary 的 task_id 是 request_id", s0["task_id"] == task_id, s0["task_id"])
        ok("cancel_requested 是 bool", isinstance(s0["cancel_requested"], bool),
           type(s0["cancel_requested"]).__name__)
        ok("有 progress_summary", bool(s0.get("progress_summary")), str(s0.get("progress_summary")))

    detail = (await api(c, "GET", f"/api/subagents/runs/{task_id}?sessionId={sess}"))["data"]
    ok("任务详情读回", detail.get("task_id") == task_id, str(detail.get("task_id")))
    ok("详情带 status_label", bool(detail.get("status_label")), str(detail.get("status_label")))

    # 随便传一个不存在的 taskId 应该 404（顺带确认不是按主键 id 查的）
    wrong = await api(c, "GET", f"/api/subagents/runs/2099999999999?sessionId={sess}")
    ok("乱传 taskId → 404", wrong.get("code") == 404, str(wrong.get("code")))

    events = (await api(c, "GET", f"/api/subagents/runs/{task_id}/events?sessionId={sess}&limit=5"))["data"]
    ok("事件接口返回结构", {"task_id", "events", "next_cursor"} <= set(events), str(list(events)))
    if events["events"]:
        e0 = events["events"][0]
        ok("事件字段是 snake_case（create_time）", "create_time" in e0, str(list(e0)))
        ok("cursor 是字符串", isinstance(e0["cursor"], str), type(e0["cursor"]).__name__)

        # 游标翻页：用 next_cursor 再拉一次，应该拿不到重复
        cur = events["next_cursor"]
        page2 = (await api(
            c, "GET", f"/api/subagents/runs/{task_id}/events?sessionId={sess}&cursor={cur}&limit=5"
        ))["data"]
        ids1 = {e["cursor"] for e in events["events"]}
        ids2 = {e["cursor"] for e in page2["events"]}
        ok("游标翻页不重复", not (ids1 & ids2), f"重叠 {ids1 & ids2}")

    thread = (await api(c, "GET", f"/api/subagents/runs/{task_id}/thread?sessionId={sess}"))["data"]
    ok("线程接口 available=false（Python 无执行器）",
       thread.get("available") is False, str(thread.get("available")))
    ok("线程 messages 是空数组", thread.get("messages") == [], str(thread.get("messages")))

    if batch_id:
        batch = (await api(c, "GET", f"/api/subagents/batches/{batch_id}?sessionId={sess}"))["data"]
        ok("批次详情返回", batch.get("batch_id") == batch_id, str(batch.get("batch_id")))
        ok("批次带计数", "total_count" in batch and "results" in batch, str(list(batch))[:90])

    # 越权：换一个 sessionId 必须 404
    cross = await api(c, "GET", f"/api/subagents/runs/{task_id}?sessionId=1")
    ok("越权（别人的 sessionId）→ 404", cross.get("code") == 404, str(cross.get("code")))

    print("\n" + "=" * 70)
    print("9. 取消（只写标记）")
    print("=" * 70)

    # 找一个未终结的任务来测；没有就跳过（已完成的任务不该被标成取消中）
    async with SessionLocal() as db:
        open_row = (
            await db.execute(
                text(
                    "SELECT r.parent_session_id, r.request_id FROM subagent_run r "
                    "JOIN chat_session cs ON cs.id = r.parent_session_id "
                    "WHERE cs.user_id = :u AND r.status IN ('pending','submitted','running') "
                    "ORDER BY r.id DESC LIMIT 1"
                ),
                {"u": settings.dev_user_id},
            )
        ).mappings().first()

    if open_row is None:
        done = await api(c, "POST", f"/api/subagents/runs/{task_id}/cancel?sessionId={sess}")
        ok("已终结任务取消返回 affected=0（不假报成功）",
           done.get("code") == 200 and done["data"]["affected"] == 0,
           str(done.get("data")))
    else:
        r = await api(
            c, "POST",
            f"/api/subagents/runs/{open_row['request_id']}/cancel?sessionId={open_row['parent_session_id']}",
        )
        ok("取消未终结任务 affected>=1",
           r.get("code") == 200 and r["data"]["affected"] >= 1, str(r.get("data")))


async def main() -> None:
    await cleanup()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=BASE, timeout=60) as c:
        await check_prompts(c)
        await check_subagents(c)
    await cleanup()
    print("\n已清理测试数据")

    print()
    if failures:
        print(f"❌ 失败 {failures} 项")
        raise SystemExit(1)
    print("✅ 全部通过")


if __name__ == "__main__":
    if "--offline" in sys.argv:
        print("离线模式：当前脚本所有断言都需要库，直接跑全量即可")
    asyncio.run(main())
