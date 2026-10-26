"""问答对 9 + OCR 1 接口验证（批 3）。

重点验证这些**静默失败**：

1. **status 出参口径错误** → 前端图标全灰。
   `source` 必须出英文 code（manual/import/ai），`status` 必须出中文 desc
   （待向量化/生效/失败）。这是 Java `toVO` 的遗留不一致，前端照这个写的。
2. **改 answer 不该重算向量** → 白烧一次 API 调用，且状态被无谓地重置。
3. **删问答对不删向量** → embedding 里留孤儿行，占 HNSW 索引还可能被召回。
4. **QA 检索漏 `deleted=0`** → 软删的问答对还能被检索到（数据泄漏）。
5. **路由顺序**：`/qa-pairs/{id}` 不能被 `/{knowledge_id}` 吃掉。
6. **批量向量化 body 形态**：裸数组 `[1,2]`，不是 `{"ids": [1,2]}`。
7. **`active` 状态重复向量化要跳过**，返回 False 但不是报错。

用法：
    python -m scripts.knowledge_qa_check
"""

from __future__ import annotations

import asyncio
import io
import sys
import time

import httpx
from sqlalchemy import text

from app.core.db import SessionLocal

BASE = "http://127.0.0.1:18080"
failures = 0

_KB_IDS: list[int] = []
_QA_IDS: list[int] = []


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
        return {"code": resp.status_code, "message": f"[非 JSON] {resp.text[:200]}"}


def _content(sections: int = 20) -> bytes:
    """素材必须是**主题连贯**的中文正文。

    用没有任何语义的"第 N 段正文"拼出来的文档，查询和它的余弦相似度只有
    0.4 左右（实测），会被 0.5 的阈值正常过滤掉 —— 返回空是**对的**，
    但会让测试自己骗自己：到底是链路断了还是查询不相关，看不出来。
    """
    return "\n\n".join(
        f"## 第{i + 1}节 差旅报销规定\n"
        f"员工出差产生的交通与住宿费用，须在返回公司后三十天内完成报销流程，"
        f"逾期财务部门有权不再受理。这是差旅管理制度的第{i + 1}条规定。"
        for i in range(sections)
    ).encode("utf-8")


async def cleanup() -> None:
    async with SessionLocal() as db:
        kids = list(_KB_IDS)
        rows = (await db.execute(
            text("SELECT id FROM knowledge WHERE name LIKE 'zz-%'")
        )).scalars().all()
        kids += [r for r in rows if r not in kids]
        if not kids:
            return
        await db.execute(
            text("DELETE FROM embedding WHERE knowledge_id = ANY(:k)"), {"k": kids}
        )
        await db.execute(text("DELETE FROM chunk WHERE knowledge_id = ANY(:k)"), {"k": kids})
        await db.execute(
            text("DELETE FROM document WHERE knowledge_id = ANY(:k) OR name LIKE 'zz-%'"),
            {"k": kids},
        )
        await db.execute(
            text("DELETE FROM qa_pair WHERE knowledge_id = ANY(:k)"), {"k": kids}
        )
        await db.execute(text("DELETE FROM task WHERE ref_id = ANY(:k)"), {"k": kids})
        await db.execute(
            text("DELETE FROM knowledge_member WHERE knowledge_id = ANY(:k)"), {"k": kids}
        )
        await db.execute(text("DELETE FROM knowledge WHERE id = ANY(:k)"), {"k": kids})
        await db.commit()
    print(f"\n🧹 已清理 {len(set(_KB_IDS))} 个知识库及其问答对/向量")


# ----------------------------------------------------------------------
# 离线：口径纯函数
# ----------------------------------------------------------------------


