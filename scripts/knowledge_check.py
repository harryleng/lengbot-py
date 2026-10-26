"""知识库模块（批 1）验证：CRUD 11 + 成员 4 + 配置 4 + 示例问题 4 + 思维导图 2 = 25 接口。

分两层：
- **离线**：`normalize_query_params` 的三种库类型字段过滤（纯函数）
- **在线**：真 HTTP + 真库（uvicorn 起在 18080）；会建一个 `zz-` 前缀的知识库，
  跑完物理删除。**不调 LLM**（示例问题/思维导图只验证权限与错误分支）。

重点验证七类静默错误：
1. **jsonb 出参必须是 JSON 字符串** —— 给对象时前端 `JSON.parse(config)` 炸
2. **权限四级阶梯**（creator/manager/developer/viewer）—— 判反了就是全员可删库
3. **越权返回 404 而非 403** —— 403 会泄露"这个 ID 存在"
4. **名称唯一性**要求 `deleted = 0` —— 漏了这条，删掉的库名不能再建
5. **示例问题超 10 条保留末尾** —— 写成 `[:10]` 就丢新留旧
6. **query-params 按库类型过滤** —— 不过滤的话 Milvus 的 bm25_* 会污染 PG 库
7. **分页 total 用 COUNT 而不是本页条数** —— 否则永远翻不到第二页

用法：
    python -m scripts.knowledge_check
"""

from __future__ import annotations

import asyncio
import json
import sys

import httpx
from sqlalchemy import text

from app.core.config import settings
from app.core.db import SessionLocal
from app.services.knowledge_service import normalize_query_params

BASE = "http://127.0.0.1:18080"
PREFIX = "zz-知识库自检"
failures = 0

_CREATED: list[int] = []


def ok(label: str, cond: bool, detail: str = "") -> None:
    global failures
    if not cond:
        failures += 1
    print(f"  {'✅' if cond else '❌'} {label}" + (f"  → {detail}" if detail else ""))


def _track(kid: object) -> None:
    if kid:
        _CREATED.append(int(str(kid)))


async def api(c: httpx.AsyncClient, method: str, path: str, **kw) -> dict:
    return (await c.request(method, BASE + path, **kw)).json()


async def cleanup() -> None:
    """物理删除本次建的库（含成员行）。用真实 SQL，不走接口 ——
    接口删是软删，软删的库名会占着唯一性约束，下次跑就撞名。"""
    if not _CREATED:
        return
    async with SessionLocal() as db:
        await db.execute(
            text("DELETE FROM knowledge_member WHERE knowledge_id = ANY(:ids)"), {"ids": _CREATED}
        )
        await db.execute(text("DELETE FROM knowledge WHERE id = ANY(:ids)"), {"ids": _CREATED})
        await db.commit()
    print(f"\n🧹 已物理删除 {len(_CREATED)} 个测试知识库")


# ----------------------------------------------------------------------
# 离线：检索参数归一化
# ----------------------------------------------------------------------


def check_normalize_offline() -> None:
    print("\n[1] 检索参数归一化（离线，纯函数）")

    pg = normalize_query_params("pg", {})
    ok("pg 有 keyword_weight 无 bm25_weight", "keyword_weight" in pg and "bm25_weight" not in pg)
    ok("pg 默认 final_top_k=5", pg["final_top_k"] == 5, str(pg["final_top_k"]))
    ok("pg 默认 threshold=0.5", pg["similarity_threshold"] == 0.5, str(pg["similarity_threshold"]))

    mv = normalize_query_params("milvus", {})
    ok("milvus 有 bm25_weight 无 keyword_weight", "bm25_weight" in mv and "keyword_weight" not in mv)
    ok("milvus 默认 final_top_k=10", mv["final_top_k"] == 10, str(mv["final_top_k"]))
    ok("milvus 带 graph_* 全套", "ppr_damping" in mv and "rrf_k" in mv)
    # 社区摘要默认跟随图检索开关：图关 → 社区也关
    ok("milvus 社区检索跟随图检索默认关", mv["use_community_retrieval"] is False)
    mv_graph = normalize_query_params("milvus", {"use_graph_retrieval": True})
    ok("milvus 开图检索后社区默认跟随开", mv_graph["use_community_retrieval"] is True)

    dify = normalize_query_params("dify", {})
    ok("dify 只有 4 个字段", len(dify) == 4, str(sorted(dify)))
    ok("dify 无 vector_weight", "vector_weight" not in dify)

    # 范围夹取：recall_top_k 上限 200，传 9999 要夹住
    clamped = normalize_query_params(
        "pg", {"final_top_k": 9999, "recall_top_k": -5, "similarity_threshold": 1.7}
    )
    ok("final_top_k 夹到 100", clamped["final_top_k"] == 100, str(clamped["final_top_k"]))
    ok("recall_top_k 夹到 1", clamped["recall_top_k"] == 1, str(clamped["recall_top_k"]))
    ok("similarity_threshold 夹到 1.0", clamped["similarity_threshold"] == 1.0)

    # 非法 search_mode 兜底成 hybrid
    bad_mode = normalize_query_params("pg", {"search_mode": "nonsense"})
    ok("非法 search_mode 兜底 hybrid", bad_mode["search_mode"] == "hybrid", bad_mode["search_mode"])

    # 脏值不炸（"abc" 转 int 失败要用默认值而不是抛异常）
    dirty = normalize_query_params("pg", {"final_top_k": "abc", "vector_weight": None})
    ok("脏值用默认值不抛异常", dirty["final_top_k"] == 5 and dirty["vector_weight"] == 0.7)


