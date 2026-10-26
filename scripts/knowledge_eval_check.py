"""知识库 RAG 评估 10 接口验证（对齐前端 `src/api/knowledgeEval.js`）。

重点验证这些**静默失败**：

1. **`generate` / `run` 返回的是 Task 记录**，不是 benchmark/result 本身。
   前端拿 `taskId` 去任务中心轮询 —— 返回错了前端永远显示"运行中"。
2. **`status` 大小写两套口径**：benchmark 是小写 `generating/ready`，
   result 是**大写** `RUNNING/COMPLETED/FAILED`。前端按这个匹配，别"顺手统一"。
3. **`overallScore` 只由检索指标（8 个 recall/f1 均值）构成，不含答案准确率**。
   这是 Java 的有意设计，不是漏算 —— 断言里专门验证。
4. **errorOnly 的语义**：`answer_score IS NULL`（没配评判模型）和 `= 0.0`（判了不合格）
   都算"问题明细"，但前者其实是"没跑"。
5. **gold_chunk_ids 存的是 JSON 数组字符串**，上传/下载往返必须无损。

跑评估**不配 answerProvider/judgeProvider** 时不花 LLM 的钱（只调本地 embedding），
真调模型的 generate/answer/judge 放 `--llm` 可选段。

用法：
    python -m scripts.knowledge_eval_check            # 免费路径
    python -m scripts.knowledge_eval_check --llm      # 追加真调模型
"""

from __future__ import annotations

import asyncio
import io
import json
import sys
import time

import httpx
from sqlalchemy import text

from app.core.db import SessionLocal

BASE = "http://127.0.0.1:18080"
failures = 0
_KB_IDS: list[int] = []
_STAMP = str(int(time.time()))[-6:]


def ok(label: str, cond: bool, detail: str = "") -> None:
    global failures
    if not cond:
        failures += 1
    print(f"  {'✅' if cond else '❌'} {label}" + (f"  → {detail}" if detail else ""))


async def api(c: httpx.AsyncClient, method: str, path: str, **kw) -> dict:
    resp = await c.request(method, BASE + path, **kw)
    ct = resp.headers.get("content-type", "")
    if "application/json" not in ct:
        # 下载等非 JSON 响应：包一层让断言统一处理
        return {"code": resp.status_code, "_bytes": resp.content, "_headers": dict(resp.headers)}
    try:
        return resp.json()
    except Exception:  # noqa: BLE001
        return {"code": resp.status_code, "message": resp.text[:200]}


def data(body: dict) -> dict | list | None:
    return body.get("data")


def _content(sections: int = 24) -> bytes:
    """主题连贯的素材（垃圾分类）。知识不连贯向量相似度会掉到阈值以下，测试自欺。"""
    parts = ["# 垃圾分类知识手册\n"]
    cats = ["可回收物", "厨余垃圾", "有害垃圾", "其他垃圾"]
    for i in range(sections):
        cat = cats[i % 4]
        parts.append(
            f"## 第{i + 1}节 {cat}细则{i + 1}\n\n"
            f"{cat}中第{i + 1}类常见物品需要特别注意：破损的包装应当清空内容物后投放，"
            f"污染严重的纸制品属于{cat}而非可回收物。"
            f"投放时间：工作日上午七点至九点，傍晚六点至八点。第{i + 1}号投放点在东门北侧。"
            f"如遇大件物品，需预约上门回收，电话 555-{1000 + i}。\n"
        )
    return "\n".join(parts).encode("utf-8")


async def cleanup() -> None:
    """按 knowledge_id 扫 zz- 库 + 清 zz- 基准/结果。"""
    async with SessionLocal() as db:
        rows = (await db.execute(
            text("SELECT id FROM knowledge WHERE name LIKE 'zz-%'")
        )).scalars().all()
        kids = [int(r) for r in rows]
        await db.execute(text("DELETE FROM eval_rag_result_detail WHERE result_id IN "
                              "(SELECT id FROM eval_rag_result WHERE knowledge_id = ANY(:k))"),
                         {"k": kids})
        await db.execute(text("DELETE FROM eval_rag_result WHERE knowledge_id = ANY(:k)"), {"k": kids})
        await db.execute(text("DELETE FROM eval_rag_benchmark_item WHERE benchmark_id IN "
                              "(SELECT id FROM eval_rag_benchmark WHERE knowledge_id = ANY(:k))"),
                         {"k": kids})
        await db.execute(text("DELETE FROM eval_rag_benchmark WHERE knowledge_id = ANY(:k)"), {"k": kids})
        await db.execute(
            text("DELETE FROM task WHERE name LIKE 'zz-%' OR ref_id IN "
                 "(SELECT id FROM eval_rag_benchmark WHERE knowledge_id = ANY(:k)) "
                 "OR ref_id IN (SELECT id FROM eval_rag_result WHERE knowledge_id = ANY(:k))"),
            {"k": kids},
        )
        await db.execute(text("DELETE FROM embedding WHERE knowledge_id = ANY(:k)"), {"k": kids})
        await db.execute(text("DELETE FROM chunk WHERE knowledge_id = ANY(:k)"), {"k": kids})
        await db.execute(text("DELETE FROM document WHERE knowledge_id = ANY(:k)"), {"k": kids})
        await db.execute(text("DELETE FROM knowledge_member WHERE knowledge_id = ANY(:k)"), {"k": kids})
        await db.execute(text("DELETE FROM knowledge WHERE id = ANY(:k)"), {"k": kids})
        await db.commit()