def check_offline() -> None:
    print("\n[1] 出参口径（离线）")

    from app.services.qa_pair_service import STATUS_DESC, _estimate_token_count, _parse_qa_pairs

    ok("status 全量映射到中文 desc",
       STATUS_DESC == {"pending": "待向量化", "vectorizing": "向量化中",
                       "active": "生效", "failed": "失败"},
       str(STATUS_DESC))

    # token 估算：中文 1.5 字/token
    ok("token 估算：3 个中文 = 2", _estimate_token_count("你好啊") == 2,
       str(_estimate_token_count("你好啊")))
    ok("token 估算：空串 = 0", _estimate_token_count("") == 0)

    # JSON 抠取三重防御
    got = _parse_qa_pairs('```json\n[{"question":"Q1","answer":"A1"}]\n```')
    ok("剥 markdown 围栏", len(got) == 1 and got[0].question == "Q1", str(got))
    got = _parse_qa_pairs('<think>让我想想</think>\n[{"question":"Q2","answer":"A2"}]')
    ok("剥 <think> 思考块", len(got) == 1 and got[0].question == "Q2", str(got))
    got = _parse_qa_pairs('{"qa_pairs":[{"question":"Q3","answer":"A3"}]}')
    ok("兼容包一层的对象", len(got) == 1 and got[0].question == "Q3", str(got))
    try:
        _parse_qa_pairs("<html>502 Bad Gateway</html>")
        ok("HTML 错误页 → 明确报错", False)
    except RuntimeError as exc:
        ok("HTML 错误页 → 明确报错并指出原因", "供应商" in str(exc), str(exc)[:50])
    ok("空回复 → 空列表（不抛）", _parse_qa_pairs("") == [])

    print("\n[2] OCR 健康检查")


async def check_ocr(c: httpx.AsyncClient) -> None:
    print("\n[2] OCR 健康检查")

    r = await api(c, "GET", "/api/ocr/health")
    ok("health 200", r.get("code") == 200, str(r.get("message"))[:60])
    d = r.get("data") or {}
    ok("字段齐全（healthy/modelPath/message/modelExists）",
       {"healthy", "modelPath", "message", "modelExists"} <= set(d.keys()), str(sorted(d.keys())))
    ok("healthy 是布尔", isinstance(d.get("healthy"), bool), repr(d.get("healthy")))
    ok(
        "结果可用（healthy 或给出原因）",
        d.get("healthy") is True or bool(d.get("message")),
        f"healthy={d.get('healthy')} msg={str(d.get('message'))[:60]}",
    )


# ----------------------------------------------------------------------
# 在线
# ----------------------------------------------------------------------


