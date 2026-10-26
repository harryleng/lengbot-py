"""模型供应商管理的端到端验证（15 个接口）。

**走真实 HTTP**（httpx + ASGITransport），不走 service 直调 —— 因为最关键的
几个契约都发生在**序列化层**，直调 service 根本测不到：

1. `apiKey` 必须完全不出现在响应里（Java 是 WRITE_ONLY）
2. `config` 必须是 **JSON 字符串**，不能是 dict
   （前端 `parseJsonObject(str)` 会直接 `str.trim()`，传对象 → TypeError → 白屏）
3. `id` 必须是字符串（19 位雪花 ID 超过 JS 安全整数）

用法：
    python -m scripts.model_provider_check          # 默认：不花一分钱、不碰外部网络
    python -m scripts.model_provider_check --live   # 额外做真连通性检查（几分钱）

**默认不跑真实网络**：连通性检查和拉模型列表都要打外部 API，既花钱又依赖
网络可用性（国内访问 api.deepseek.com 时通时断）。把它们放进默认路径会让
"回归挂了"和"网断了"混在一起。日常回归跑默认，需要验证凭证时加 --live。

注意：默认路径**会写库**（创建一条 `zz-自检临时供应商` 并验证软删），
跑完会物理删掉，不留垃圾。
"""

from __future__ import annotations

import asyncio
import json
import sys

import httpx

from app.main import app

BASE = "http://test"
failures = 0


def ok(label: str, cond: bool, detail: str = "") -> None:
    global failures
    if not cond:
        failures += 1
    print(f"  {'✅' if cond else '❌'} {label}" + (f"  → {detail}" if detail else ""))


async def api(client: httpx.AsyncClient, method: str, path: str, **kwargs) -> dict:
    resp = await client.request(method, BASE + path, **kwargs)
    body = resp.json()
    return body


def data_of(body: dict) -> object:
    """后端统一包 Result：code/message/data。"""
    assert body.get("code") == 200, f"非 200: {body}"
    return body.get("data")


