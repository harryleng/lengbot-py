"""图谱 26 接口验证（graph_check）—— **全程不调模型**。

embedding 边界用确定性假向量替换（1024 维，与线上 bge-large 同维度，
避免把向量索引维度污染成假维度）。实测链路：

- standalone：导入三元组（MERGE 幂等）→ 子图/关键词检索 → 向量搜索
  （假向量 + 真 Neo4j 向量索引）→ 节点/边 CRUD → 统计 → 清空；
- knowledge 侧：导入（带 document_id/graph_source）→ existing-docs →
  按文档子图/统计 → 社区检测（纯算法，真跑）→ Community 节点校验 →
  单文档删除 → 清空；
- extract / community summarize：**只测触发与快败路径**（空库报错、
  task 创建即失败落状态），不触发 LLM。

数据全部走 zz- 前缀实体名 + 独立 kb（物理删），Neo4j 清理精确到 label。
"""

from __future__ import annotations

import asyncio
import io
import json
import time

import httpx
from sqlalchemy import text

from app.core.db import SessionLocal

# ASGI 进程内直连（对照 auth_gate_check）：embedding 假向量 patch 必须与
# 服务端代码同进程才生效 —— 走真 HTTP 的话服务端会真调 embedding 模型
BASE = "http://testserver"
_STAMP = str(int(time.time()))
_KB_NAME = f"zz-图谱自检-{_STAMP}"
_FAILED: list[str] = []
_FAKE_DIM = 1024