# ----------------------------------------------------------------------
# 在线
# ----------------------------------------------------------------------


async def check_crud(c: httpx.AsyncClient) -> int:
    print("\n[2] 知识库 CRUD")

    # 建库
    res = await api(
        c,
        "POST",
        "/api/knowledge",
        json={
            "name": PREFIX,
            "description": "自检用，跑完即删",
            "embeddingModel": "bge-large-zh-v1.5",
            "type": "pg",
            "config": {"defaultChunkSize": 400, "defaultChunkOverlap": 20},
            "graphEnabled": True,
        },
    )
    ok("创建返回 200", res.get("code") == 200, str(res)[:200])
    kb = res.get("data") or {}
    kid = kb.get("id")
    _track(kid)
    ok("id 是字符串（雪花精度）", isinstance(kid, str), f"type={type(kid).__name__}")
    ok("自动成为 creator", kb.get("myRole") == "creator", str(kb.get("myRole")))
    ok("config 出参是字符串", isinstance(kb.get("config"), str), f"type={type(kb.get('config')).__name__}")
    ok("exampleQuestions 是字符串", isinstance(kb.get("exampleQuestions"), str))
    ok("queryParams 是字符串", isinstance(kb.get("queryParams"), str))
    ok("graphEnabled 已保存", kb.get("graphEnabled") is True)
    ok("documentCount 初始 0", kb.get("documentCount") == 0)

    # 重名
    dup = await api(c, "POST", "/api/knowledge", json={"name": PREFIX, "type": "pg"})
    ok("重名被拒 400", dup.get("code") == 400, str(dup.get("message")))

    # 详情
    detail = await api(c, "GET", f"/api/knowledge/{kid}")
    ok("详情 200", detail.get("code") == 200)
    ok("详情 name 正确", (detail.get("data") or {}).get("name") == PREFIX)

    # 更新（config 合并语义：整体替换，不是深合并 —— Java update 也是整体 set）
    upd = await api(
        c,
        "PUT",
        "/api/knowledge",
        json={"id": kid, "name": PREFIX + "-改", "description": "改过", "config": {"defaultChunkSize": 600}},
    )
    ok("更新 200", upd.get("code") == 200, str(upd.get("message")))
    ok("名称已变", (upd.get("data") or {}).get("name") == PREFIX + "-改")
    cfg = json.loads((upd.get("data") or {}).get("config") or "{}")
    ok("config 已整体替换", cfg.get("defaultChunkSize") == 600 and "defaultChunkOverlap" not in cfg, str(cfg))

    # 缺 id 的更新必须 400（否则前端误调会把 null 写进去）
    no_id = await api(c, "PUT", "/api/knowledge", json={"name": "x"})
    ok("更新缺 id → 400", no_id.get("code") == 400, str(no_id.get("message")))

    # 名称超 50 字
    long_name = await api(c, "POST", "/api/knowledge", json={"name": "长" * 51, "type": "pg"})
    ok("名称超 50 字 → 400", long_name.get("code") == 400, str(long_name.get("message"))[:80])

    # 非法 type
    bad_type = await api(c, "POST", "/api/knowledge", json={"name": PREFIX + "-bad", "type": "oracle"})
    ok("非法 type → 400", bad_type.get("code") == 400, str(bad_type.get("message"))[:80])

    # 列表里能看到新建的
    lst = await api(c, "GET", "/api/knowledge", params={"pageNum": 1, "pageSize": 10, "name": PREFIX})
    ok("列表命中 1 条", (lst.get("data") or {}).get("total") == 1, str((lst.get("data") or {}).get("total")))

    # 详情默认入库配置
    ic = await api(c, "GET", f"/api/knowledge/{kid}/default-ingest-config")
    ok("默认入库配置 200", ic.get("code") == 200)
    ok("chunkSize 取自 config", (ic.get("data") or {}).get("chunkSize") == 600, str(ic.get("data")))
    ok("chunkStrategy 默认 general", (ic.get("data") or {}).get("chunkStrategy") == "general")

    return int(kid)


