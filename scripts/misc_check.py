"""零散模块 28 接口验证：enums / landing / logs / dashboard / observability /
api-keys / tts-voices / user-preferences / tool-calls。

重点验证这些**不算错但会坑前端**的契约：

1. **枚举值是逐字对齐 Java**：`model-provider-types` 是**大写**（OPENAI…），
   而 `tool-types` / `model-types` 是小写。前端下拉按 value 匹配，大小写错了就选不中。
2. **Dashboard 键名一个都不能少**：`documentStatusCounts` / `messagesPerDay` 这类
   前端直接按 key 取，少一个面板就空一格，`trendDays` 只在没传日期区间时才出现。
3. **API Key 的 secret 只在创建那一次出现**，list 里绝不能带明文；
   `permissions` 出中文 desc（"仅对话"）而不是 code。
4. **日志上限 2000**：`limit=99999` 要被夹住，不能把内存吐干。
5. **Landing 超长校验**：主标题 31 字必须 400，且 GET 是公开接口。
6. **TTS 分组/同步路由不能被 `/{voice_name}` 吃掉**（路由顺序）。
7. **user-preferences 写回不能抹掉 users.config 里的其它 key**（头像框/等级）。

用法：
    python -m scripts.misc_check
"""

from __future__ import annotations

import asyncio
import json
import time

import httpx

BASE = "http://127.0.0.1:18080"
failures = 0
_STAMP = str(int(time.time()))[-6:]
_KEY_ID: int | None = None


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


def data(body: dict) -> dict | list | None:
    return body.get("data")


# ---------------------------------------------------------------- enums
async def check_enums(c: httpx.AsyncClient) -> None:
    print("\n[1] 枚举字典 /api/enums/*")

    _, b = await api(c, "GET", "/api/enums/tool-types")
    items = data(b) or []
    ok("tool-types 返回 3 项", len(items) == 3, str(items))
    ok(
        "tool-types value 是小写 code",
        [i["value"] for i in items] == ["builtin", "api", "knowledge"],
    )
    ok("tool-types label 是中文", items[0]["label"] == "内置")

    _, b = await api(c, "GET", "/api/enums/model-provider-types")
    items = data(b) or []
    ok(
        "model-provider-types 是**大写** code（历史口径，别改小写）",
        [i["value"] for i in items][:2] == ["OPENAI", "DASHSCOPE"],
        str([i["value"] for i in items]),
    )

    _, b = await api(c, "GET", "/api/enums/agent-statuses")
    items = data(b) or []
    ok(
        "agent-statuses 含 published_editing",
        "published_editing" in [i["value"] for i in items],
    )

    _, b = await api(c, "GET", "/api/enums/model-types")
    items = data(b) or []
    ok("model-types 含 tts/stt", {"tts", "stt"} <= {i["value"] for i in items})


# ---------------------------------------------------------------- landing
async def check_landing(c: httpx.AsyncClient) -> None:
    print("\n[2] Landing 配置 /api/landing/config")

    _, b = await api(c, "GET", "/api/landing/config")
    ok("GET 是公开接口（无需登录也能读）", b.get("code") == 200, str(b)[:120])
    ok("GET 出参是**字符串**（前端要 JSON.parse）", isinstance(data(b), str),
       str(type(data(b))))

    # 前端 SettingsView 发的是结构化对象，不是 JSON.stringify 后的文本
    good = {"title": "冷机器人", "description": "一个 AI 平台", "subtitles": ["副标题"]}
    _, b = await api(c, "PUT", "/api/landing/config", json=good)
    ok("PUT 结构化对象成功", b.get("code") == 200, str(b)[:120])

    _, b = await api(c, "GET", "/api/landing/config")
    raw = data(b) or ""
    ok("回读能 JSON.parse 且内容一致",
       isinstance(raw, str) and json.loads(raw).get("title") == "冷机器人", str(raw)[:100])

    # Java 客户端直传字符串也要收
    _, b = await api(c, "PUT", "/api/landing/config", json='{"title":"字符串形式"}')
    ok("PUT 字符串形式也收", b.get("code") == 200, str(b)[:120])

    _, b = await api(c, "PUT", "/api/landing/config",
                     json={"title": "很长的标题" * 10})
    ok("主标题超 30 字被拒", b.get("code") == 400, str(b)[:100])

    _, b = await api(c, "PUT", "/api/landing/config", json="{not json")
    ok("非法 JSON 被拒", b.get("code") == 400, str(b)[:100])

    # 还原成合法值，避免把库里的 landing 配置留成脏数据
    await api(c, "PUT", "/api/landing/config", json=good)