async def setup_knowledge(c: httpx.AsyncClient) -> tuple[int, list[int]]:
    """建 zz- 库 + 传一个会向量化的小文档，返回 (knowledge_id, chunk_ids)。"""
    kb_name = f"zz-RAG评估自检-{int(time.time())}"
    r = await api(c, "POST", "/api/knowledge", json={"name": kb_name, "type": "pg"})
    if r.get("code") != 200:
        raise RuntimeError(f"建库失败: {r.get('message')}")
    kid = int(r["data"]["id"])
    _KB_IDS.append(kid)

    res = await api(
        c, "POST", f"/api/knowledge/{kid}/documents",
        files={"file": ("zz-eval-src.md", io.BytesIO(_content()), "text/markdown")},
        params={"ocrEnabled": "false"},
    )
    if res.get("code") != 200:
        raise RuntimeError(f"上传失败: {res.get('message')}")

    async with SessionLocal() as db:
        rows = (await db.execute(
            text("SELECT id FROM chunk WHERE knowledge_id = :k AND status='vectorized' ORDER BY id"),
            {"k": kid},
        )).scalars().all()
    return kid, [int(x) for x in rows]


def _jsonl(kid_chunks: list[int]) -> str:
    """两道考题：gold_chunk_ids 指向真实 chunk，保证检索指标能算出非零值。"""
    a, b = kid_chunks[0], kid_chunks[1] if len(kid_chunks) > 1 else kid_chunks[0]
    lines = [
        {"query": "破损的包装应如何投放？", "gold_answer": "清空内容物后投放",
         "gold_chunk_ids": [str(a)]},
        {"query": "大件物品怎么处理？", "gold_answer": "预约上门回收",
         "gold_chunk_ids": [str(b)]},
    ]
    return "\n".join(json.dumps(x, ensure_ascii=False) for x in lines)