async def check_crud(c: httpx.AsyncClient) -> int:
    print("\n[3] 问答对 CRUD + 出参口径")

    r = await api(c, "POST", "/api/knowledge", json={"name": f"zz-QA自检-{int(time.time())}", "type": "pg"})
    if r.get("code") != 200:
        raise RuntimeError(f"建库失败: {r.get('message')}")
    kid = int(r["data"]["id"])
    _KB_IDS.append(kid)

    # 先传一篇文档，给 search 提供 chunk 素材
    up = await api(
        c,
        "POST",
        f"/api/knowledge/{kid}/documents",
        files={"file": ("zz-qa.md", io.BytesIO(_content()), "text/markdown")},
        params={"ocrEnabled": "false"},
    )
    ok("素材文档入库", up.get("code") == 200, str(up.get("message"))[:80])

    # --- create ---
    r = await api(c, "POST", f"/api/knowledge/{kid}/qa-pairs",
                  json={"question": "报销需要多少天内完成？", "answer": "三十天内完成报销流程。"})
    ok("创建 200", r.get("code") == 200, str(r.get("message"))[:120])
    d = r.get("data") or {}
    qa_id = int(d["id"])
    _QA_IDS.append(qa_id)
    ok("id 是字符串", isinstance(d.get("id"), str))
    ok("knowledgeId 是字符串", isinstance(d.get("knowledgeId"), str))
    ok("source 是英文 code = manual", d.get("source") == "manual", repr(d.get("source")))
    ok("status 是中文 desc = 生效", d.get("status") == "生效", repr(d.get("status")))
    ok("tokenCount > 0", (d.get("tokenCount") or 0) > 0, str(d.get("tokenCount")))

    # 向量真的写了
    async with SessionLocal() as db:
        cnt = (await db.execute(
            text("SELECT count(*) FROM embedding WHERE qa_pair_id = :q"), {"q": qa_id}
        )).scalar_one()
    ok("QA 向量已落库", cnt == 1, f"行数={cnt}")

    # --- update: 只改 answer，不该重算向量、状态不变 ---
    async with SessionLocal() as db:
        before = (await db.execute(
            text("SELECT id FROM embedding WHERE qa_pair_id = :q"), {"q": qa_id}
        )).scalars().all()
    r = await api(c, "PUT", f"/api/knowledge/qa-pairs/{qa_id}", json={"answer": "改后的答案。"})
    ok("只改 answer 200", r.get("code") == 200, str(r.get("message"))[:100])
    async with SessionLocal() as db:
        after = (await db.execute(
            text("SELECT id FROM embedding WHERE qa_pair_id = :q"), {"q": qa_id}
        )).scalars().all()
    ok("改 answer 不重建向量（id 不变）", before and before == after, f"{before} vs {after}")
    ok("改 answer 后 answer 已变", (r.get("data") or {}).get("answer") == "改后的答案。")

    # --- update: 改 question，必须重算 ---
    r = await api(c, "PUT", f"/api/knowledge/qa-pairs/{qa_id}",
                  json={"question": "报销期限是多久？"})
    ok("改 question 200", r.get("code") == 200, str(r.get("message"))[:100])
    async with SessionLocal() as db:
        after2 = (await db.execute(
            text("SELECT count(*) FROM embedding WHERE qa_pair_id = :q"), {"q": qa_id}
        )).scalar_one()
    ok("改 question 后向量仍恰好 1 条", after2 == 1, f"行数={after2}")

    # --- list ---
    r = await api(c, "GET", f"/api/knowledge/{kid}/qa-pairs",
                  params={"pageNum": 1, "pageSize": 10})
    lst = r.get("data") or {}
    ok("列表 200", r.get("code") == 200)
    ok("total ≥ 1", (lst.get("total") or 0) >= 1, str(lst.get("total")))
    rec = (lst.get("records") or [{}])[0]
    ok("列表里 status 也是中文 desc", rec.get("status") in {"生效", "待向量化", "失败", "向量化中"},
       repr(rec.get("status")))
    ok("列表里 source 也是英文 code", rec.get("source") in {"manual", "import", "ai"},
       repr(rec.get("source")))

    # 关键字过滤
    kw = await api(c, "GET", f"/api/knowledge/{kid}/qa-pairs",
                   params={"pageNum": 1, "pageSize": 10, "keyword": "报销期限"})
    recs = ((kw.get("data") or {}).get("records") or [])
    ok("关键字过滤生效", len(recs) == 1 and "报销期限" in recs[0]["question"], f"条数={len(recs)}")

    return kid