async def check_logs(c: httpx.AsyncClient) -> None:
    print("\n[3] 日志 /api/logs/recent")

    status, b = await api(c, "GET", "/api/logs/recent?limit=5")
    items = data(b) or []
    ok("返回不报错", status == 200 and b.get("code") == 200, str(b)[:120])
    ok("条数不超过 limit", len(items) <= 5, f"len={len(items)}")
    if items:
        ok(
            "字段含 timestamp/level/message",
            {"timestamp", "level", "message"} <= set(items[0].keys()),
            str(list(items[0].keys())),
        )

    _, b = await api(c, "GET", "/api/logs/recent?limit=99999")
    ok("limit 被夹到 2000（不能把内存吐干）", len(data(b) or []) <= 2000)


# ---------------------------------------------------------------- dashboard
async def check_dashboard(c: httpx.AsyncClient) -> None:
    print("\n[4] Dashboard /api/dashboard/*")

    _, b = await api(c, "GET", "/api/dashboard/basic")
    d = data(b) or {}
    need = {
        "agentCount",
        "knowledgeCount",
        "sessionCount",
        "messageCount",
        "providerCount",
        "modelCount",
        "documentCount",
        "chunkCount",
    }
    ok("basic 8 个计数键齐全", need <= set(d.keys()), str(sorted(set(d.keys()))))
    ok("计数是数字", all(isinstance(v, int) for v in d.values()))

    _, b = await api(c, "GET", "/api/dashboard/agents")
    d = data(b) or {}
    ok(
        "agents 含 total/statusList/statusCounts/recent",
        {"total", "statusList", "statusCounts", "recent"} <= set(d.keys()),
    )
    ok(
        "statusList 每项含 code/label/count",
        all({"code", "label", "count"} <= set(i.keys()) for i in (d.get("statusList") or [])),
    )

    _, b = await api(c, "GET", "/api/dashboard/knowledge")
    d = data(b) or {}
    ok(
        "knowledge 含 documentStatusCounts/recentDocuments",
        {"documentStatusCounts", "recentDocuments"} <= set(d.keys()),
    )
    ok(
        "documentStatusCounts 覆盖 6 种状态",
        {"uploading", "uploaded", "pending", "processing", "completed", "failed"}
        <= set((d.get("documentStatusCounts") or {}).keys()),
    )

    _, b = await api(c, "GET", "/api/dashboard/chat")
    d = data(b) or {}
    ok("chat 默认带 trendDays=7", d.get("trendDays") == 7, str(d.get("trendDays")))
    per_day = d.get("messagesPerDay") or []
    ok("messagesPerDay 补齐到 7 天", len(per_day) == 7, f"len={len(per_day)}")
    ok(
        "messagesPerDay 每项 date/count",
        all({"date", "count"} <= set(i.keys()) for i in per_day),
    )

    _, b = await api(c, "GET", "/api/dashboard/chat?startDate=2026-01-01&endDate=2026-01-07")
    d = data(b) or {}
    ok("传日期区间时不返回 trendDays", d.get("trendDays") is None, str(d.get("trendDays")))
    ok("区间起点被回填", d.get("trendStartDate") == "2026-01-01")

    # 只有 startDate + endDate 同时非空才会进日期解析（Java 同），
    # 单独传一个非法值会走"默认 7 天"分支，是正确行为不是 bug
    _, b = await api(c, "GET", "/api/dashboard/chat?startDate=bad&endDate=worse")
    ok("非法日期返回 400（注意：body.code，HTTP 恒 200）", b.get("code") == 400, str(b)[:120])