async def check_upload_and_list(c: httpx.AsyncClient, kid: int, chunks: list[int]) -> int:
    print("\n[1] 上传 JSONL 基准 / 列表 / 详情")

    bad = await api(
        c, "POST", f"/api/knowledge/{kid}/eval/benchmarks/upload",
        files={"file": ("bad.jsonl", io.BytesIO(b"{not json\n"), "application/octet-stream")},
        data={"name": f"zz-bad-{_STAMP}"},
    )
    ok("非法 JSONL 返回 400", bad.get("code") == 400, str(bad)[:120])

    no_query = await api(
        c, "POST", f"/api/knowledge/{kid}/eval/benchmarks/upload",
        files={"file": ("nq.jsonl", io.BytesIO(b'{"gold_answer":"x"}\n'), "application/octet-stream")},
        data={"name": f"zz-nq-{_STAMP}"},
    )
    ok("缺 query 字段返回 400", no_query.get("code") == 400, str(no_query)[:120])

    res = await api(
        c, "POST", f"/api/knowledge/{kid}/eval/benchmarks/upload",
        files={"file": ("benchmark.jsonl", io.BytesIO(_jsonl(chunks).encode()), "application/octet-stream")},
        data={"name": f"zz-bm-{_STAMP}", "description": "自检基准"},
    )
    ok("上传成功", res.get("code") == 200, str(res)[:150])
    bm = data(res) or {}
    ok("questionCount=2", bm.get("questionCount") == 2, str(bm.get("questionCount")))
    ok("出参 status 是小写 ready", bm.get("status") == "ready", str(bm.get("status")))
    ok("id 是字符串", isinstance(bm.get("id"), str))
    bm_id = int(bm["id"])

    lst = await api(c, "GET", f"/api/knowledge/{kid}/eval/benchmarks")
    items = data(lst) or []
    ok("列表含新建基准", any(int(x["id"]) == bm_id for x in items))
    ok("列表带 hasGoldChunks/hasGoldAnswer 徽标",
       all({"hasGoldChunks", "hasGoldAnswer"} <= set(x.keys()) for x in items),
       str(sorted(items[0].keys()))[:150] if items else "")
    ok("hasGoldChunks=True（gold 指向真实 chunk）",
       items and items[0].get("hasGoldChunks") is True, str(items[0].get("hasGoldChunks")) if items else "")

    detail = await api(c, "GET", f"/api/knowledge/{kid}/eval/benchmarks/{bm_id}?pageSize=1")
    ok("详情分页 pageSize=1 生效", len(data(detail) or []) == 1)
    detail_all = await api(c, "GET", f"/api/knowledge/{kid}/eval/benchmarks/{bm_id}")
    rows = data(detail_all) or []
    uploaded_gold = json.loads(_jsonl(chunks).splitlines()[0])["gold_chunk_ids"]
    ok("gold_chunk_ids 往返无损（JSON 数组字符串）",
       rows and json.loads(rows[0]["goldChunkIds"]) == uploaded_gold,
       str(rows[0].get("goldChunkIds"))[:80] if rows else "")

    dl = await api(c, "GET", f"/api/knowledge/{kid}/eval/benchmarks/{bm_id}/download")
    body = dl.get("_bytes") or b""
    ok("下载回 JSONL 字节流", body and b"query" in body, f"len={len(body)}")
    lines = [json.loads(x) for x in body.decode().splitlines() if x.strip()]
    ok("下载内容与上传一致",
       len(lines) == 2 and lines[0]["gold_chunk_ids"] == uploaded_gold)

    return bm_id