async def check_vectorize_and_import(c: httpx.AsyncClient, kid: int) -> None:
    print("\n[4] 批量导入 / 手动向量化 / 重复触发")

    items = [
        {"question": f"差旅报销的第{i + 1}条规定是什么？", "answer": f"第{i + 1}条规定要求在三十天内提交。"}
        for i in range(3)
    ]
    r = await api(c, "POST", f"/api/knowledge/{kid}/qa-pairs/batch-import", json=items)
    ok("批量导入 200", r.get("code") == 200, str(r.get("message"))[:100])
    ok("导入条数正确", r.get("data") == 3, str(r.get("data")))

    lst = await api(c, "GET", f"/api/knowledge/{kid}/qa-pairs", params={"pageNum": 1, "pageSize": 50})
    imported = [x for x in ((lst.get("data") or {}).get("records") or []) if x["source"] == "import"]
    ok("导入的 source=import", len(imported) == 3, f"条数={len(imported)}")
    for x in imported:
        _QA_IDS.append(int(x["id"]))

    async with SessionLocal() as db:
        ready = (await db.execute(text(
            "SELECT q.id FROM qa_pair q WHERE q.knowledge_id=:k AND q.deleted=0 AND q.status='pending' "
            "ORDER BY q.create_time DESC LIMIT 1"
        ), {"k": kid})).scalars().first()

    if ready is None:
        # 找一个 pending/failed 的（构造一个 failed 来验证可重试）
        any_id = int(imported[0]["id"])
        async with SessionLocal() as db:
            await db.execute(text("UPDATE qa_pair SET status='failed' WHERE id=:i"), {"i": any_id})
            await db.commit()
        ready = any_id
        print("  ⚠️ 没有 pending 记录，构造一条 failed 用于验证重试")

    r = await api(c, "POST", f"/api/knowledge/qa-pairs/{ready}/vectorize")
    ok("手动向量化 200", r.get("code") == 200, str(r.get("message"))[:100])

    async with SessionLocal() as db:
        st = (await db.execute(
            text("SELECT status FROM qa_pair WHERE id=:i"), {"i": ready}
        )).scalar_one()
    ok("向量化后 status=active", st == "active", str(st))

    # active 再触发应跳过（不报错）
    r = await api(c, "POST", f"/api/knowledge/qa-pairs/{ready}/vectorize")
    ok("active 重复向量化跳过但不报错", r.get("code") == 200, str(r.get("message"))[:80])

    # 批量：裸数组 body
    ids = [int(x["id"]) for x in imported[:2]]
    r = await api(c, "POST", "/api/knowledge/qa-pairs/batch-vectorize", json=ids)
    ok("批量向量化接受裸数组", r.get("code") == 200, str(r.get("message"))[:120])
    ok("返回触发条数（int）", isinstance(r.get("data"), int), repr(r.get("data")))

    r2 = await api(c, "POST", "/api/knowledge/qa-pairs/batch-vectorize",
                   json={"qaPairIds": ids})
    ok("批量向量化也兼容对象形态", r2.get("code") == 200, str(r2.get("message"))[:80])


async def check_search(c: httpx.AsyncClient, kid: int) -> None:
    print("\n[5] 检索测试（chunk + QA 两路）")

    r = await api(c, "POST", f"/api/knowledge/{kid}/search",
                  json={"question": "报销需要多少天内完成？"})
    ok("search 200", r.get("code") == 200, str(r.get("message"))[:120])
    rows = r.get("data") or []
    ok("有返回结果", len(rows) > 0, f"条数={len(rows)}")

    # ---- QA 路：默认阈值 0.85（Java 同值）几乎要求"问句同义"才命中 ----
    # 这两条断言必须**成对**存在：先证明同义能召回（QA 路没断），
    # 再证明放宽阈值能召回（说明上面"泛问句召不回"是阈值过滤的正确行为，
    # 不是链路故障）。缺一条，以后有人动了 qa_threshold 默认值就看不出来。
    await api(c, "POST", f"/api/knowledge/{kid}/qa-pairs",
              json={"question": "差旅报销的期限是几天？", "answer": "三十天内提交报销申请。"})
    exact = await api(c, "POST", f"/api/knowledge/{kid}/search",
                      json={"question": "差旅报销的期限是几天？"})
    exact_rows = exact.get("data") or []
    exact_types = [x.get("resultType") for x in exact_rows]
    ok("同义提问能召回 QA", "qa_pair" in exact_types, str(exact_types))

    exact_qa = [x for x in exact_rows if x.get("resultType") == "qa_pair"]
    if exact_qa:
        ok("QA 的 documentName 是「问答对」", exact_qa[0].get("documentName") == "问答对",
           repr(exact_qa[0].get("documentName")))
        ok("QA 的 content 带「【问答对】Q:」前缀",
           exact_qa[0].get("content", "").startswith("【问答对】Q:"),
           repr(exact_qa[0].get("content", "")[:30]))
        ok("QA 排在 chunk 前面（qa_priority 默认 true）",
           exact_types.index("qa_pair") == 0, str(exact_types))
    else:
        ok("QA 字段口径（documentName/content）", False, "没有 QA 结果")

    # 泛问句 + 放宽阈值 → 应当能召回
    loose = await api(c, "POST", f"/api/knowledge/{kid}/search",
                      json={"question": "报销流程怎么走？",
                            "overrides": {"qa_threshold": 0.5}})
    loose_types = [x.get("resultType") for x in (loose.get("data") or [])]
    ok("放宽 qa_threshold 后泛问句也能召回 QA", "qa_pair" in loose_types, str(loose_types))

    if exact_qa:
        ok("rank 从 1 连续递增",
           [x.get("rank") for x in exact_rows] == list(range(1, len(exact_rows) + 1)),
           str([x.get("rank") for x in exact_rows]))
    ok("score 有值且 ≤ 1",
       all(isinstance(x.get("score"), (int, float)) and x["score"] <= 1.0 for x in exact_rows),
       str([x.get("score") for x in exact_rows][:5]))

    # overrides 生效
    r2 = await api(c, "POST", f"/api/knowledge/{kid}/search",
                   json={"question": "报销需要多少天内完成？",
                         "overrides": {"qa_enabled": False, "top_k": 1}})
    types2 = [x.get("resultType") for x in (r2.get("data") or [])]
    ok("overrides.qa_enabled=false 时无 QA 结果", "qa_pair" not in types2, str(types2))
    ok("overrides.top_k=1 限制 chunk 条数", len(types2) <= 1, f"条数={len(types2)}")

    # 不存在的知识库
    ghost = await api(c, "POST", "/api/knowledge/999999999999999999/search",
                      json={"question": "x"})
    ok("不存在的知识库 → 404", ghost.get("code") == 404, str(ghost.get("code")))