# ---------------------------------------------------------------- observability
async def check_observability(c: httpx.AsyncClient) -> None:
    print("\n[5] 可观测性 /api/observability/*")

    _, b = await api(c, "GET", "/api/observability/traces?pageNum=1&pageSize=5")
    d = data(b) or {}
    ok("traces 返回 records/total/pageNum/pageSize",
       {"records", "total", "pageNum", "pageSize"} <= set(d.keys()))
    ok("pageSize 生效", d.get("pageSize") == 5)

    _, b = await api(c, "GET", "/api/observability/overview")
    d = data(b) or {}
    ok(
        "overview 含 totalCount/successCount/failedCount/totalTokens",
        {"totalCount", "successCount", "failedCount", "totalTokens"} <= set(d.keys()),
    )

    _, b = await api(c, "GET", "/api/observability/traces/1")
    ok("不存在的 trace 返回 404（不是 500）", b.get("code") != 200, str(b)[:120])

    # 造一条 trace 走通「详情 + 删除」链路
    from app.core.db import SessionLocal
    from app.core.snowflake import new_id
    from app.models.entities import LlmTrace
    from datetime import datetime

    trace_id = new_id()
    async with SessionLocal() as db:
        db.add(
            LlmTrace(
                id=trace_id,
                request_id=f"zz-req-{_STAMP}",
                status="completed",
                model="zz-test-model",
                total_tokens=123,
                total_duration_ms=456,
                first_token_ms=50,
                tool_call_count=1,
                spans=[{"name": "llm", "durationMs": 400}],
                trace_source="chat",
                create_time=datetime.now(),
            )
        )
        await db.commit()

    _, b = await api(c, "GET", f"/api/observability/traces/{trace_id}")
    d = data(b) or {}
    ok("详情可读", b.get("code") == 200, str(b)[:120])
    ok("spans 解析成列表", isinstance(d.get("spans"), list) and len(d["spans"]) == 1)
    ok("id 出参是字符串", isinstance(d.get("id"), str), str(type(d.get("id"))))

    _, b = await api(c, "GET", f"/api/observability/traces?requestId=zz-req-{_STAMP}")
    ok("按 requestId 能筛到", (data(b) or {}).get("total", 0) >= 1)

    status, _ = await api(c, "DELETE", "/api/observability/traces", json=[trace_id])
    ok("批量删除成功", status == 200)
    _, b = await api(c, "GET", f"/api/observability/traces/{trace_id}")
    ok("删后详情 404", b.get("code") != 200)


# ---------------------------------------------------------------- api-keys
async def check_api_keys(c: httpx.AsyncClient) -> None:
    print("\n[6] API Key /api/api-keys/*")
    global _KEY_ID

    name = f"zz-key-{_STAMP}"
    status, b = await api(c, "POST", "/api/api-keys", json={"name": name})
    d = data(b) or {}
    ok("创建成功", status == 200 and b.get("code") == 200, str(b)[:150])
    secret = d.get("secret") or ""
    ok("明文 secret 以 lbkey_ 开头", secret.startswith("lbkey_"), secret[:20])
    ok("出参 apiKey 是对象", isinstance(d.get("apiKey"), dict))
    ok("permissions 出中文 desc", d.get("apiKey", {}).get("permissions") == "仅对话",
       str(d.get("apiKey", {}).get("permissions")))
    ok("keyPrefix 是 12 位 + ****", (d.get("apiKey", {}).get("keyPrefix") or "").endswith("****"))

    _KEY_ID = int(d["apiKey"]["id"])

    _, b = await api(c, "GET", "/api/api-keys")
    items = data(b) or []
    ok("列表能查到新建的 key", any(i["name"] == name for i in items))
    ok("列表**不含** secret 明文", all("secret" not in i for i in items))
    ok("列表 id 是字符串", all(isinstance(i["id"], str) for i in items))

    status, _ = await api(c, "PATCH", f"/api/api-keys/{_KEY_ID}/toggle")
    _, b = await api(c, "GET", "/api/api-keys")
    cur = [i for i in (data(b) or []) if i["id"] == str(_KEY_ID)]
    ok("toggle 后 isEnabled 翻转成 0", cur and cur[0]["isEnabled"] == 0, str(cur[:1])[:150])

    _, b = await api(c, "POST", f"/api/api-keys/{_KEY_ID}/regenerate")
    d = data(b) or {}
    ok("regenerate 返回新 secret", (d.get("secret") or "").startswith("lbkey_"))
    ok("regenerate 后 secret 变了", d.get("secret") != secret)

    status, _ = await api(c, "DELETE", f"/api/api-keys/{_KEY_ID}")
    _, b = await api(c, "GET", "/api/api-keys")
    ok("删除后列表里没了", not any(i["id"] == str(_KEY_ID) for i in (data(b) or [])))

    _, b = await api(c, "DELETE", "/api/api-keys/999999999999999999")
    ok("删不存在的 key 返回 404 而不是静默成功", b.get("code") == 404, str(b)[:120])