async def poll_result(c: httpx.AsyncClient, kid: int, result_id: str, timeout: float = 120) -> dict:
    """轮询评估结果直到终态。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        # 详情接口出的是明细页；结果状态要从 results 列表里看
        lst = await api(c, "GET", f"/api/knowledge/{kid}/eval/results?pageSize=50")
        for row in data(lst) or []:
            if row.get("id") == str(result_id):
                if row.get("status") in ("COMPLETED", "FAILED"):
                    return row
        await asyncio.sleep(2)
    return {"status": "TIMEOUT"}


async def check_run(c: httpx.AsyncClient, kid: int, bm_id: int) -> None:
    print("\n[2] 运行评估（不配 answer/judge，只花 embedding）")

    res = await api(c, "POST", f"/api/knowledge/{kid}/eval/run",
                    json={"benchmarkId": str(bm_id)})
    ok("run 返回 200", res.get("code") == 200, str(res)[:150])
    task = data(res) or {}
    ok("返回的是 **Task 记录**（不是 result 本身，type 是小写 code）",
       task.get("type") == "rag_evaluation" and "benchmarkId" in (task.get("payload") or {}),
       str({k: task.get(k) for k in ("type", "status")}))
    ok("Task payload 含 resultId/benchmarkId/knowledgeId",
       all(k in (task.get("payload") or {}) for k in ("resultId", "benchmarkId", "knowledgeId")))
    result_id = (task.get("payload") or {}).get("resultId")

    final = await poll_result(c, kid, result_id)
    ok("评估终态 COMPLETED（大写）", final.get("status") == "COMPLETED",
       f"status={final.get('status')} error={final.get('error')}")
    ok("overallScore 在 [0,1]",
       final.get("overallScore") is not None and 0.0 <= final.get("overallScore") <= 1.0,
       str(final.get("overallScore")))

    retrieval = json.loads(final.get("retrievalJson") or "{}")
    ok("retrievalJson 恰好 8 个键（recall/f1 @1,3,5,10）",
       len(retrieval) == 8 and all(k.startswith(("recall@", "f1@")) for k in retrieval),
       str(sorted(retrieval.keys())))
    ok("retrievalJson **不含 precision**（Java 聚合口径）",
       all(not k.startswith("precision@") for k in retrieval), str(sorted(retrieval.keys())))
    ok("不配 judgeProvider 时 answerJson 为空",
       not final.get("answerJson"), str(final.get("answerJson"))[:80])
    ok("configJson 记录了 null 配置", final.get("configJson") and "null" in final.get("configJson"),
       str(final.get("configJson"))[:120])
    ok("durationMs > 0", (final.get("durationMs") or 0) > 0, str(final.get("durationMs")))

    # 明细
    det = await api(c, "GET", f"/api/knowledge/{kid}/eval/results/{result_id}?pageSize=100")
    rows = data(det) or []
    ok("明细 2 条", len(rows) == 2, f"len={len(rows)}")  # noqa: F841
    ok("明细 retrieved_chunk_ids 非空（向量真检索到了）",
       all(json.loads(r["retrievedChunkIds"]) for r in rows),
       str([r.get("retrievedChunkIds") for r in rows[:1]]))
    ok("明细 answer_score 为 NULL（没配 judge）",
       all(r.get("answerScore") is None for r in rows),
       str([r.get("answerScore") for r in rows]))
    ok("明细 retrieval_scores 是 JSON 文本且带 precision@1",
       rows and "precision@1" in (rows[0].get("retrievalScores") or ""),
       str(rows[0].get("retrievalScores"))[:120] if rows else "")

    err_only = await api(c, "GET",
                         f"/api/knowledge/{kid}/eval/results/{result_id}?errorOnly=true&pageSize=100")
    ok("errorOnly 过滤生效（NULL 也算问题明细）",
       len(data(err_only) or []) == 2, f"len={len(data(err_only) or [])}")

    hist = await api(c, "GET", f"/api/knowledge/{kid}/eval/results")
    ok("历史列表含该结果", any(r.get("id") == str(result_id) for r in (data(hist) or [])))
    return result_id


async def check_delete(c: httpx.AsyncClient, kid: int, bm_id: int, result_id: str) -> None:
    print("\n[3] 删除基准 / 结果")

    r = await api(c, "DELETE", f"/api/knowledge/{kid}/eval/results/{result_id}")
    ok("删除结果 200", r.get("code") == 200, str(r)[:120])
    hist = await api(c, "GET", f"/api/knowledge/{kid}/eval/results")
    ok("删除后历史里没了", not any(x.get("id") == str(result_id) for x in (data(hist) or [])))

    async with SessionLocal() as db:
        left = (await db.execute(text(
            "SELECT count(*) FROM eval_rag_result_detail WHERE result_id = :r"), {"r": int(result_id)}
        )).scalar_one()
    ok("明细被级联物理删", left == 0, str(left))

    r = await api(c, "DELETE", f"/api/knowledge/{kid}/eval/benchmarks/{bm_id}")
    ok("删除基准 200", r.get("code") == 200, str(r)[:120])
    lst = await api(c, "GET", f"/api/knowledge/{kid}/eval/benchmarks")
    ok("删除后列表里没了", not any(int(x["id"]) == bm_id for x in (data(lst) or [])))


async def check_llm_path(c: httpx.AsyncClient, kid: int, chunks: list[int]) -> None:
    """--llm 段：真调模型生成基准。默认跳过（花钱）。"""
    print("\n[4] AI 生成基准（真调模型）")
    res = await api(c, "POST", f"/api/knowledge/{kid}/eval/benchmarks/generate",
                    json={"name": f"zz-gen-{_STAMP}", "count": 2})
    ok("generate 返回 Task 记录", res.get("code") == 200
       and (data(res) or {}).get("type") == "benchmark_generate", str(res)[:200])
    task = data(res) or {}
    task_id = task.get("id")

    deadline = time.time() + 300
    status = "running"
    while time.time() < deadline:
        tasks = await api(c, "GET", "/api/tasks?pageNum=1&pageSize=50")
        records = (data(tasks) or {}).get("records") or []
        row = next((t for t in records if t.get("id") == str(task_id)), None)
        if row is None:
            break
        status = row.get("status")
        if status in ("success", "failed", "cancelled"):
            break
        await asyncio.sleep(3)
    ok("生成任务终态 success", status == "success", f"status={status}")
    # 后续断言（题目质量）人工看，脚本只确认链路通


async def main() -> int:
    global failures
    run_llm = "--llm" in sys.argv

    await cleanup()
    async with httpx.AsyncClient(timeout=120, trust_env=False) as c:
        kid, chunks = await setup_knowledge(c)
        if not chunks:
            print("❌ 向量化失败，无法继续")
            return 1
        bm_id = await check_upload_and_list(c, kid, chunks)
        result_id = await check_run(c, kid, bm_id)
        await check_delete(c, kid, bm_id, result_id)
        if run_llm:
            await check_llm_path(c, kid, chunks)
        await cleanup()

    print(f"\n{'=' * 60}")
    print(f"失败 {failures} 项")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