async def main() -> None:
    live = "--live" in sys.argv
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=BASE, timeout=40) as client:
        # ---------------------------------------------------------------
        print("=" * 70)
        print("1. 静态数据：presets / default-model")
        print("=" * 70)

        presets = data_of(await api(client, "GET", "/api/model-providers/presets"))
        ok("presets 有 11 条", len(presets) == 11, f"{len(presets)} 条")
        first = presets[0]
        need = {"code", "name", "description", "type", "logo", "baseUrl",
                "defaultModelId", "completionsPath", "modelsEndpoint",
                "headersJson", "extraJson"}
        ok("preset 字段齐全", need <= set(first), f"缺 {need - set(first)}")
        ok("headersJson 是字符串", isinstance(first["headersJson"], str),
           type(first["headersJson"]).__name__)

        for t, expect in [("OPENAI", "gpt-4o-mini"), ("DEEPSEEK", "deepseek-chat"),
                          ("DASHSCOPE", "qwen-plus"), ("OLLAMA", "qwen2.5:latest"),
                          ("MIMO", "mimo-v2.5")]:
            got = data_of(await api(client, "GET", f"/api/model-providers/default-model?type={t}"))
            ok(f"default-model({t}) = {expect}", got == expect, str(got))

        # ---------------------------------------------------------------
        print("\n" + "=" * 70)
        print("2. 列表：序列化契约（最关键的三个）")
        print("=" * 70)

        page = data_of(await api(client, "GET", "/api/model-providers?pageNum=1&pageSize=5"))
        ok("分页结构有 records/total", "records" in page and "total" in page,
           f"total={page.get('total')}")
        records = page["records"]
        ok("列表非空（库里有 10 条）", len(records) > 0, f"{len(records)} 条")
        ok("pageSize 生效（只要 5 条）", len(records) <= 5, f"{len(records)} 条")

        # 类型覆盖检查要用**全量**，不能用这一页 5 条 —— 否则库里排在前面的
        # 恰好都是 OPENAI 时，其余 4 种类型全被"跳过"，等于没验。
        all_records = data_of(
            await api(client, "GET", "/api/model-providers?pageNum=1&pageSize=100")
        )["records"]

        if records:
            r = records[0]
            ok("响应里**没有** apiKey 字段", "apiKey" not in r, str(list(r))[:120])
            ok("config 是字符串（不是对象）", isinstance(r["config"], str),
               f"{type(r['config']).__name__}={str(r['config'])[:60]}")
            ok("headersJson 是字符串", isinstance(r["headersJson"], str),
               type(r["headersJson"]).__name__)
            ok("extraJson 是字符串", isinstance(r["extraJson"], str),
               type(r["extraJson"]).__name__)
            ok("id 是字符串", isinstance(r["id"], str), type(r["id"]).__name__)
            # 前端 parseJsonObject 的硬性前提
            try:
                json.loads(r["config"])
                ok("config 能被 JSON.parse（前端前提）", True)
            except (json.JSONDecodeError, TypeError) as exc:
                ok("config 能被 JSON.parse（前端前提）", False, str(exc))

        # ---------------------------------------------------------------
        print("\n" + "=" * 70)
        print("3. 配置字段定义（前端动态表单用）")
        print("=" * 70)

        for t, expect_n in [("OPENAI", 6), ("DASHSCOPE", 4), ("DEEPSEEK", 4),
                            ("OLLAMA", 4), ("MIMO", 6)]:
            # 借库里任意一个该类型的 provider; 没有就跳过
            target = next((x for x in all_records if (x.get("type") or "") == t), None)
            if target is None:
                print(f"  - 跳过 {t}（库里没有该类型的 provider 可借用）")
                continue
            fields = data_of(
                await api(client, "GET", f"/api/model-providers/{target['id']}/config-fields")
            )
            ok(f"{t} config-fields {expect_n} 个", len(fields) == expect_n, f"{len(fields)} 个")
            model_field = next((f for f in fields if f["key"] == "modelId"), None)
            ok(f"{t} 有 modelId 字段", model_field is not None)
            caps = data_of(
                await api(client, "GET",
                          f"/api/model-providers/{target['id']}/model-capabilities")
            )
            ok(f"{t} model-capabilities 是数组", isinstance(caps, list), f"{len(caps)} 个")

        # ---------------------------------------------------------------
        print("\n" + "=" * 70)
        print("4. with-models（ModelSelect 数据源）")
        print("=" * 70)

        wm = data_of(await api(client, "GET", "/api/model-providers/with-models"))
        ok("返回数组", isinstance(wm, list), f"{len(wm)} 组")
        if wm:
            ok("每组有 id/name/type/models", {"id", "name", "type", "models"} <= set(wm[0]),
               str(list(wm[0])))
            ok("provider.id 是字符串", isinstance(wm[0]["id"], str))
            total_models = sum(len(g["models"]) for g in wm)
            ok("模型总数 > 0", total_models > 0, f"{total_models} 个")

        llm_only = data_of(await api(client, "GET", "/api/model-providers/with-models?type=llm"))
        ok("按 llm 过滤后每组都非空",
           all(len(g["models"]) > 0 for g in llm_only), f"{len(llm_only)} 组")
        emb = data_of(await api(client, "GET", "/api/model-providers/with-models?type=embedding"))
        ok("按 embedding 过滤生效",
           all(m["type"] == "embedding" for g in emb for m in g["models"]),
           f"{len(emb)} 组")
        none_type = data_of(
            await api(client, "GET", "/api/model-providers/with-models?type=__nope__")
        )
        ok("不存在的类型返回空数组", none_type == [], str(none_type))

        # ---------------------------------------------------------------
        print("\n" + "=" * 70)
        print("5. CRUD 全流程（创建 → 读 → 改 → 禁用 → 删）")
        print("=" * 70)

        created = data_of(
            await api(
                client, "POST", "/api/model-providers",
                json={
                    "name": "zz-自检临时供应商",
                    "type": "DEEPSEEK",
                    "apiKey": "sk-test-not-real",
                    "baseUrl": "https://api.deepseek.com",
                    "defaultModelId": "deepseek-chat",
                    "config": json.dumps({"temperature": 0.3, "custom": "x"}),
                    "headersJson": "{}",
                    "extraJson": '{"timeout": 30000}',
                },
            )
        )
        pid = created["id"]
        ok("创建成功，返回字符串 id", isinstance(pid, str), str(pid))
        ok("创建响应不含 apiKey", "apiKey" not in created, str(list(created))[:100])
        cfg = json.loads(created["config"])
        ok("defaultModelId 被塞进 config.modelId", cfg.get("modelId") == "deepseek-chat",
           json.dumps(cfg, ensure_ascii=False))
        ok("原 config 的其它键保留", cfg.get("custom") == "x", json.dumps(cfg, ensure_ascii=False))
        ok("extraJson 落成 JSONB 后能读回字符串",
           isinstance(created["extraJson"], str) and "timeout" in created["extraJson"],
           str(created["extraJson"]))

        got = data_of(await api(client, "GET", f"/api/model-providers/{pid}"))
        ok("按 id 读回", got["name"] == "zz-自检临时供应商", str(got.get("name")))

        # 关键：更新时不传 apiKey 应该保留原 key（Java 会清空，这里刻意改安全）
        updated = data_of(
            await api(
                client, "PUT", "/api/model-providers",
                json={
                    "id": pid,
                    "name": "zz-自检临时供应商-改名",
                    "type": "DEEPSEEK",
                    "apiKey": "",          # ← 前端编辑弹窗回显不了 key，只能传空
                    "baseUrl": "https://api.deepseek.com",
                    "defaultModelId": "deepseek-reasoner",
                    "config": "{}",
                    "headersJson": "{}",
                    "extraJson": "{}",
                },
            )
        )
        ok("更新生效", updated["name"].endswith("改名"), str(updated["name"]))
        ok("新 modelId 写进 config",
           json.loads(updated["config"]).get("modelId") == "deepseek-reasoner",
           str(updated["config"]))

        # 直接查库确认 key 没被空串覆盖
        from sqlalchemy import text as sql_text

        from app.core.db import SessionLocal

        async with SessionLocal() as db:
            row = (
                await db.execute(
                    sql_text("SELECT api_key FROM model_provider WHERE id = :id"),
                    {"id": int(pid)},
                )
            ).first()
        ok("传空 apiKey 时保留原 key（刻意偏离 Java）",
           row is not None and row[0] == "sk-test-not-real", str(row[0] if row else None))

        await api(client, "PATCH", f"/api/model-providers/{pid}/status?status=disabled")
        got2 = data_of(await api(client, "GET", f"/api/model-providers/{pid}"))
        ok("禁用生效", got2["status"] == "disabled", str(got2["status"]))

        await api(client, "DELETE", f"/api/model-providers/{pid}")
        after = await api(client, "GET", f"/api/model-providers/{pid}")
        ok("删除后查不到（软删 → 404）", after.get("code") == 404, str(after.get("code")))

        async with SessionLocal() as db:
            row = (
                await db.execute(
                    sql_text("SELECT deleted FROM model_provider WHERE id = :id"),
                    {"id": int(pid)},
                )
            ).first()
        ok("是软删（deleted=1，记录还在）", row is not None and row[0] == 1,
           str(row[0] if row else None))

        # 验证完软删行为后，把测试记录**物理清掉** —— 否则每跑一次自检
        # 库里就多一条 deleted=1 的 "zz-自检临时供应商"，跑几十次就攒一堆垃圾。
        async with SessionLocal() as db:
            await db.execute(
                sql_text(
                    "DELETE FROM model WHERE provider_id IN "
                    "(SELECT id FROM model_provider WHERE name LIKE 'zz-自检%')"
                )
            )
            await db.execute(
                sql_text("DELETE FROM model_provider WHERE name LIKE 'zz-自检%'")
            )
            await db.commit()
        ok("已清理测试记录", True)

        # ---------------------------------------------------------------
        print("\n" + "=" * 70)
        print("6. 错误分支")
        print("=" * 70)

        nf = await api(client, "GET", "/api/model-providers/999999999999999")
        ok("不存在的 id → 404", nf.get("code") == 404, str(nf.get("message"))[:60])

        bad_status = await api(
            client, "PATCH", f"/api/model-providers/{records[0]['id']}/status?status=flying"
        )
        ok("非法 status → 400", bad_status.get("code") == 400,
           str(bad_status.get("message"))[:60])

        empty_check = await api(client, "POST", "/api/model-providers/check",
                               json={"type": "OPENAI", "apiKey": ""})
        ok("缺 apiKey 的连通性检查 → 400", empty_check.get("code") == 400,
           str(empty_check.get("message"))[:60])

        # ---------------------------------------------------------------
        if live:
            print("\n" + "=" * 70)
            print("7. 真实网络：连通性检查 + 拉模型列表（--live）")
            print("=" * 70)

            # 找一个**真的能连通**的供应商来测。
            #
            # 不能按 type=DEEPSEEK 找：库里那 5 条 type=DEEPSEEK/DASHSCOPE/MIMO/OLLAMA
            # 是 Java 初始化时的**模板记录**，全部 status=disabled 且 api_key 为空。
            # 实际在用的 5 个都是 type=OPENAI（走 OpenAI 兼容协议），
            # 其中 baseUrl 指向 api.deepseek.com 的那两个才有有效 key。
            deepseek = next(
                (
                    x for x in all_records
                    if x.get("status") == "active"
                    and "deepseek" in (x.get("baseUrl") or "").lower()
                ),
                None,
            )
            if deepseek is None:
                print("  - 跳过：库里没有启用的 deepseek 兼容供应商（无可用 key 可测）")
            else:
                print(f"     目标: {deepseek['name']} ({deepseek['id']})")
                chk = await api(client, "GET", f"/api/model-providers/{deepseek['id']}/check")
                ok("连通性检查成功", chk.get("code") == 200 and "成功" in str(chk.get("data")),
                   f"code={chk.get('code')} {str(chk.get('data') or chk.get('message'))[:80]}")

                fm = await api(client, "GET", f"/api/model-providers/{deepseek['id']}/fetch-models")
                if fm.get("code") == 200:
                    models = fm["data"]
                    ok("拉到模型列表", len(models) > 0, f"{len(models)} 个")
                    ok("每项有 modelId + type",
                       all("modelId" in m and "type" in m for m in models),
                       str(models[0]) if models else "")
                    ok("type 都在合法枚举内",
                       all(m["type"] in {"llm", "embedding", "rerank", "tts", "stt"}
                           for m in models),
                       str({m["type"] for m in models}))
                    print(f"     样例: {[m['modelId'] for m in models[:4]]}")
                else:
                    ok("拉模型列表（网络问题允许失败，但要有明确错误）", False,
                       str(fm.get("message"))[:100])

            # 表单态检查（不落库）：故意用错 key，必须明确失败
            bad = await api(
                client, "POST", "/api/model-providers/check",
                json={"type": "DEEPSEEK", "apiKey": "sk-obviously-wrong",
                      "baseUrl": "https://api.deepseek.com", "modelId": "deepseek-chat"},
            )
            ok("错 key 的表单检查返回 400 + 上游原文",
               bad.get("code") == 400 and "401" in str(bad.get("message")),
               str(bad.get("message"))[:100])

    print()
    if failures:
        print(f"❌ 失败 {failures} 项")
        raise SystemExit(1)
    print("✅ 全部通过")


if __name__ == "__main__":
    asyncio.run(main())