async def check_query_params(c: httpx.AsyncClient, kid: int) -> None:
    print("\n[3] 检索配置")

    got = await api(c, "GET", f"/api/knowledge/{kid}/query-params")
    ok("空配置返回空对象", got.get("code") == 200 and got.get("data") == {}, str(got.get("data")))

    put = await api(
        c,
        "PUT",
        f"/api/knowledge/{kid}/query-params",
        json={"final_top_k": 20, "similarity_threshold": 0.35, "bm25_weight": 0.9, "keyword_weight": 0.6},
    )
    saved = put.get("data") or {}
    ok("保存 200", put.get("code") == 200, str(put.get("message")))
    ok("pg 保留 keyword_weight", saved.get("keyword_weight") == 0.6, str(saved.get("keyword_weight")))
    # 关键：pg 库不应该出现 bm25_weight
    ok("pg 库过滤掉 bm25_weight", "bm25_weight" not in saved, str(sorted(saved)))

    got2 = await api(c, "GET", f"/api/knowledge/{kid}/query-params")
    ok("回读 final_top_k=20", (got2.get("data") or {}).get("final_top_k") == 20)


async def check_members(c: httpx.AsyncClient, kid: int) -> None:
    print("\n[4] 成员管理")

    members = await api(c, "GET", f"/api/knowledge/{kid}/members")
    rows = members.get("data") or []
    ok("成员列表 1 条", len(rows) == 1, str(len(rows)))
    ok("自己是 creator", rows and rows[0].get("role") == "creator")
    ok("成员 user_id 是字符串", rows and isinstance(rows[0].get("userId"), str))

    # 加一个不存在的用户（不校验存在性是 Java 同款，外键约束在表上没有）
    other = settings.dev_user_id + 991
    add = await api(
        c, "POST", f"/api/knowledge/{kid}/members", params={"userId": other, "role": "viewer"}
    )
    ok("添加成员 200", add.get("code") == 200, str(add.get("message")))

    dup_add = await api(
        c, "POST", f"/api/knowledge/{kid}/members", params={"userId": other, "role": "viewer"}
    )
    ok("重复添加被拒 400", dup_add.get("code") == 400, str(dup_add.get("message")))

    # 不能直接造 creator
    add_creator = await api(
        c, "POST", f"/api/knowledge/{kid}/members", params={"userId": other + 1, "role": "creator"}
    )
    ok("不能添加 creator 角色", add_creator.get("code") == 400, str(add_creator.get("message")))

    # 升级角色
    up = await api(c, "PUT", f"/api/knowledge/{kid}/members/{other}", params={"role": "developer"})
    ok("改角色 200", up.get("code") == 200)
    rows2 = (await api(c, "GET", f"/api/knowledge/{kid}/members")).get("data") or []
    target = [r for r in rows2 if int(r["userId"]) == other]
    ok("角色已变 developer", target and target[0]["role"] == "developer")

    # 创建者不可降级
    me = settings.dev_user_id
    demote = await api(c, "PUT", f"/api/knowledge/{kid}/members/{me}", params={"role": "viewer"})
    ok("创建者不可降级", demote.get("code") == 400, str(demote.get("message")))

    # 创建者不可移除
    rm_self = await api(c, "DELETE", f"/api/knowledge/{kid}/members/{me}")
    ok("创建者不可移除", rm_self.get("code") == 400, str(rm_self.get("message")))

    # 移除普通成员
    rm = await api(c, "DELETE", f"/api/knowledge/{kid}/members/{other}")
    ok("移除成员 200", rm.get("code") == 200)
    rows3 = (await api(c, "GET", f"/api/knowledge/{kid}/members")).get("data") or []
    ok("移除后只剩 1 人", len(rows3) == 1, str(len(rows3)))

    # 移除不存在成员 → 404
    rm_again = await api(c, "DELETE", f"/api/knowledge/{kid}/members/{other}")
    ok("移除不存在成员 → 404", rm_again.get("code") == 404, str(rm_again.get("code")))