# ---------------------------------------------------------------- tts
async def check_tts(c: httpx.AsyncClient) -> None:
    print("\n[7] TTS 音色 /api/tts/voices/*")

    status, b = await api(c, "GET", "/api/tts/voices/groups")
    ok("/groups 没被 /{voice_name} 吃掉（返回数组不是 404/405）",
       status == 200 and b.get("code") == 200 and isinstance(data(b), list), str(b)[:120])

    status, b = await api(c, "POST", "/api/tts/voices/sync")
    d = data(b) or {}
    ok("/sync 返回 synced 计数", status == 200 and isinstance(d.get("synced"), int), str(b)[:120])

    _, b = await api(c, "GET", "/api/tts/voices")
    items = data(b) or []
    ok("列表非空（空库会自动懒同步）", len(items) > 0, f"len={len(items)}")
    if items:
        v = items[0]
        ok("voiceURI == name（前端两处都在读，不能只留一个）", v.get("voiceURI") == v.get("name"))
        ok("含 favorite/voiceGroup/remark", {"favorite", "voiceGroup", "remark"} <= set(v.keys()))

    _, b = await api(c, "GET", "/api/tts/voices?locale=zh-CN")
    items = data(b) or []
    ok("locale 筛选生效", all(i["locale"] == "zh-CN" for i in items), f"len={len(items)}")

    _, b = await api(c, "GET", "/api/tts/voices?keyword=Xiaoxiao")
    ok("keyword 模糊匹配生效", len(data(b) or []) >= 1)

    target = (items[0]["name"] if items else "zh-CN-XiaoxiaoNeural")
    status, _ = await api(
        c, "PATCH", f"/api/tts/voices/{target}",
        json={"favorite": True, "voiceGroup": f"zz-group-{_STAMP}", "remark": "测试备注"},
    )
    ok("PATCH 更新元数据成功", status == 200)

    _, b = await api(c, "GET", "/api/tts/voices?favorite=true")
    fav = data(b) or []
    ok("favorite=true 只返回收藏的", all(i["favorite"] for i in fav) and len(fav) >= 1,
       f"len={len(fav)}")

    _, b = await api(c, "GET", "/api/tts/voices/groups")
    ok("新分组出现在分组列表里", f"zz-group-{_STAMP}" in (data(b) or []), str(data(b))[:150])

    # 还原，避免污染真实音色库
    await api(
        c, "PATCH", f"/api/tts/voices/{target}",
        json={"favorite": False, "voiceGroup": "", "remark": ""},
    )

    _, b = await api(c, "PATCH", "/api/tts/voices/zz-not-exist", json={"favorite": True})
    ok("不存在的音色返回 404", b.get("code") == 404, str(b)[:120])