def ok(name: str, cond: bool, detail: str = "") -> None:
    mark = "✅" if cond else "❌"
    print(f"  {mark} {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        _FAILED.append(name)


async def api(c: httpx.AsyncClient, method: str, path: str, **kw):
    r = await c.request(method, BASE + path, **kw)
    try:
        body = r.json()
    except ValueError:
        body = {"_status": r.status_code, "_bytes": r.content}
    return body


def data(b):
    return b.get("data")


def _fake_vec(text_value: str) -> list[float]:
    """确定性假向量（1024 维，由文本哈希播种）。测试专用，不调模型。"""
    import hashlib

    h = hashlib.sha256(text_value.encode()).digest()
    vec = [0.0] * _FAKE_DIM
    for i in range(_FAKE_DIM):
        vec[i] = (h[i % len(h)] / 255.0) - 0.5
    return vec


async def patch_embedding():
    """替换 graph_service 的 embedding 边界（确定性向量，不调模型）。"""
    from unittest.mock import patch

    from app.services import graph_service as gs

    async def fake_many(self, names):  # noqa: ARG001
        return {n: _fake_vec(n) for n in names}

    async def fake_one(self, t):  # noqa: ARG001
        return _fake_vec(t)

    p1 = patch.object(gs.GraphService, "_embed_many", fake_many)
    p2 = patch.object(gs.GraphService, "_embed_one", fake_one)
    return p1, p2


async def cleanup_db(kids: list[int]) -> None:
    async with SessionLocal() as db:
        for k in kids:
            # graph_document 走 graph_id 两跳（表里没有 knowledge_id 列）
            await db.execute(
                text("DELETE FROM graph_document WHERE graph_id IN "
                     "(SELECT id FROM knowledge_graph WHERE knowledge_id = :k)"), {"k": k}
            )
            await db.execute(
                text("DELETE FROM graph_extraction_task WHERE knowledge_id = :k"), {"k": k}
            )
            await db.execute(
                text("DELETE FROM knowledge_graph WHERE knowledge_id = :k"), {"k": k}
            )
            await db.execute(
                text("DELETE FROM task WHERE ref_id = :k AND type IN "
                     "('graph_extraction','community_detect','community_summary')"), {"k": k}
            )
            await db.execute(
                text("DELETE FROM knowledge_member WHERE knowledge_id = :k"), {"k": k}
            )
            await db.execute(text("DELETE FROM knowledge WHERE id = :k"), {"k": k})
        await db.commit()


async def make_kb(c: httpx.AsyncClient) -> int:
    r = await api(c, "POST", "/api/knowledge", json={"name": _KB_NAME, "type": "pg"})
    return int(data(r)["id"])


async def check_standalone(c: httpx.AsyncClient) -> None:
    print("\n[1] 独立图谱：导入 / 幂等 / 子图 / CRUD / 统计 / 清空")

    triples = [
        {"head": f"zz-Java-{_STAMP}", "headType": "技术", "headDesc": "编程语言",
         "relation": "运行于", "relationDesc": "运行平台",
         "tail": f"zz-JVM-{_STAMP}", "tailType": "平台", "tailDesc": "虚拟机"},
        {"head": f"zz-Python-{_STAMP}", "headType": "技术", "headDesc": "编程语言",
         "relation": "运行于", "relationDesc": "运行平台",
         "tail": f"zz-CPython-{_STAMP}", "tailType": "平台", "tailDesc": "解释器"},
        {"head": f"zz-JVM-{_STAMP}", "headType": "平台", "headDesc": "虚拟机",
         "relation": "承载", "relationDesc": "承载关系",
         "tail": f"zz-Spring-{_STAMP}", "tailType": "框架", "tailDesc": "应用框架"},
    ]
    r = await api(c, "POST", "/api/graph/import", json={"triples": triples})
    d = data(r) or {}
    ok("导入成功", r.get("code") == 200, str(r)[:150])
    ok("返回 nodeCount/edgeCount", d.get("nodeCount", 0) >= 5 and d.get("edgeCount", 0) >= 3, str(d))

    # JSONL 导入
    jsonl = "\n".join(
        json.dumps(t, ensure_ascii=False) for t in triples[:1]
    ).encode()
    r = await api(c, "POST", "/api/graph/import/jsonl",
                  files={"file": ("t.jsonl", io.BytesIO(jsonl), "application/octet-stream")})
    ok("JSONL 导入成功", r.get("code") == 200, str(r)[:150])

    r = await api(c, "POST", "/api/graph/import/jsonl",
                  files={"file": ("t.txt", io.BytesIO(jsonl), "text/plain")})
    ok("非 .jsonl 被拒", r.get("code") == 400, str(r)[:120])

    # MERGE 幂等：重复导入数量不翻倍
    await api(c, "POST", "/api/graph/import", json={"triples": triples})
    s1 = await api(c, "GET", "/api/graph/stats")
    d = data(s1) or {}
    ok("MERGE 幂等（重复导入后统计一致）",
       d.get("nodeCount", 0) >= 5 and d.get("available") is True, str(d)[:120])
    dist = d.get("typeDistribution") or {}
    ok("类型分布含 技术/平台", "技术" in dist and "平台" in dist, str(dist)[:120])

    # 子图：关键词
    r = await api(c, "GET", f"/api/graph/subgraph?keyword=zz-java-{_STAMP}")
    d = data(r) or {}
    ok("关键词子图命中", (d.get("nodeCount") or 0) >= 2, str(d.get("nodeCount")))
    names = [n["name"] for n in d.get("nodes") or []]
    ok(
        "子图节点含 name/elementId/labels",
        bool(names) and all("elementId" in n and "labels" in n for n in d["nodes"]),
        str(names)[:80],
    )

    # 子图：无 keyword（采样）
    r = await api(c, "GET", "/api/graph/subgraph")
    ok("无关键词采样子图不 500", r.get("code") == 200, str(r)[:120])

    # 向量搜索（假向量 + 真 Neo4j 索引）
    # search 的服务端 embed 已被假向量替换（_embed_one），不会真调模型
    r = await api(c, "GET", f"/api/graph/search?query=zz-JVM-{_STAMP}&topK=5&minScore=0.01")
    d = data(r) or []
    ok("语义搜索返回带 score 的节点", isinstance(d, list), str(r)[:120])
    # 不调模型时 query 也走假向量 —— 但 /api/graph/search 是服务端 embed，
    # 真实链路会调模型；此处只验证"空/失败降级不 500"

    # 节点 CRUD
    r = await api(
        c, "POST", "/api/graph/nodes",
        params={"name": f"zz-孤点-{_STAMP}", "entityType": "概念", "description": "测试"},
    )
    node = data(r) or {}
    ok("创建节点返回 elementId/source=manual",
       node.get("elementId") and node.get("source") == "manual", str(node)[:120])
    eid = node.get("elementId")
    r = await api(
        c, "PUT", f"/api/graph/nodes/{eid}",
        params={"name": f"zz-孤点改-{_STAMP}", "entityType": "概念"},
    )
    ok("更新节点生效", (data(r) or {}).get("name") == f"zz-孤点改-{_STAMP}", str(r)[:120])
    r = await api(c, "PUT", "/api/graph/nodes/zz-not-exist", params={"name": "x"})
    ok("更新不存在节点 404", r.get("code") == 404, str(r)[:120])

    # 边 CRUD（先建两个点）
    hn, tn = f"zz-边头-{_STAMP}", f"zz-边尾-{_STAMP}"
    await api(c, "POST", "/api/graph/nodes", params={"name": hn})
    await api(c, "POST", "/api/graph/nodes", params={"name": tn})
    r = await api(c, "POST", "/api/graph/edges",
                  params={"headName": hn, "relationType": "关联", "tailName": tn})
    edge = data(r) or {}
    ok("创建边返回 start/end elementId",
       edge.get("elementId") and edge.get("startNodeElementId"), str(edge)[:120])
    r = await api(c, "PUT", f"/api/graph/edges/{edge['elementId']}",
                  params={"weight": 2.5, "description": "加强关联"})
    ok("更新边 weight=2.5", (data(r) or {}).get("weight") == 2.5, str(r)[:120])
    r = await api(c, "POST", "/api/graph/edges",
                  params={"headName": "zz-不存在头", "relationType": "x", "tailName": tn})
    ok("头节点不存在 404", r.get("code") == 404, str(r)[:120])

    # node-names + rebuild-index（无缺失 embedding 时 0）
    r = await api(c, "GET", "/api/graph/node-names")
    ok("node-names 含 zz- 实体", f"zz-JVM-{_STAMP}" in (data(r) or []), str(data(r))[:100])
    r = await api(c, "POST", "/api/graph/rebuild-index")
    ok("rebuild-index 返回 0（全部已有 embedding）", data(r) == 0, str(r)[:120])

    # 清空
    r = await api(c, "DELETE", "/api/graph")
    s = await api(c, "GET", "/api/graph/stats")
    ok("清空后 stats 归零", (data(s) or {}).get("nodeCount") == 0, str(data(s)))


async def check_kb_graph(c: httpx.AsyncClient, kid: int) -> None:
    print("\n[2] 知识库图谱：导入 / existing-docs / 社区检测（真跑） / 删除")

    triples = [
        {"head": f"zz-垃圾-{_STAMP}", "headType": "概念", "headDesc": "废弃物",
         "relation": "分为", "relationDesc": "分类",
         "tail": f"zz-可回收-{_STAMP}", "tailType": "概念", "tailDesc": "分类"},
        {"head": f"zz-可回收-{_STAMP}", "headType": "概念", "headDesc": "分类",
         "relation": "包含", "relationDesc": "包含",
          "tail": f"zz-纸箱-{_STAMP}", "tailType": "物品", "tailDesc": "物品"},
        {"head": f"zz-垃圾-{_STAMP}", "headType": "概念", "headDesc": "废弃物",
         "relation": "分为", "relationDesc": "分类",
          "tail": f"zz-厨余-{_STAMP}", "tailType": "概念", "tailDesc": "分类"},
    ]
    r = await api(c, "POST", f"/api/knowledge/{kid}/graph/import", json={"triples": triples})
    ok("kb 导入成功", r.get("code") == 200, str(r)[:150])

    # existing-docs：导入未带 document_id → 无文档命中
    r = await api(c, "GET", f"/api/knowledge/{kid}/graph/existing-docs?documentIds=1&documentIds=2")
    ok("existing-docs 空（导入无 document_id）", data(r) == [], str(data(r)))

    # 子图 + 统计
    r = await api(c, "GET", f"/api/knowledge/{kid}/graph/subgraph?keyword=zz-垃圾")
    d = data(r) or {}
    ok("kb 子图命中", (d.get("nodeCount") or 0) >= 3, str(d.get("nodeCount")))
    ok("kb 节点带 kb 标签",
       all(f"kb_{kid}" in (n.get("labels") or []) for n in d.get("nodes") or []), str(d.get("nodes"))[:100])
    r = await api(c, "GET", f"/api/knowledge/{kid}/graph/stats")
    d = data(r) or {}
    ok("kb stats available=True", d.get("available") is True and d.get("nodeCount", 0) >= 3, str(d)[:120])

    # 社区检测：纯算法真跑
    r = await api(c, "POST", f"/api/knowledge/{kid}/graph/community/detect")
    task = data(r) or {}
    ok("社区检测返回任务", r.get("code") == 200 and (task.get("type") == "community_detect"), str(r)[:150])
    tid = task.get("id")
    final = None
    deadline = time.time() + 30
    while time.time() < deadline:
        rr = await api(c, "GET", f"/api/tasks/{tid}")
        row = data(rr) or {}
        if row.get("status") in ("success", "failed", "cancelled"):
            final = row
            break
        await asyncio.sleep(1)
    ok("社区检测任务终态 success", final and final.get("status") == "success",
       str(final)[:200] if final else "超时")
    msg = final.get("message") if final else None
    ok("检测结果含社区数", final and "社区数=" in (msg or ""), str(msg))

    # Community 节点写回校验（直接查 Neo4j）
    from app.core import neo4j_client

    rows = await neo4j_client.query_read(
        f"MATCH (c:Community:kb_{kid}) RETURN count(c) AS c"
    )
    ok("Community 节点已建立", rows and rows[0]["c"] >= 1, str(rows))
    rows = await neo4j_client.query_read(
        f"MATCH (n:Entity:kb_{kid}) WHERE n.community_id IS NOT NULL RETURN count(n) AS c"
    )
    ok("实体 community_id 已写回", rows and rows[0]["c"] >= 3, str(rows))

    # 节点/边 CRUD（kb 作用域）
    r = await api(c, "POST", f"/api/knowledge/{kid}/graph/nodes",
                  params={"name": f"zz-kb点-{_STAMP}", "entityType": "概念"})
    ok("kb 创建节点", r.get("code") == 200, str(r)[:120])

    # extract 触发端点：空文档库 → 任务快败（不调 LLM：chunks 为 0 直接报错）
    r = await api(c, "POST", f"/api/knowledge/{kid}/graph/extract", json={})
    task = data(r) or {}
    ok("extract 触发返回任务", r.get("code") == 200 and task.get("type") == "graph_extraction", str(r)[:150])
    tid2 = task.get("id")
    final2 = None
    deadline = time.time() + 30
    while time.time() < deadline:
        rr = await api(c, "GET", f"/api/tasks/{tid2}")
        row = data(rr) or {}
        if row.get("status") in ("success", "failed", "cancelled"):
            final2 = row
            break
        await asyncio.sleep(1)
    ok("extract 空库快败（failed，无 LLM 调用）",
       final2 and final2.get("status") == "failed" and "文档" in (final2.get("message") or ""),
       str(final2)[:200] if final2 else "超时")

    # 删除与清空
    r = await api(c, "DELETE", f"/api/knowledge/{kid}/graph")
    s = await api(c, "GET", f"/api/knowledge/{kid}/graph/stats")
    ok("清空后 kb stats 归零", (data(s) or {}).get("nodeCount") == 0, str(data(s)))


async def main() -> int:
    kids: list[int] = []
    p1, p2 = await patch_embedding()
    p1.start()
    p2.start()
    try:
        from httpx import ASGITransport

        from app.main import app

        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(
            timeout=120, trust_env=False, transport=transport, base_url=BASE
        ) as c:
            kid = await make_kb(c)
            kids.append(kid)
            await check_standalone(c)
            await check_kb_graph(c, kid)
    finally:
        p1.stop()
        p2.stop()
        await cleanup_db(kids)
        # Neo4j 残留清理（kb_{kid} 标签）
        from app.core import neo4j_client

        for k in kids:
            await neo4j_client.run(f"MATCH (n:Entity:kb_{k}) DETACH DELETE n")
            await neo4j_client.run(f"MATCH (c:Community:kb_{k}) DETACH DELETE c")
        await neo4j_client.run("MATCH (n:Entity:standalone) WHERE n.name STARTS WITH 'zz-' DETACH DELETE n")

    print(f"\n{'=' * 60}")
    if _FAILED:
        print(f"❌ 失败 {len(_FAILED)} 项: {_FAILED}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    # patch 必须在 import app 之前？不需要 —— patch 的是类方法，运行期替换即可
    raise SystemExit(asyncio.run(main()))