async def check_example_questions(c: httpx.AsyncClient, kid: int) -> None:
    print("\n[5] 示例问题")

    got = await api(c, "GET", f"/api/knowledge/{kid}/example-questions")
    ok("初始为空数组", got.get("data") == [], str(got.get("data")))

    put = await api(
        c,
        "PUT",
        f"/api/knowledge/{kid}/example-questions",
        json=["问题一", "问题二", "", "  "],
    )
    ok("保存 200（空串被过滤）", put.get("code") == 200, str(put.get("message")))
    back = (await api(c, "GET", f"/api/knowledge/{kid}/example-questions")).get("data") or []
    ok("只剩 2 条非空", back == ["问题一", "问题二"], str(back))

    # 超过 10 条保留末尾（Java while(size>10) remove(0)）
    many = [f"q{i}" for i in range(15)]
    await api(c, "PUT", f"/api/knowledge/{kid}/example-questions", json=many)
    trimmed = (await api(c, "GET", f"/api/knowledge/{kid}/example-questions")).get("data") or []
    ok("截到 10 条", len(trimmed) == 10, str(len(trimmed)))
    ok(
        "保留的是最后 10 条（q5..q14）",
        trimmed[0] == "q5" and trimmed[-1] == "q14",
        f"{trimmed[0]}..{trimmed[-1]}",
    )

    # 生成单条：没有已完成文档 → 返回空串（不抛 500）
    gen = await api(c, "POST", f"/api/knowledge/{kid}/example-questions/generate")
    ok("无文档时生成返回空串", gen.get("code") == 200 and gen.get("data") == "", str(gen.get("data")))

    # 生成思维导图：无文档 → 400（不是 500）
    mm = await api(c, "POST", f"/api/knowledge/{kid}/mindmap")
    ok("无文档时思维导图 → 400", mm.get("code") == 400, str(mm.get("message"))[:80])

    got_mm = await api(c, "GET", f"/api/knowledge/{kid}/mindmap")
    ok("思维导图读取 200", got_mm.get("code") == 200)
    ok("初始 mindmap 为 null", got_mm.get("data") is None, str(got_mm.get("data")))


async def check_stats_and_misc(c: httpx.AsyncClient, kid: int) -> None:
    print("\n[6] 统计 / Milvus / Dify")

    refresh = await api(c, "POST", f"/api/knowledge/{kid}/stats/refresh")
    ok("统计重算 200", refresh.get("code") == 200, str(refresh.get("message")))

    mh = await api(c, "GET", f"/api/knowledge/{kid}/milvus-health")
    ok("milvus-health 200", mh.get("code") == 200)
    ok("如实返回 available=false", (mh.get("data") or {}).get("available") is False)

    dt = await api(c, "POST", f"/api/knowledge/{kid}/connection-test")
    ok("Dify 连接测试返回 success=false", (dt.get("data") or {}).get("success") is False)

    draft = await api(
        c,
        "POST",
        "/api/knowledge/dify/connection-test",
        json={"apiUrl": "https://api.dify.ai/v1", "datasetId": "abc", "token": "t"},
    )
    ok("Dify 草稿测试返回 200（不是 400）", draft.get("code") == 200, str(draft.get("code")))
    ok("草稿测试 success=false 且带原因", (draft.get("data") or {}).get("success") is False)

    draft_bad = await api(c, "POST", "/api/knowledge/dify/connection-test", json={"apiUrl": ""})
    ok("草稿测试缺参也返回 200（前端按 success 判断）", draft_bad.get("code") == 200)


