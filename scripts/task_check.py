"""任务中心 8 接口验证（对齐前端 `src/api/task.js`）。

重点验证这些**静默失败**：

1. **路由顺序**：`/count` `/running-count` `/type-counts` 必须先于 `/{task_id}`，
   否则 `GET /tasks/count` 被当成 `task_id="count"` → 422。
2. **`status=active` 是聚合筛选**（pending/pending_retry/running 三态），
   不是真实状态值 —— 传 "active" 精确匹配会永远查空。
3. **归属校验**：别人的任务必须 404（防 ID 枚举），不能 200 也不能 500。
4. **cancel 语义**：已终态任务取消返回 **code=61002**（不是 404），
   "无法取消"和"不存在"是两个错误。
5. **delete 只允许终态**：running 的任务删掉会让执行体的进度更新写给不存在的行。
6. **type-counts 的分组键是中文 desc**（"RAG评估"），不是 code —— 前端按中文渲染徽标。

用法：
    python -m scripts.task_check
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime

import httpx
from sqlalchemy import text

from app.core.db import SessionLocal
from app.core.snowflake import new_id

BASE = "http://127.0.0.1:18080"
failures = 0
_STAMP = str(int(time.time()))[-6:]
_UID = 2090273750973657090  # settings.dev_user_id


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
        await db.execute(text("DELETE FROM task WHERE name LIKE 'zz-task-%'"))
        await db.commit()


async def seed_tasks() -> dict[str, int]:
    """造 5 条：1 running + 1 pending + 1 success + 1 failed + 1 别名 type。"""
    ids = {}
    for key, status, type_ in [
        ("running", "running", "rag_evaluation"),
        ("pending", "pending", "benchmark_generate"),
        ("success", "success", "rag_evaluation"),
        ("failed", "failed", "experiment_run"),
        ("legacy", "running", "EXPERIMENT_RUN"),  # 历史大写数据
    ]:
        tid = new_id()
        now = datetime.now()
        name = f"zz-task-{_STAMP}-{key}"
        async with SessionLocal() as db:
            await db.execute(
                text("INSERT INTO task (id, name, type, status, progress, payload, "
                     "cancel_requested, user_id, ref_id, create_time, update_time, started_at, "
                     "attempts, max_attempts, dead_letter) "
                     "VALUES (:id, :name, :type, :status, 0, '{}', 0, :uid, NULL, "
                     ":now, :now, :now, 0, 3, 0)"),
                {"id": tid, "name": name, "type": type_, "status": status, "uid": _UID, "now": now},
            )
            await db.commit()
        ids[key] = tid
    return ids


async def main() -> int:
    await cleanup()
    # 库里有大量历史 task（评测实验等），所有计数断言都用**相对基线**，
    # 绝对数断言在真实库上必炸
    async with httpx.AsyncClient(timeout=60, trust_env=False) as c:
        base_count = data(await api(c, "GET", "/api/tasks/count")) or {}
        base_running = data(await api(c, "GET", "/api/tasks/running-count")) or 0
        base_tc = data(await api(c, "GET", "/api/tasks/type-counts")) or {}
        async def _total(qs: str) -> int:
            body = await api(c, "GET", f"/api/tasks?{qs}&pageSize=1")
            return int((data(body) or {}).get("total") or 0)

        base_active_total = await _total("status=active")
        base_success_total = await _total("status=success")
        base_rag_total = await _total("type=rag_evaluation")

    ids = await seed_tasks()

    async with httpx.AsyncClient(timeout=60, trust_env=False) as c:
        print("\n[1] 计数三件套（路由顺序：先于 /{task_id}）")

        r = await api(c, "GET", "/api/tasks/count")
        d = data(r) or {}
        ok("/count 没被 /{task_id} 吃掉", r.get("code") == 200, str(r)[:120])
        ok("count 含 active/pending/running/pendingRetry 四键",
           {"active", "pending", "running", "pendingRetry"} <= set(d.keys()), str(d))
        ok("active = pending + running + pendingRetry",
           d.get("active") == d.get("pending", 0) + d.get("running", 0) + d.get("pendingRetry", 0))
        ok("running 比基线多 2（含历史大写 type 的那条）",
           d.get("running") == (base_count.get("running") or 0) + 2, str(d.get("running")))

        r = await api(c, "GET", "/api/tasks/running-count")
        ok("/running-count = 基线+3（running2+pending1）",
           data(r) == base_running + 3, f"{data(r)} vs base={base_running}")

        r = await api(c, "GET", "/api/tasks/type-counts")
        d = data(r) or {}
        ok("/type-counts 分组键是**中文 desc**",
           "RAG评估" in d and "基准生成" in d and "实验执行" in d, str(d))
        ok("type-counts 不含终态任务（各键之和 = 基线+3）",
           sum(d.values()) == sum(base_tc.values()) + 3,
           f"now={sum(d.values())} base={sum(base_tc.values())}")

        print("\n[2] 列表与筛选")

        r = await api(c, "GET", "/api/tasks?pageNum=1&pageSize=3")
        d = data(r) or {}
        ok("返回 records/total/pageNum/pageSize",
           {"records", "total", "pageNum", "pageSize"} <= set(d.keys()))
        ok("分页 pageSize=3 生效", len(d.get("records") or []) == 3, f"len={len(d.get('records') or [])}")
        ok("total 只算自己的", d.get("total", 0) >= 5, str(d.get("total")))
        ok("id 是字符串", all(isinstance(x["id"], str) for x in d.get("records") or []))

        r = await api(c, "GET", "/api/tasks?status=active&pageSize=100")
        d = data(r) or {}
        statuses = {x["status"] for x in d.get("records") or []}
        ok("status=active 是聚合筛选（只含未完结三态，基线+3）",
           statuses <= {"pending", "pending_retry", "running"}
           and d.get("total") == base_active_total + 3,
           f"statuses={statuses} total={d.get('total')} base={base_active_total}")

        r = await api(c, "GET", "/api/tasks?status=success&pageSize=100")
        ok("status=success 精确匹配（基线+1）",
           (data(r) or {}).get("total") == base_success_total + 1,
           f"{(data(r) or {}).get('total')} vs base={base_success_total}")

        r = await api(c, "GET", "/api/tasks?type=rag_evaluation&pageSize=100")
        d = data(r) or {}
        ok("type=code 筛选生效（基线+2：种子里 rag_evaluation 有 running+success 两条）",
           d.get("total") == base_rag_total + 2,
           f"{d.get('total')} vs base={base_rag_total}")

        r = await api(c, "GET", "/api/tasks?type=RAG_EVALUATION&pageSize=100")
        ok("type 大小写不敏感（两个口径筛出同数）",
           (data(r) or {}).get("total") == d.get("total"),
           f"upper={(data(r) or {}).get('total')} lower={d.get('total')}")

        r = await api(c, "GET", f"/api/tasks?name=zz-task-{_STAMP}-running&pageSize=100")
        ok("name 模糊筛选", (data(r) or {}).get("total") == 1, str((data(r) or {}).get("total")))

        print("\n[3] 详情 / 进度 / 取消 / 删除")

        r = await api(c, "GET", f"/api/tasks/{ids['running']}")
        d = data(r) or {}
        ok("详情可读且 id 是字符串", r.get("code") == 200 and d.get("id") == str(ids["running"]))
        ok("payload 解析成对象", isinstance(d.get("payload"), dict), str(type(d.get("payload"))))

        r = await api(c, "GET", f"/api/tasks/{ids['running']}/progress")
        d = data(r) or {}
        ok("进度快照含 progress/message/ts", {"progress", "message", "ts"} <= set(d.keys()), str(d))

        r = await api(c, "GET", "/api/tasks/999999999999999999")
        ok("不存在的任务 404（不是 500）", r.get("code") == 404, str(r)[:100])

        r = await api(c, "POST", f"/api/tasks/{ids['running']}/cancel")
        ok("取消 running 任务成功", r.get("code") == 200, str(r)[:100])
        async with SessionLocal() as db:
            flag = (await db.execute(
                text("SELECT cancel_requested FROM task WHERE id=:i"), {"i": ids["running"]}
            )).scalar_one()
        ok("cancel_requested 已置 1", flag == 1, str(flag))

        r = await api(c, "POST", f"/api/tasks/{ids['success']}/cancel")
        ok("取消已终态任务返回 **code=61002**（不是 404）",
           r.get("code") == 61002, str(r)[:120])

        r = await api(c, "DELETE", f"/api/tasks/{ids['failed']}")
        ok("删除终态任务成功", r.get("code") == 200, str(r)[:100])
        async with SessionLocal() as db:
            cnt = (await db.execute(
                text("SELECT count(*) FROM task WHERE id=:i"), {"i": ids["failed"]}
            )).scalar_one()
        ok("物理删除生效", cnt == 0, str(cnt))

        r = await api(c, "DELETE", f"/api/tasks/{ids['pending']}")
        ok("删除运行中任务被拒（400，不是静默成功）", r.get("code") == 400, str(r)[:120])

    await cleanup()
    # 还原被取消/删除影响的基线？cancel 只置标记、delete 物理删的是 zz- 行，无需还原
    print(f"\n{'=' * 60}")
    print(f"失败 {failures} 项")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