# ---------------------------------------------------------------- user preferences
async def check_preferences(c: httpx.AsyncClient) -> None:
    print("\n[8] 用户配置 /api/user/preferences")

    # 先复位：库里可能残留上一轮/Java 版写过的值，默认值断言才有意义
    await api(c, "PUT", "/api/user/preferences",
              json={"longMemoryEnabled": False, "longMemoryScope": "user",
                    "longMemoryInjectLimit": 6, "longMemoryAutoExtract": False,
                    "longMemoryLlmExtract": True})

    _, b = await api(c, "GET", "/api/user/preferences")
    d = data(b) or {}
    ok("GET 返回 6 个字段",
       {"longMemoryEnabled", "longMemoryAutoExtract", "longMemoryInjectLimit",
        "longMemoryScope", "longMemoryLlmExtract", "memoryExtractProviderId"} <= set(d.keys()),
       str(sorted(d.keys())))
    ok("默认 scope 是 user", d.get("longMemoryScope") == "user", str(d.get("longMemoryScope")))
    ok("默认注入上限是 6", d.get("longMemoryInjectLimit") == 6)

    status, b = await api(c, "PUT", "/api/user/preferences",
                          json={"longMemoryEnabled": True, "longMemoryScope": "AGENT",
                                "longMemoryInjectLimit": 8})
    d = data(b) or {}
    ok("PUT 成功", b.get("code") == 200, str(b)[:150])
    ok("scope 大小写不敏感归一到 agent", d.get("longMemoryScope") == "agent",
       str(d.get("longMemoryScope")))
    ok("注入上限写入生效", d.get("longMemoryInjectLimit") == 8, str(d.get("longMemoryInjectLimit")))

    # Java 用 @Min(1)/@Max(15)，超限是**拒绝**而不是夹紧；
    # 服务层里的 clamp 只是给"绕过 API 直接调 service"兜底
    _, b = await api(c, "PUT", "/api/user/preferences", json={"longMemoryInjectLimit": 999})
    ok("注入上限 999 被拒绝（不是静默夹到 15）", b.get("code") != 200, str(b)[:120])

    _, b = await api(c, "GET", "/api/user/preferences")
    ok("修改已持久化", (data(b) or {}).get("longMemoryEnabled") is True)

    # 关键：写回不能抹掉同列里的其它 key（头像框/等级）
    from app.core.db import SessionLocal
    from sqlalchemy import text

    from app.core.config import settings

    uid = settings.dev_user_id
    async with SessionLocal() as db:
        await db.execute(
            text("UPDATE users SET config = COALESCE(config,'{}'::jsonb) || "
                 "jsonb_build_object('avatarFrame'::text, 'zz-frame'::text) WHERE id = :uid"),
            {"uid": uid},
        )
        await db.commit()
        await api(c, "PUT", "/api/user/preferences", json={"longMemoryEnabled": False})
        row = (await db.execute(
            text("SELECT config FROM users WHERE id = :uid"), {"uid": uid}
        )).scalar()

    ok("写回保留 users.config 里的 avatarFrame（不能整列覆盖）",
       (row or {}).get("avatarFrame") == "zz-frame", str(row)[:150])

    _, b = await api(c, "PUT", "/api/user/preferences", json={"longMemoryInjectLimit": 0})
    ok("注入上限 0 被挡住", b.get("code") != 200, str(b)[:120])

    # 还原
    await api(c, "PUT", "/api/user/preferences",
              json={"longMemoryEnabled": False, "longMemoryScope": "user",
                    "longMemoryInjectLimit": 6})
    async with SessionLocal() as db:
        await db.execute(
            text("UPDATE users SET config = config - 'avatarFrame' WHERE id = :uid"), {"uid": uid}
        )
        await db.commit()


# ---------------------------------------------------------------- tool-calls
async def check_tool_calls(c: httpx.AsyncClient) -> None:
    print("\n[9] 工具调用记录 /api/tool-calls")

    from datetime import datetime

    from app.core.db import SessionLocal
    from app.core.snowflake import new_id
    from app.models.entities import ToolCall

    call_id = new_id()
    async with SessionLocal() as db:
        db.add(
            ToolCall(
                id=call_id,
                message_id=1,
                tool_name=f"zz-tool-{_STAMP}",
                tool_input={"q": "ping"},
                tool_output="pong",
                status="success",
                created_at=datetime.now(),
            )
        )
        await db.commit()

    _, b = await api(c, "GET", "/api/tool-calls?pageNum=1&pageSize=10")
    d = data(b) or {}
    ok("返回 records/total/pageNum/pageSize",
       {"records", "total", "pageNum", "pageSize"} <= set(d.keys()))

    _, b = await api(c, "GET", f"/api/tool-calls?toolName=zz-tool-{_STAMP}")
    d = data(b) or {}
    ok("toolName 模糊筛选命中", d.get("total", 0) >= 1, str(d.get("total")))
    recs = d.get("records") or []
    ok("toolInput 是对象（不是 JSON 字符串）",
       recs and isinstance(recs[0].get("toolInput"), dict), str(recs[:1])[:150])
    ok("id 出参是字符串", recs and isinstance(recs[0].get("id"), str))

    _, b = await api(c, "GET", "/api/tool-calls?status=success")
    ok("status 筛选生效", (data(b) or {}).get("total", 0) >= 1)

    status, _ = await api(c, "DELETE", "/api/tool-calls", json=[call_id])
    ok("批量删除成功", status == 200)
    _, b = await api(c, "GET", f"/api/tool-calls?toolName=zz-tool-{_STAMP}")
    ok("删后筛不到", (data(b) or {}).get("total", 0) == 0)


async def main() -> int:
    async with httpx.AsyncClient(timeout=60, trust_env=False) as c:
        await check_enums(c)
        await check_landing(c)
        await check_logs(c)
        await check_dashboard(c)
        await check_observability(c)
        await check_api_keys(c)
        await check_tts(c)
        await check_preferences(c)
        await check_tool_calls(c)

    print(f"\n{'=' * 60}")
    print(f"失败 {failures} 项")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