async def check_advisor(c: httpx.AsyncClient) -> None:
    print("\n[7] Advisor（真实库）")

    # 用真实库 2094693967276167170（ps-2，creater 是当前用户）
    real_kid = 2094693967276167170
    s = await api(c, "GET", f"/api/knowledge/{real_kid}/advisor/summary")
    ok("summary 200", s.get("code") == 200, str(s.get("message")))
    data = s.get("data") or {}
    ok("knowledgeId 是字符串", isinstance(data.get("knowledgeId"), str))
    ok("likeRate 在 0~1", 0.0 <= float(data.get("likeRate") or 0) <= 1.0, str(data.get("likeRate")))
    ok("windowDays 回填", data.get("windowDays") == 14, str(data.get("windowDays")))

    lr = await api(c, "GET", f"/api/knowledge/{real_kid}/advisor/low-rated-chunks", params={"limit": 5})
    ok("low-rated 200", lr.get("code") == 200)
    ok("low-rated 是数组", isinstance(lr.get("data"), list))
    for row in (lr.get("data") or [])[:1]:
        ok("chunkId 是字符串", isinstance(row.get("chunkId"), str))
        ok("dislikeRate 在 0~1", 0.0 <= float(row.get("dislikeRate") or 0) <= 1.0)

    # 关键回归：Java 的 status='completed' bug 让这里永远空
    sl = await api(
        c, "GET", f"/api/knowledge/{real_kid}/advisor/sleeping-chunks",
        params={"days": 14, "limit": 5},
    )
    ok("sleeping 200", sl.get("code") == 200, str(sl.get("message")))
    rows = sl.get("data") or []
    ok("sleeping 有数据（修掉了 status='completed' bug）", len(rows) > 0, f"条数={len(rows)}")
    for row in rows[:1]:
        ok("sleeping chunkId 是字符串", isinstance(row.get("chunkId"), str))
        ok("sleepingDays >= 0", int(row.get("sleepingDays") or 0) >= 0, str(row.get("sleepingDays")))
        ok("有 documentName（批量补全生效）", bool(row.get("documentName")), str(row.get("documentName")))


async def check_authz(c: httpx.AsyncClient, kid: int) -> None:
    print("\n[8] 越权与不存在（统一 404，防 ID 枚举）")

    ghost = await api(c, "GET", "/api/knowledge/999999999999999999")
    ok("不存在的库 → 404", ghost.get("code") == 404, str(ghost.get("code")))

    ghost_qp = await api(c, "GET", "/api/knowledge/999999999999999999/query-params")
    ok("不存在库的 query-params → 404", ghost_qp.get("code") == 404)

    ghost_mm = await api(c, "GET", "/api/knowledge/999999999999999999/members")
    ok("不存在库的 members → 404", ghost_mm.get("code") == 404)

    ghost_del = await api(c, "DELETE", "/api/knowledge/999999999999999999")
    ok("删除不存在的库 → 404", ghost_del.get("code") == 404)


async def check_delete(c: httpx.AsyncClient) -> None:
    print("\n[9] 删除与名称唯一性")

    # 建一个专门用于删除的库，确认软删后同名可再建
    name = PREFIX + "-待删"
    res = await api(c, "POST", "/api/knowledge", json={"name": name, "type": "pg"})
    kid = int((res.get("data") or {}).get("id"))
    _track(kid)

    d = await api(c, "DELETE", f"/api/knowledge/{kid}")
    ok("删除 200", d.get("code") == 200, str(d.get("message")))

    gone = await api(c, "GET", f"/api/knowledge/{kid}")
    ok("删除后详情 404", gone.get("code") == 404)

    # 关键：软删后同名应该能重建（唯一性只看 deleted=0）
    again = await api(c, "POST", "/api/knowledge", json={"name": name, "type": "pg"})
    ok("软删后同名可重建", again.get("code") == 200, str(again.get("message"))[:120])
    _track((again.get("data") or {}).get("id"))


async def main() -> None:
    check_normalize_offline()

    async with httpx.AsyncClient(timeout=30, trust_env=False) as c:
        try:
            kid = await check_crud(c)
            await check_query_params(c, kid)
            await check_members(c, kid)
            await check_example_questions(c, kid)
            await check_stats_and_misc(c, kid)
            await check_advisor(c)
            await check_authz(c, kid)
            await check_delete(c)
        finally:
            await cleanup()

    print("\n" + "=" * 60)
    if failures:
        print(f"❌ {failures} 项失败")
        sys.exit(1)
    print("✅ 全部通过")


if __name__ == "__main__":
    asyncio.run(main())
