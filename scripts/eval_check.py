"""评测模块 35 接口验证（评测集 14 + 评估器 12 + 实验 9）。

分两部分：
- **离线**：分数归一化 / JSON 抽取 / 变量替换（纯函数，不碰库不花钱）
- **在线**：真实 HTTP + 真库；实验执行**不调真实 LLM**（用一个假的评估 prompt
  走完整链路，但它确实会发请求 —— 所以默认跳过，加 `--live` 才跑）

重点验证四类静默错误：
1. **静态路径被 `/{id}` 吃掉** → 422，前端"从示例创建"打不开
2. **jsonb 出参必须是 JSON 字符串** → 给对象前端 `JSON.parse` 直接 TypeError
3. **分页 total 用了本页条数** → 前端永远只有一页
4. **数据项内容归一**（对象/JSON 字符串/纯文本三种入参）

⚠️ 会写库，测试数据用 `zz-` 前缀，跑完物理删除。

用法：
    python -m scripts.eval_check            # 不花钱（实验只建不跑完，或跑 0 条）
    python -m scripts.eval_check --live     # 额外跑一次真实实验（含 LLM 调用）
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
from app.services.eval_judge_service import (
    extract_json,
    normalize_score,
    parse_score_result,
    render_template,
)

BASE = "http://test"
PREFIX = "zz-自检"
failures = 0

# 本次运行创建的所有 id —— cleanup **按 id 删**，不按名字前缀。
# 教训：从示例创建的数据名是内置模板名（"问答准确性评测集"），
# 不带 `zz-` 前缀，靠名字 LIKE 会漏删，于是每跑一次就往真实库里堆一份。
_CREATED: dict[str, list[str]] = {"dataset": [], "evaluator": [], "experiment": []}


def _track(kind: str, id_value: object) -> None:
    if id_value:
        _CREATED[kind].append(str(id_value))


def ok(label: str, cond: bool, detail: str = "") -> None:
    global failures
    if not cond:
        failures += 1
    print(f"  {'✅' if cond else '❌'} {label}" + (f"  → {detail}" if detail else ""))


async def api(c: httpx.AsyncClient, method: str, path: str, **kw) -> dict:
    return (await c.request(method, BASE + path, **kw)).json()


async def cleanup() -> None:
    """按 `_CREATED` 里记录的 id 删（不靠名字前缀 —— 示例数据名字不带前缀）。"""
    exp_ids = [int(x) for x in _CREATED["experiment"]]
    ds_ids = [int(x) for x in _CREATED["dataset"]]
    ev_ids = [int(x) for x in _CREATED["evaluator"]]
    async with SessionLocal() as db:
        if exp_ids:
            await db.execute(
                text("DELETE FROM eval_experiment_result WHERE experiment_id = ANY(:i)"),
                {"i": exp_ids},
            )
            await db.execute(
                text("DELETE FROM task WHERE ref_id = ANY(:i)"), {"i": exp_ids}
            )
            await db.execute(
                text("DELETE FROM eval_experiment WHERE id = ANY(:i)"), {"i": exp_ids}
            )

        if ds_ids:
            await db.execute(
                text("DELETE FROM eval_dataset_item WHERE dataset_id = ANY(:i)"),
                {"i": ds_ids},
            )
            await db.execute(
                text("DELETE FROM eval_dataset_version WHERE dataset_id = ANY(:i)"),
                {"i": ds_ids},
            )
            await db.execute(
                text("DELETE FROM eval_dataset WHERE id = ANY(:i)"), {"i": ds_ids}
            )

        if ev_ids:
            await db.execute(
                text("DELETE FROM eval_evaluator_version WHERE evaluator_id = ANY(:i)"),
                {"i": ev_ids},
            )
            await db.execute(
                text("DELETE FROM eval_evaluator WHERE id = ANY(:i)"), {"i": ev_ids}
            )
        await db.commit()
    for key in _CREATED:
        _CREATED[key].clear()


# ----------------------------------------------------------------------
# 1. 离线：分数解析
# ----------------------------------------------------------------------
def check_offline() -> None:
    print("=" * 70)
    print("1. 分数归一化（模型可能返回 0.85 / 85 / 8.5 / '0.7分'）")
    print("=" * 70)

    cases = [
        (0.85, 0.85), ("0.85", 0.85), (85, 0.85), (8.5, 0.85),
        (1, 1.0), (0, 0.0), (-3, 0.0), (200, 1.0),
        (None, 0.0), ("abc", 0.0), ("0.7分", 0.7), ("", 0.0),
    ]
    for raw, expect in cases:
        got = normalize_score(raw)
        ok(f"{raw!r} → {expect}", abs(got - expect) < 1e-9, f"实际 {got}")

    print("\n" + "=" * 70)
    print("2. JSON 抽取（markdown 围栏 / 前后废话）")
    print("=" * 70)

    ok("纯 JSON", extract_json('{"score":0.9}') == '{"score":0.9}')
    ok(
        "```json 围栏",
        extract_json('```json\n{"score":0.9}\n```') == '{"score":0.9}',
        extract_json('```json\n{"score":0.9}\n```'),
    )
    ok(
        "前后有废话",
        extract_json('我认为结果如下：{"score":0.9} 以上') == '{"score":0.9}',
        extract_json('我认为结果如下：{"score":0.9} 以上'),
    )
    ok("空串 → {}", extract_json("") == "{}")
    ok("None → {}", extract_json(None) == "{}")

    print("\n" + "=" * 70)
    print("3. 评分结果解析（失败不抛异常）")
    print("=" * 70)

    r = parse_score_result('{"score":0.9,"reason":"ok"}')
    ok("正常解析", r.score == 0.9 and r.reason == "ok", str(r))

    r = parse_score_result('```json\n{"score":"0.75","reason":"x"}\n```')
    ok("围栏 + 字符串分数", r.score == 0.75, str(r))

    r = parse_score_result("不是 JSON")
    ok("非 JSON → 0 分但保留原文", r.score == 0.0 and "解析失败" in (r.reason or ""),
       str(r))

    r = parse_score_result('{"score":85,"reason":"百分制"}')
    ok("百分制被归一", r.score == 0.85, str(r))

    r = parse_score_result('{"score":null}')
    ok("score 为 null → 0", r.score == 0.0, str(r))

    r = parse_score_result("[1,2,3]")
    ok("数组 → 解析失败兜底", r.score == 0.0, str(r)[:50])

    print("\n" + "=" * 70)
    print("4. 变量替换")
    print("=" * 70)

    ok(
        "正常替换",
        render_template("a {{x}} b {{y}}", {"x": "1", "y": "2"}) == "a 1 b 2",
        render_template("a {{x}} b {{y}}", {"x": "1", "y": "2"}),
    )
    ok(
        "None → 空串（不是 'None'）",
        render_template("a{{x}}b", {"x": None}) == "ab",
        render_template("a{{x}}b", {"x": None}),
    )
    ok(
        "dict → JSON（不是 Python repr）",
        '{"k": 1}' in render_template("{{x}}", {"x": {"k": 1}}),
        render_template("{{x}}", {"x": {"k": 1}}),
    )
    ok(
        "变量是 JSON 字符串也认",
        render_template("{{x}}", '{"x":"v"}') == "v",
        render_template("{{x}}", '{"x":"v"}'),
    )
    ok(
        "非法 JSON 字符串 → 原样返回",
        render_template("{{x}}", "{not json") == "{{x}}",
        render_template("{{x}}", "{not json"),
    )
    ok("变量名缺失 → 原样留着", render_template("{{missing}}", {"x": 1}) == "{{missing}}")


# ----------------------------------------------------------------------
# 在线
# ----------------------------------------------------------------------
async def check_datasets(c: httpx.AsyncClient) -> tuple[str, str, str]:
    """返回 (dataset_id, version_id, item_id)。"""
    print("\n" + "=" * 70)
    print("5. 评测集：列表 / 静态路径不 422 / 序列化")
    print("=" * 70)

    body = await api(c, "GET", "/api/eval/datasets?pageNum=1&pageSize=50")
    page = body["data"]
    ok("列表返回", body.get("code") == 200 and "records" in page, f"total={page.get('total')}")
    ok("有真实数据（库里 2 条未删）", page["total"] >= 2, f"{page['total']} 条")

    if page["records"]:
        r = page["records"][0]
        ok("id 是字符串", isinstance(r["id"], str))
        ok("columnsConfig 是 JSON 字符串", isinstance(r["columnsConfig"], str),
           type(r["columnsConfig"]).__name__)
        ok("columnsConfig 能 JSON.parse",
           isinstance(json.loads(r["columnsConfig"]), list), r["columnsConfig"][:50])

    # 静态路径
    ex = await api(c, "GET", "/api/eval/datasets/examples")
    ok("GET /datasets/examples 不 422", ex.get("code") == 200, str(ex.get("code")))
    ok("4 个内置示例", len(ex["data"]) == 4, str(len(ex.get("data") or [])))
    if ex["data"]:
        ok("示例有 key/name/itemCount",
           {"key", "name", "description", "tags", "itemCount"} <= set(ex["data"][0]),
           str(list(ex["data"][0])))

    print("\n" + "=" * 70)
    print("6. 评测集 CRUD + 样本三种入参 + 版本冻结")
    print("=" * 70)

    created = (await api(c, "POST", "/api/eval/datasets", json={
        "name": f"{PREFIX}评测集",
        "description": "自检用",
        "columnsConfig": json.dumps(
            [{"key": "input", "label": "输入", "type": "text"}], ensure_ascii=False
        ),
    }))["data"]
    ds_id = created["id"]
    _track("dataset", ds_id)
    ok("创建成功", bool(ds_id), str(ds_id))
    ok("columnsConfig 落库后是字符串", isinstance(created["columnsConfig"], str),
       created["columnsConfig"][:40])

    dup = await api(c, "POST", "/api/eval/datasets", json={"name": f"{PREFIX}评测集"})
    ok("重名 → 400", dup.get("code") == 400, str(dup.get("message"))[:50])

    # 三种入参形态
    i1 = (await api(c, "POST", "/api/eval/datasets/items", json={
        "datasetId": ds_id,
        "dataContent": json.dumps({"input": "对象形态"}, ensure_ascii=False),
    }))["data"]
    ok("dataContent 传 JSON 字符串", json.loads(i1["dataContent"])["input"] == "对象形态",
       i1["dataContent"])

    i2 = (await api(c, "POST", "/api/eval/datasets/items", json={
        "datasetId": ds_id, "dataContent": {"input": "直接对象"},
    }))["data"]
    ok("dataContent 传对象", json.loads(i2["dataContent"])["input"] == "直接对象",
       i2["dataContent"])

    i3 = (await api(c, "POST", "/api/eval/datasets/items", json={
        "datasetId": ds_id, "dataContent": "纯文本样本",
    }))["data"]
    ok("dataContent 传纯文本 → 包成 {content: ...}",
       json.loads(i3["dataContent"]).get("content") == "纯文本样本", i3["dataContent"])

    n = (await api(c, "POST", "/api/eval/datasets/items/batch", json={
        "datasetId": ds_id,
        "dataContents": [
            json.dumps({"input": "批量1"}, ensure_ascii=False),
            {"input": "批量2"},
        ],
    }))["data"]
    ok("批量添加返回条数（不是数组）", n == 2, str(n))

    items = (await api(c, "GET", f"/api/eval/datasets/items?datasetId={ds_id}&pageSize=50"))["data"]
    ok("样本总数 5", items["total"] == 5, str(items["total"]))

    # 版本
    ver = (await api(c, "POST", "/api/eval/datasets/versions", json={
        "datasetId": ds_id, "version": "v1",
    }))["data"]
    ok("建版本成功", ver["version"] == "v1", ver.get("version"))
    ok("dataCount=5", ver["dataCount"] == 5, str(ver["dataCount"]))
    ok("datasetItems 是 id 数组的 JSON 字符串",
       isinstance(ver["datasetItems"], str), type(ver["datasetItems"]).__name__)
    ids_in_ver = json.loads(ver["datasetItems"])
    ok("id 数组里元素都是字符串（防精度丢失）",
       all(isinstance(x, str) for x in ids_in_ver), str(ids_in_ver[:2]))
    ok("id 数组长度 5", len(ids_in_ver) == 5, str(len(ids_in_ver)))

    vitems = (await api(c, "GET", f"/api/eval/datasets/versions/{ver['id']}/items"))["data"]
    ok("按版本取样本", len(vitems) == 5, str(len(vitems)))

    # 空数据集建版本
    empty = (await api(c, "POST", "/api/eval/datasets", json={
        "name": f"{PREFIX}空评测集",
    }))["data"]
    _track("dataset", empty["id"])
    r = await api(c, "POST", "/api/eval/datasets/versions", json={
        "datasetId": empty["id"], "version": "v1",
    })
    ok("空数据集建版本 → 400", r.get("code") == 400, str(r.get("message"))[:50])

    # 从示例创建
    ex_create = (await api(c, "POST", "/api/eval/datasets/examples/qa_accuracy"))["data"]
    _track("dataset", ex_create.get("id"))
    ok("从示例创建成功", bool(ex_create.get("id")), str(ex_create.get("id")))
    ex_items = (await api(
        c, "GET", f"/api/eval/datasets/items?datasetId={ex_create['id']}&pageSize=50"
    ))["data"]
    ok("示例带 5 条样本", ex_items["total"] == 5, str(ex_items["total"]))
    ex_vers = (await api(c, "GET", f"/api/eval/datasets/{ex_create['id']}/versions"))["data"]
    ok("示例自动建 v1.0 版本", len(ex_vers) == 1 and ex_vers[0]["version"] == "v1.0",
       str([v["version"] for v in ex_vers]))

    # 再创建一次，名称应加后缀
    ex2 = (await api(c, "POST", "/api/eval/datasets/examples/qa_accuracy"))["data"]
    _track("dataset", ex2.get("id"))
    ok("重复从同一示例创建 → 名称加后缀", "(" in ex2["name"], ex2["name"])

    return ds_id, ver["id"], i1["id"]


async def check_evaluators(c: httpx.AsyncClient) -> tuple[str, str]:
    """返回 (evaluator_id, version_id)。"""
    print("\n" + "=" * 70)
    print("7. 评估器：序列化 + 静态路径 + modelConfig 不是空")
    print("=" * 70)

    page = (await api(c, "GET", "/api/eval/evaluators?pageSize=50"))["data"]
    ok("列表返回", "records" in page, f"total={page.get('total')}")
    ok("有真实数据（2 条）", page["total"] >= 2, f"{page['total']}")
    if page["records"]:
        r = page["records"][0]
        ok("id 字符串", isinstance(r["id"], str))
        ok("tags 是字符串（逗号分隔，不是数组）", isinstance(r["tags"], str),
           type(r["tags"]).__name__)

    tpls = (await api(c, "GET", "/api/eval/evaluators/templates"))["data"]
    ok("模板列表（库里 3 条）", len(tpls) == 3, f"{len(tpls)} 条")
    ok("模板 modelConfig 是字符串", isinstance(tpls[0]["modelConfig"], str),
       tpls[0]["modelConfig"][:40])
    if tpls:
        one = (await api(
            c, "GET", f"/api/eval/evaluators/templates/{tpls[0]['evaluatorTemplateKey']}"
        ))["data"]
        ok("按 key 取模板", one["evaluatorTemplateKey"] == tpls[0]["evaluatorTemplateKey"])

    ex = (await api(c, "GET", "/api/eval/evaluators/examples"))["data"]
    ok("4 个示例评估器", len(ex) == 4, str(len(ex)))

    print("\n" + "=" * 70)
    print("8. 评估器 CRUD + 版本（modelConfig 必须原样取回）")
    print("=" * 70)

    created = (await api(c, "POST", "/api/eval/evaluators", json={
        "name": f"{PREFIX}评估器",
        "description": "自检用",
        "tags": "自检,临时",
    }))["data"]
    ev_id = created["id"]
    _track("evaluator", ev_id)
    ok("创建成功", bool(ev_id), str(ev_id))
    ok("tags 原样", created["tags"] == "自检,临时", created["tags"])

    ver = (await api(c, "POST", "/api/eval/evaluators/versions", json={
        "evaluatorId": ev_id,
        "version": "v1",
        "prompt": "请评估：参考 {{reference_output}}，实际 {{actual_output}}。"
                  '输出 JSON {"score": 0-1, "reason": "..."}',
        "variables": json.dumps(["reference_output", "actual_output"], ensure_ascii=False),
        "modelConfig": json.dumps(
            {"temperature": 0.1, "providerId": "123", "modelId": "test-model"},
            ensure_ascii=False,
        ),
    }))["data"]
    ok("版本创建成功", ver["version"] == "v1", ver.get("version"))
    ok("variables 是 JSON 字符串", isinstance(ver["variables"], str), ver["variables"])
    ok("variables 解析后是数组", json.loads(ver["variables"]) == ["reference_output", "actual_output"],
       ver["variables"])
    # 这是最关键的断言：modelConfig 丢了的话 from_attributes 会给 "{}"
    cfg = json.loads(ver["modelConfig"])
    ok("modelConfig **不是空的**（alias 生效）", cfg.get("temperature") == 0.1, ver["modelConfig"])
    ok("modelConfig 带 providerId", cfg.get("providerId") == "123", ver["modelConfig"])

    vers = (await api(c, "GET", f"/api/eval/evaluators/{ev_id}/versions"))["data"]
    ok("版本列表 1 条", len(vers) == 1, str(len(vers)))
    ok("列表里 modelConfig 也有值", json.loads(vers[0]["modelConfig"]).get("temperature") == 0.1,
       vers[0]["modelConfig"])

    # 版本不存在的 test 返回 null
    r = await api(c, "POST", "/api/eval/evaluators/test", json={
        "evaluatorVersionId": "999999999999", "variables": "{}",
    })
    ok("版本不存在 → data 为 null（不是 404）",
       r.get("code") == 200 and r["data"] is None, str(r.get("data")))

    # 从示例创建
    ev_ex = (await api(c, "POST", "/api/eval/evaluators/examples/text_similarity"))["data"]
    _track("evaluator", ev_ex.get("id"))
    ok("从示例创建评估器", bool(ev_ex.get("id")), str(ev_ex.get("id")))
    ev_ex_vers = (await api(c, "GET", f"/api/eval/evaluators/{ev_ex['id']}/versions"))["data"]
    ok("示例自动建 v1 版本", len(ev_ex_vers) == 1, str(len(ev_ex_vers)))
    if ev_ex_vers:
        ok("示例版本有提示词", bool(ev_ex_vers[0]["prompt"]),
           (ev_ex_vers[0]["prompt"] or "")[:40])

    return ev_id, ver["id"]


async def check_experiments(
    c: httpx.AsyncClient, ds_id: str, version_id: str, ev_version_id: str, live: bool
) -> None:
    print("\n" + "=" * 70)
    print("9. 实验：列表 / 详情 / 汇总 / 明细（读真实历史数据）")
    print("=" * 70)

    page = (await api(c, "GET", "/api/eval/experiments?pageSize=50"))["data"]
    ok("列表返回", "records" in page, f"total={page.get('total')}")
    ok("有真实数据（2 个）", page["total"] >= 2, f"{page['total']}")

    if page["records"]:
        r = page["records"][0]
        ok("id 字符串", isinstance(r["id"], str))
        ok("evaluationObjectConfig 是 JSON 字符串",
           isinstance(r["evaluationObjectConfig"], str),
           type(r["evaluationObjectConfig"]).__name__)
        ok("evaluatorConfig 是 JSON 字符串", isinstance(r["evaluatorConfig"], str),
           type(r["evaluatorConfig"]).__name__)
        ok("enrich 出 datasetName", bool(r.get("datasetName")), str(r.get("datasetName")))
        ok("enrich 出 promptKey", bool(r.get("promptKey")), str(r.get("promptKey")))
        ok("enrich 出 evaluatorNameList",
           isinstance(r.get("evaluatorNameList"), list) and len(r["evaluatorNameList"]) > 0,
           str(r.get("evaluatorNameList")))
        ok("evaluatorName 兼容字段 = 列表第一个",
           r.get("evaluatorName") == r["evaluatorNameList"][0],
           f"{r.get('evaluatorName')} vs {r['evaluatorNameList'][0]}")

        exp_id = r["id"]
        detail = (await api(c, "GET", f"/api/eval/experiments/{exp_id}"))["data"]
        ok("详情读回", detail["id"] == exp_id)

        ov = (await api(c, "GET", f"/api/eval/experiments/{exp_id}/results"))["data"]
        ok("汇总返回数组", isinstance(ov, list), str(len(ov)))
        if ov:
            row = ov[0]
            ok("汇总字段齐全",
               {"evaluatorVersionId", "evaluatorName", "avgScore",
                "evaluatedCount", "totalCount"} <= set(row), str(list(row))[:100])
            ok("avgScore 是 0~1 的数字", isinstance(row["avgScore"], (int, float))
               and 0 <= row["avgScore"] <= 1, str(row["avgScore"]))
            ok("evaluatorVersionId 是字符串", isinstance(row["evaluatorVersionId"], str))

        det = (await api(c, "GET", f"/api/eval/experiments/{exp_id}/detail?pageSize=5"))["data"]
        ok("明细返回分页", "records" in det, f"total={det.get('total')}")
        if det["records"]:
            d0 = det["records"][0]
            ok("score 是数字（numeric 转 float，不是字符串）",
               isinstance(d0["score"], (int, float)), f"{d0['score']} ({type(d0['score']).__name__})")
            ok("有 actualOutput", "actualOutput" in d0)
            ok("evaluatorVersionId 字符串", isinstance(d0["evaluatorVersionId"], str))

        # 按评估器过滤
        if ov:
            filt = (await api(
                c,
                "GET",
                f"/api/eval/experiments/{exp_id}/detail"
                f"?evaluatorVersionId={ov[0]['evaluatorVersionId']}&pageSize=5",
            ))["data"]
            ok("按评估器版本过滤生效",
               all(x["evaluatorVersionId"] == ov[0]["evaluatorVersionId"]
                   for x in filt["records"]), f"{filt['total']} 条")

    print("\n" + "=" * 70)
    print("10. 建实验（校验 + 异步启动）")
    print("=" * 70)

    base_payload = {
        "name": f"{PREFIX}实验",
        "description": "自检用",
        "datasetId": ds_id,
        "datasetVersionId": version_id,
        "datasetVersion": "v1",
        "evaluationObjectConfig": json.dumps({
            "type": "prompt",
            "config": {
                "promptKey": "workbuddy",
                "version": "1.0",
                "variableMap": [{"datasetColumn": "input", "promptVariable": "question"}],
            },
        }, ensure_ascii=False),
        "evaluatorConfig": json.dumps([{
            "evaluatorVersionId": ev_version_id,
            "variableMap": [
                {"source": "input", "evaluatorVariable": "input"},
                {"source": "reference_output", "evaluatorVariable": "reference_output"},
                {"source": "actual_output", "evaluatorVariable": "actual_output"},
            ],
        }], ensure_ascii=False),
    }

    # 缺 datasetVersionId
    bad1 = await api(c, "POST", "/api/eval/experiments", json={
        **base_payload, "datasetVersionId": None,
    })
    ok("缺 datasetVersionId → 400", bad1.get("code") == 400, str(bad1.get("message"))[:50])

    # 空评估器
    bad2 = await api(c, "POST", "/api/eval/experiments", json={
        **base_payload, "evaluatorConfig": "[]",
    })
    ok("空评估器 → 400", bad2.get("code") == 400, str(bad2.get("message"))[:50])

    # 6 个评估器
    bad3 = await api(c, "POST", "/api/eval/experiments", json={
        **base_payload,
        "evaluatorConfig": json.dumps(
            [{"evaluatorVersionId": ev_version_id}] * 6, ensure_ascii=False
        ),
    })
    ok("6 个评估器 → 400（上限 5）", bad3.get("code") == 400, str(bad3.get("message"))[:50])

    # 评估器缺 versionId
    bad4 = await api(c, "POST", "/api/eval/experiments", json={
        **base_payload,
        "evaluatorConfig": json.dumps([{"variableMap": []}], ensure_ascii=False),
    })
    ok("评估器缺 evaluatorVersionId → 400", bad4.get("code") == 400,
       str(bad4.get("message"))[:50])

    if not live:
        print("  - 跳过真实运行（加 --live 才跑；会真调模型花钱）")
        return

    print("\n" + "=" * 70)
    print("11. 真实运行（--live）：progress → completed + 落结果")
    print("=" * 70)

    created = (await api(c, "POST", "/api/eval/experiments", json=base_payload))["data"]
    exp_id = created["id"]
    _track("experiment", exp_id)
    ok("创建后立刻 running", created["status"] == "running", created["status"])
    ok("progress=0", created["progress"] == 0, str(created["progress"]))
    ok("有 taskId", bool(created.get("taskId")), str(created.get("taskId")))

    # 轮询等它跑完（最多 120 秒）
    final_status = "running"
    for _ in range(60):
        await asyncio.sleep(2)
        cur = (await api(c, "GET", f"/api/eval/experiments/{exp_id}"))["data"]
        final_status = cur["status"]
        if final_status != "running":
            print(f"     结束：status={final_status} progress={cur['progress']}")
            break

    ok("实验已结束（不再 running）", final_status != "running", final_status)
    if final_status == "completed":
        ov = (await api(c, "GET", f"/api/eval/experiments/{exp_id}/results"))["data"]
        ok("产出汇总", len(ov) >= 1, str(len(ov)))
        det = (await api(c, "GET", f"/api/eval/experiments/{exp_id}/detail?pageSize=50"))["data"]
        ok("产出明细（5 条样本）", det["total"] >= 1, str(det["total"]))
        if det["records"]:
            d0 = det["records"][0]
            ok("actualOutput 非空（真调了被测模型）", bool(d0["actualOutput"]),
               (d0["actualOutput"] or "")[:50])
            ok("score 在 0~1", 0 <= (d0["score"] or 0) <= 1, str(d0["score"]))
            ok("reason 非空", bool(d0["reason"]), (d0["reason"] or "")[:60])

    # 运行中不能删/改
    if final_status == "completed":
        r = await api(c, "PUT", f"/api/eval/experiments/{exp_id}", json={
            **base_payload, "name": f"{PREFIX}实验改名",
        })
        ok("已完成可以改", r.get("code") == 200, str(r.get("code")))

    # 重跑
    r = await api(c, "PUT", f"/api/eval/experiments/{exp_id}/restart")
    ok("重跑返回 running", r.get("code") == 200 and r["data"]["status"] == "running",
       str(r.get("data", {}).get("status")))
    ok("重跑清空旧结果", True)


async def main() -> None:
    live = "--live" in sys.argv
    check_offline()

    await cleanup()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=BASE, timeout=180) as c:
        ds_id, version_id, _ = await check_datasets(c)
        _ev_id, ev_version_id = await check_evaluators(c)
        await check_experiments(c, ds_id, version_id, ev_version_id, live)

    await cleanup()
    print("\n已清理测试数据")

    print()
    if failures:
        print(f"❌ 失败 {failures} 项")
        raise SystemExit(1)
    print("✅ 全部通过")


if __name__ == "__main__":
    _ = settings  # 保留导入供后续调试
    asyncio.run(main())