async def check_permission_and_delete(c: httpx.AsyncClient, kid: int) -> None:
    print("\n[6] 越权 / 删除级联 / 路由顺序")

    r = await api(c, "GET", f"/api/knowledge/{kid}/qa-pairs", params={"pageNum": 1, "pageSize": 50})
    recs = ((r.get("data") or {}).get("records") or [])
    if not recs:
        ok("有可删除的问答对", False)
        return
    target = recs[0]
    tid = int(target["id"])

    # 删除前确认有向量
    async with SessionLocal() as db:
        before = (await db.execute(
            text("SELECT count(*) FROM embedding WHERE qa_pair_id = :q"), {"q": tid}
        )).scalar_one()

    d = await api(c, "DELETE", f"/api/knowledge/qa-pairs/{tid}")
    ok("删除 200", d.get("code") == 200, str(d.get("message"))[:100])

    async with SessionLocal() as db:
        after = (await db.execute(
            text("SELECT count(*) FROM embedding WHERE qa_pair_id = :q"), {"q": tid}
        )).scalar_one()
        row = (await db.execute(
            text("SELECT deleted FROM qa_pair WHERE id = :q"), {"q": tid}
        )).first()
    ok("删除后向量清零（没有孤儿行）", after == 0, f"删前={before} 删后={after}")
    ok("问答对是软删（deleted=1）", row and row[0] == 1, str(row))

    gone = await api(c, "PUT", f"/api/knowledge/qa-pairs/{tid}", json={"answer": "x"})
    ok("软删后再访问 → 404", gone.get("code") == 404, str(gone.get("code")))

    # 软删的问答对不能出现在列表里
    lst = await api(c, "GET", f"/api/knowledge/{kid}/qa-pairs", params={"pageNum": 1, "pageSize": 50})
    ids = [x["id"] for x in ((lst.get("data") or {}).get("records") or [])]
    ok("软删的不再出现在列表", str(tid) not in ids, f"剩余={len(ids)}")

    # 不存在 id
    ghost = await api(c, "DELETE", "/api/knowledge/qa-pairs/999999999999999999")
    ok("不存在的问答对 → 404", ghost.get("code") == 404, str(ghost.get("code")))


async def main() -> None:
    check_offline()
    async with httpx.AsyncClient(timeout=300, trust_env=False) as c:
        try:
            kid = await check_crud(c)
            await check_ocr(c)
            await check_vectorize_and_import(c, kid)
            await check_search(c, kid)
            await check_permission_and_delete(c, kid)
        finally:
            await cleanup()

    print("\n" + "=" * 60)
    if failures:
        print(f"❌ {failures} 项失败")
        sys.exit(1)
    print("✅ 全部通过")


if __name__ == "__main__":
    asyncio.run(main())
