"""知识图谱服务（对照 Java `StandaloneGraphServiceImpl` + `GraphServiceImpl` 的查询/导入/CRUD 面）。

## 数据模型（与 Java 完全同构，同一张图混跑）

- 实体节点：`(n:Entity:{scope_label})`，scope_label = `standalone` 或 `kb_{knowledgeId}`。
  属性：id（雪花）、name、entity_type、description、source（auto/import/manual）、
  created_at/updated_at、embedding（向量）、knowledge 侧多 document_id / graph_source
  （`single_doc` 抽取 / `merged` 合并抽取 / `import` 手工导入）。
- 关系：`[:RELATION {relation_type, id, description, weight, source}]`。
- 社区节点：`(:Community:{scope_label})`（社区检测执行器写）。
- 向量索引：`standalone_entity_embedding`（standalone 专用，懒创建）。

## 与 Java 的差异（有意）

- embedding 失败不阻断导入：Java `generateEmbeddings` 吞异常跳过失败实体，
  节点照写（无 embedding 属性）—— Python 同款，`_embed_many` 返回部分 map。
- id 生成用 Python 侧 `new_id()`（雪花，不与 Neo4j elementId 混用）。
"""

from __future__ import annotations

import logging
from typing import Any

from app.core import neo4j_client
from app.core.snowflake import new_id

logger = logging.getLogger(__name__)

VECTOR_INDEX_NAME = "standalone_entity_embedding"
DEFAULT_MIN_SCORE = 0.5
DEFAULT_VECTOR_DIMS = 1024  # bge-large-zh-v1.5；embedding 失败时兜底建索引用


def kb_label(knowledge_id: int) -> str:
    return f"kb_{knowledge_id}"


def _q(label: str) -> str:
    """label 进 Cypher 反引号（kb_123 是合法标识符，但防御性引用）。"""
    return f"`{label}`"


class GraphService:
    """standalone=True 走全局标签；否则走 kb_{knowledge_id}。"""

    def __init__(self, scope_label: str) -> None:
        self.label = scope_label

    # ---------------- 导入 ----------------

    async def import_triples(
        self, triples: list[dict[str, Any]], knowledge_id: int | None = None,
        document_id: int | None = None, source: str = "import",
        graph_source: str | None = None,
    ) -> dict:
        """MERGE 幂等写入。embed 失败的实体照写（Java 同款）。

        knowledge 侧带 document_id + graph_source（single_doc/import）；
        standalone 侧两者为 None。
        """
        await neo4j_client.ensure_available()

        names: list[str] = []
        seen: set[str] = set()
        for t in triples:
            for key in ("head", "tail"):
                v = (t.get(key) or "").strip()
                if v and v not in seen:
                    seen.add(v)
                    names.append(v)
        embeddings = await self._embed_many(names)

        if embeddings:
            dims = len(next(iter(embeddings.values())))
            await self._ensure_vector_index(dims)
        else:
            # 全部 embed 失败也要把索引建好（Java 用 1536 兜底；Python 用真实维度）
            await self._ensure_vector_index(DEFAULT_VECTOR_DIMS)

        nodes = 0
        edges = 0
        for t in triples:
            head = (t.get("head") or "").strip()
            tail = (t.get("tail") or "").strip()
            relation = (t.get("relation") or "").strip()
            if not head or not relation or not tail:
                continue
            await self._merge_node(head, t.get("headType") or "其他",
                                   t.get("headDesc") or "", embeddings.get(head),
                                   knowledge_id, document_id, source, graph_source)
            await self._merge_node(tail, t.get("tailType") or "其他",
                                   t.get("tailDesc") or "", embeddings.get(tail),
                                   knowledge_id, document_id, source, graph_source)
            nodes += 2
            edges += await self._merge_edge(head, relation, tail,
                                            t.get("relationDesc") or "", source,
                                            knowledge_id, document_id, graph_source)
        return {"nodeCount": nodes, "edgeCount": edges, "typeDistribution": {}}

    async def _merge_node(
        self, name: str, entity_type: str, description: str,
        embedding: list[float] | None, knowledge_id: int | None,
        document_id: int | None, source: str, graph_source: str | None,
    ) -> None:
        cypher = (
            f"MERGE (n:Entity:{_q(self.label)} {{name: $name}})\n"
            "ON CREATE SET n.id = $nodeId, n.entity_type = $entityType,\n"
            "              n.description = $description, n.source = $source,\n"
            "              n.created_at = datetime(), n.updated_at = datetime()\n"
            "ON MATCH SET n.entity_type = $entityType, n.description = $description,\n"
            "             n.updated_at = datetime()\n"
        )
        params: dict[str, Any] = {
            "name": name, "nodeId": str(new_id()), "entityType": entity_type,
            "description": description, "source": source,
        }
        if knowledge_id is not None:
            cypher += (
                "ON CREATE SET n.knowledge_id = $kid\n"
                "SET n.knowledge_id = $kid\n"
            )
            params["kid"] = knowledge_id
        if document_id is not None:
            cypher += "SET n.document_id = $docId, n.graph_source = $gsrc\n"
            params["docId"] = str(document_id)
            params["gsrc"] = graph_source or source
        await neo4j_client.query_write(cypher + "RETURN n", params)
        if embedding:
            await neo4j_client.run(
                f"MATCH (n:Entity:{_q(self.label)} {{name: $name}}) SET n.embedding = $emb",
                {"name": name, "emb": embedding},
            )

    async def _merge_edge(
        self, head: str, relation: str, tail: str, description: str,
        source: str, knowledge_id: int | None, document_id: int | None,
        graph_source: str | None,
    ) -> int:
        cypher = (
            f"MATCH (h:Entity:{_q(self.label)} {{name: $head}})\n"
            f"MATCH (t:Entity:{_q(self.label)} {{name: $tail}})\n"
            "MERGE (h)-[r:RELATION {relation_type: $relation}]->(t)\n"
            "ON CREATE SET r.id = $relId, r.description = $description, r.weight = 1.0,\n"
            "              r.source = $source, r.created_at = datetime()\n"
            "ON MATCH SET r.description = $description, r.updated_at = datetime()\n"
        )
        params: dict[str, Any] = {
            "head": head, "tail": tail, "relation": relation,
            "relId": str(new_id()), "description": description, "source": source,
        }
        if knowledge_id is not None:
            cypher += "SET r.knowledge_id = $kid\n"
            params["kid"] = knowledge_id
        if document_id is not None:
            cypher += "SET r.document_id = $docId, r.graph_source = $gsrc\n"
            params["docId"] = str(document_id)
            params["gsrc"] = graph_source or source
        cypher += "RETURN elementId(r) AS eid"
        rows = await neo4j_client.query_write(cypher, params)
        return len(rows)

    # ---------------- 查询 ----------------

    async def get_subgraph(
        self, keyword: str | None, max_depth: int, max_nodes: int,
        document_id: int | None = None,
    ) -> dict:
        if not await neo4j_client.is_available():
            return {"nodes": [], "edges": [], "nodeCount": 0, "edgeCount": 0}
        if max_nodes <= 0:
            max_nodes = 50

        if document_id is not None:
            records = await self._query_by_document(str(document_id), max_nodes)
        elif keyword:
            records = await self._query_by_keyword(keyword, max_nodes)
        else:
            records = await self._query_sample(max_nodes)
        return self._build_subgraph(records)

    async def _query_by_keyword(self, keyword: str, max_nodes: int) -> list[dict]:
        cypher = (
            f"MATCH (n:Entity:{_q(self.label)})\n"
            "WHERE toLower(n.name) CONTAINS toLower($keyword)\n"
            "WITH n LIMIT $maxNodes\n"
            f"OPTIONAL MATCH (n)-[r]-(m:Entity:{_q(self.label)})\n"
            "RETURN n, r, m"
        )
        return await neo4j_client.query_read(cypher, {"keyword": keyword, "maxNodes": max_nodes})

    async def _query_by_document(self, document_id: str, max_nodes: int) -> list[dict]:
        cypher = (
            f"MATCH (n:Entity:{_q(self.label)})\n"
            "WHERE n.document_id = $docId\n"
            "WITH n LIMIT $maxNodes\n"
            f"OPTIONAL MATCH (n)-[r]-(m:Entity:{_q(self.label)})\n"
            "RETURN n, r, m"
        )
        return await neo4j_client.query_read(cypher, {"docId": document_id, "maxNodes": max_nodes})

    async def _query_sample(self, max_nodes: int) -> list[dict]:
        cypher = (
            f"MATCH (seed:Entity:{_q(self.label)})-[r]-(neighbor:Entity:{_q(self.label)})\n"
            "WITH seed, count(r) AS degree, collect(r) AS rels, collect(neighbor) AS neighbors\n"
            "ORDER BY degree DESC\n"
            "LIMIT 5\n"
            "UNWIND range(0, size(rels)-1) AS idx\n"
            "WITH seed, rels[idx] AS r, neighbors[idx] AS neighbor\n"
            "RETURN seed AS n, r, neighbor AS m\n"
            "LIMIT $maxNodes"
        )
        records = await neo4j_client.query_read(cypher, {"maxNodes": max_nodes})
        remain = max_nodes - len(records)
        if remain > 0:
            isolated = (
                f"MATCH (n:Entity:{_q(self.label)}) WHERE NOT (n)-[]-()\n"
                "RETURN n, null AS r, null AS m LIMIT $remain"
            )
            records += await neo4j_client.query_read(isolated, {"remain": remain})
        return records

    def _build_subgraph(self, records: list[dict]) -> dict:
        node_map: dict[str, dict] = {}
        edge_map: dict[str, dict] = {}
        for rec in records:
            n = rec.get("n")
            if n is not None and not _is_null(n):
                node_map.setdefault(n.element_id, _node_vo(n))
            m = rec.get("m")
            if m is not None and not _is_null(m):
                node_map.setdefault(m.element_id, _node_vo(m))
            r = rec.get("r")
            if r is not None and not _is_null(r):
                edge_map.setdefault(r.element_id, _edge_vo(r))
        return {
            "nodes": list(node_map.values()),
            "edges": list(edge_map.values()),
            "nodeCount": len(node_map),
            "edgeCount": len(edge_map),
        }

    async def semantic_search(
        self, query: str, top_k: int, min_score: float | None,
        document_id: int | None = None,
        standalone_only: bool = False,
    ) -> list[dict]:
        """向量检索节点。embedding 失败返回空列表（Java 同款，不抛错）。

        standalone 侧要求 `'standalone' IN labels(node)`；
        kb 侧按 kb 标签天然隔离，document_id 再过滤一层。
        """
        await neo4j_client.ensure_available()
        threshold = min_score if (min_score is not None and min_score >= 0) else DEFAULT_MIN_SCORE

        vec = await self._embed_one(query)
        if not vec:
            return []
        await self._ensure_vector_index(len(vec))
        try:
            cypher = (
                "CALL db.index.vector.queryNodes($indexName, $topK, $queryEmbedding)\n"
                "YIELD node, score\n"
                + ("WHERE 'standalone' IN labels(node)\n" if standalone_only
                   else f"WHERE '{self.label}' IN labels(node)\n")
                + "RETURN node, score ORDER BY score DESC"
            )
            records = await neo4j_client.query_read(
                cypher, {"indexName": VECTOR_INDEX_NAME, "topK": top_k, "queryEmbedding": vec}
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Graph] 向量搜索失败: %s", exc)
            return []

        out = []
        for rec in records:
            score = float(rec["score"])
            if score < threshold:
                break
            node = rec["node"]
            vo = _node_vo(node)
            if document_id is not None and vo.get("documentId") != str(document_id):
                continue
            vo["score"] = score
            out.append(vo)
        return out

    async def get_stats(self, document_id: int | None = None) -> dict:
        if not await neo4j_client.is_available():
            return {"nodeCount": 0, "edgeCount": 0, "typeDistribution": {}, "available": False}
        if document_id is not None:
            stats = await self._stats_for_doc(str(document_id))
        else:
            stats = await self._stats_for_label()
        stats["available"] = True
        return stats

    async def _stats_for_label(self) -> dict:
        rows = await neo4j_client.query_read(
            f"MATCH (n:Entity:{_q(self.label)}) WITH count(n) AS nodeCount\n"
            f"OPTIONAL MATCH (:Entity:{_q(self.label)})-[r:RELATION]->(:Entity:{_q(self.label)})\n"
            "RETURN nodeCount, count(r) AS edgeCount"
        )
        node_count = int(rows[0]["nodeCount"]) if rows else 0
        edge_count = int(rows[0]["edgeCount"]) if rows else 0
        dist_rows = await neo4j_client.query_read(
            f"MATCH (n:Entity:{_q(self.label)}) "
            "RETURN n.entity_type AS type, count(*) AS cnt ORDER BY cnt DESC"
        )
        dist = {("其他" if r["type"] is None else str(r["type"])): int(r["cnt"]) for r in dist_rows}
        return {"nodeCount": node_count, "edgeCount": edge_count, "typeDistribution": dist}

    async def _stats_for_doc(self, document_id: str) -> dict:
        rows = await neo4j_client.query_read(
            f"MATCH (n:Entity:{_q(self.label)}) WHERE n.document_id = $docId\n"
            "WITH count(n) AS nodeCount\n"
            f"OPTIONAL MATCH (:Entity:{_q(self.label)})-[r:RELATION]->(:Entity:{_q(self.label)})\n"
            "WHERE r.document_id = $docId\n"
            "RETURN nodeCount, count(r) AS edgeCount",
            {"docId": document_id},
        )
        dist_rows = await neo4j_client.query_read(
            f"MATCH (n:Entity:{_q(self.label)}) WHERE n.document_id = $docId "
            "RETURN n.entity_type AS type, count(*) AS cnt ORDER BY cnt DESC",
            {"docId": document_id},
        )
        dist = {("其他" if r["type"] is None else str(r["type"])): int(r["cnt"]) for r in dist_rows}
        return {
            "nodeCount": int(rows[0]["nodeCount"]) if rows else 0,
            "edgeCount": int(rows[0]["edgeCount"]) if rows else 0,
            "typeDistribution": dist,
        }

    async def get_existing_doc_ids(self, knowledge_id: int, document_ids: list[int]) -> list[int]:
        """哪些文档已有图谱数据（kb 标签下 document_id 存在即算）。"""
        await neo4j_client.ensure_available()
        rows = await neo4j_client.query_read(
            f"MATCH (n:Entity:{_q(kb_label(knowledge_id))}) "
            "WHERE n.document_id IS NOT NULL "
            "RETURN collect(DISTINCT n.document_id) AS ids"
        )
        have = {int(x) for x in (rows[0]["ids"] or [])} if rows else set()
        return [d for d in document_ids if d in have]

    # ---------------- 删除 ----------------

    async def delete_all(self) -> None:
        await neo4j_client.ensure_available()
        await neo4j_client.run(f"MATCH (n:Entity:{_q(self.label)}) DETACH DELETE n")
        await neo4j_client.run(f"MATCH (c:Community:{_q(self.label)}) DETACH DELETE c")

    async def delete_by_document(self, knowledge_id: int, document_id: int) -> None:
        """单文档图谱删除：只删 single_doc 来源（Java 同款 WHERE 条件）。"""
        await neo4j_client.ensure_available()
        label = kb_label(knowledge_id)
        await neo4j_client.run(
            f"MATCH (n:Entity:{_q(label)}) "
            "WHERE n.document_id = $docId AND n.graph_source = 'single_doc' DETACH DELETE n",
            {"docId": str(document_id)},
        )

    # ---------------- 节点/边 CRUD ----------------

    async def create_node(self, name: str, entity_type: str, description: str | None) -> dict:
        await neo4j_client.ensure_available()
        rows = await neo4j_client.query_write(
            f"MERGE (n:Entity:{_q(self.label)} {{name: $name}})\n"
            "ON CREATE SET n.id = $nodeId, n.entity_type = $entityType,\n"
            "              n.description = $description, n.source = 'manual',\n"
            "              n.created_at = datetime(), n.updated_at = datetime()\n"
            "ON MATCH SET n.entity_type = $entityType, n.description = $description,\n"
            "             n.updated_at = datetime()\n"
            "RETURN n",
            {"name": name, "nodeId": str(new_id()), "entityType": entity_type,
             "description": description or ""},
        )
        node = rows[0]["n"]
        vec = await self._embed_one(name)
        if vec:
            await self._ensure_vector_index(len(vec))
            await neo4j_client.run(
                f"MATCH (n:Entity:{_q(self.label)} {{name: $name}}) SET n.embedding = $emb",
                {"name": name, "emb": vec},
            )
        return _node_vo(node)

    async def update_node(self, element_id: str, name: str, entity_type: str,
                          description: str | None) -> dict:
        await neo4j_client.ensure_available()
        old = await self._node_name(element_id)
        rows = await neo4j_client.query_write(
            f"MATCH (n:Entity:{_q(self.label)}) WHERE elementId(n) = $elementId\n"
            "SET n.name = $name, n.entity_type = $entityType, n.description = $description,\n"
            "    n.updated_at = datetime()\n"
            "RETURN n",
            {"elementId": element_id, "name": name, "entityType": entity_type or "其他",
             "description": description or ""},
        )
        if not rows:
            from fastapi import HTTPException

            raise HTTPException(status_code=404, detail="图谱节点不存在")
        if name != old:
            vec = await self._embed_one(name)
            if vec:
                await self._ensure_vector_index(len(vec))
                await neo4j_client.run(
                    f"MATCH (n:Entity:{_q(self.label)} {{name: $name}}) SET n.embedding = $emb",
                    {"name": name, "emb": vec},
                )
        return _node_vo(rows[0]["n"])

    async def delete_node(self, element_id: str) -> None:
        await neo4j_client.ensure_available()
        await neo4j_client.run(
            f"MATCH (n:Entity:{_q(self.label)}) WHERE elementId(n) = $elementId DETACH DELETE n",
            {"elementId": element_id},
        )

    async def create_edge(self, head: str, relation: str, tail: str,
                          description: str | None) -> dict:
        await neo4j_client.ensure_available()
        rows = await neo4j_client.query_write(
            f"MATCH (h:Entity:{_q(self.label)} {{name: $head}})\n"
            f"MATCH (t:Entity:{_q(self.label)} {{name: $tail}})\n"
            "MERGE (h)-[r:RELATION {relation_type: $relation}]->(t)\n"
            "ON CREATE SET r.id = $relId, r.description = $description, r.weight = 1.0,\n"
            "              r.source = 'manual', r.created_at = datetime()\n"
            "ON MATCH SET r.description = $description, r.updated_at = datetime()\n"
            "RETURN r, elementId(r) AS eid, elementId(h) AS hid, elementId(t) AS tid",
            {"head": head, "tail": tail, "relation": relation,
             "relId": str(new_id()), "description": description or ""},
        )
        if not rows:
            from fastapi import HTTPException

            raise HTTPException(status_code=404, detail="头或尾节点不存在")
        vo = _edge_vo(rows[0]["r"])
        vo["startNodeElementId"] = rows[0]["hid"]
        vo["endNodeElementId"] = rows[0]["tid"]
        return vo

    async def update_edge(self, element_id: str, relation: str | None,
                          description: str | None, weight: float | None) -> dict:
        await neo4j_client.ensure_available()
        sets = ["r.updated_at = datetime()"]
        params: dict[str, Any] = {"elementId": element_id}
        if relation is not None:
            sets.append("r.relation_type = $relation")
            params["relation"] = relation
        if description is not None:
            sets.append("r.description = $description")
            params["description"] = description
        if weight is not None:
            sets.append("r.weight = $weight")
            params["weight"] = weight
        rows = await neo4j_client.query_write(
            f"MATCH ()-[r:RELATION]->() WHERE elementId(r) = $elementId\n"
            f"SET {', '.join(sets)}\n"
            "RETURN r",
            params,
        )
        if not rows:
            from fastapi import HTTPException

            raise HTTPException(status_code=404, detail="图谱关系不存在")
        return _edge_vo(rows[0]["r"])

    async def delete_edge(self, element_id: str) -> None:
        await neo4j_client.ensure_available()
        await neo4j_client.run(
            "MATCH ()-[r:RELATION]->() WHERE elementId(r) = $elementId DELETE r",
            {"elementId": element_id},
        )

    async def list_node_names(self) -> list[str]:
        if not await neo4j_client.is_available():
            return []
        rows = await neo4j_client.query_read(
            f"MATCH (n:Entity:{_q(self.label)}) RETURN n.name AS name ORDER BY name"
        )
        return [r["name"] for r in rows if r["name"] is not None]

    async def rebuild_vector_index(self) -> int:
        """给缺 embedding 的节点补向量。返回补生成数量。

        ⚠️ 这条会真调 embedding 模型（运行时行为）；测试用假 embedding 函数替换边界。
        """
        await neo4j_client.ensure_available()
        rows = await neo4j_client.query_read(
            f"MATCH (n:Entity:{_q(self.label)}) WHERE n.embedding IS NULL "
            "RETURN n.name AS name"
        )
        names = [r["name"] for r in rows if r["name"] is not None]
        if not names:
            return 0
        embeddings = await self._embed_many(names)
        if not embeddings:
            from fastapi import HTTPException

            raise HTTPException(status_code=500, detail="embedding 生成失败")
        await self._ensure_vector_index(len(next(iter(embeddings.values()))))
        count = 0
        for name, vec in embeddings.items():
            try:
                await neo4j_client.run(
                    f"MATCH (n:Entity:{_q(self.label)} {{name: $name}}) SET n.embedding = $emb",
                    {"name": name, "emb": vec},
                )
                count += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("[Graph] 节点[%s] embedding 写入失败: %s", name, exc)
        return count

    # ---------------- embedding / 索引 ----------------

    async def _embed_many(self, names: list[str]) -> dict[str, list[float]]:
        """批量生成实体名向量。失败跳过（Java generateEmbeddings 同款）。"""
        if not names:
            return {}
        from app.services.embedding_client import embed as embed_texts

        try:
            vectors = await embed_texts(names)
            return {name: vec for name, vec in zip(names, vectors) if vec}
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Graph] 批量 embedding 失败: %s", exc)
            return {}

    async def _embed_one(self, text: str) -> list[float] | None:
        from app.services.embedding_client import embed_query

        try:
            return await embed_query(text)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Graph] embedding 失败: %s", exc)
            return None

    async def _ensure_vector_index(self, dims: int) -> None:
        try:
            await neo4j_client.run(
                f"CREATE VECTOR INDEX {VECTOR_INDEX_NAME} IF NOT EXISTS\n"
                "FOR (n:standalone) ON (n.embedding)\n"
                "OPTIONS {indexConfig: {`vector.dimensions`: " + str(dims) +
                ", `vector.similarity_function`: 'cosine'}}"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Graph] 向量索引创建失败: %s", exc)

    async def _node_name(self, element_id: str) -> str | None:
        rows = await neo4j_client.query_read(
            f"MATCH (n:Entity:{_q(self.label)}) WHERE elementId(n) = $elementId "
            "RETURN n.name AS name",
            {"elementId": element_id},
        )
        if rows and rows[0]["name"] is not None:
            return rows[0]["name"]
        return None


# ---------------- VO 转换（Node/Relationship 对象 → dict，字段名 camelCase） ----------------

def _is_null(obj: Any) -> bool:
    """neo4j 驱动的 Null 节点/值判定：`RETURN null AS r` 得到 Neo4jNull。"""
    return obj is None or type(obj).__name__ in ("Null", "Neo4jNull") or obj is None


def _node_vo(node: Any) -> dict:
    props = dict(node)

    def g(key: str) -> Any:
        v = props.get(key)
        return None if v is None else v

    labels = sorted(node.labels) if hasattr(node, "labels") else []
    return {
        "elementId": node.element_id,
        "id": g("id"),
        "name": g("name"),
        "entityType": g("entity_type"),
        "description": g("description"),
        "source": g("source"),
        "documentId": g("document_id"),
        "labels": list(labels),
        "properties": {k: _plain(v) for k, v in props.items() if k != "embedding"},
    }


def _edge_vo(rel: Any) -> dict:
    props = dict(rel)
    v = props.get("relation_type")

    def g(key: str) -> Any:
        val = props.get(key)
        return None if val is None else val

    return {
        "elementId": rel.element_id,
        "id": g("id"),
        "relationType": v if v is not None else rel.type,
        "description": g("description"),
        "weight": float(props["weight"]) if props.get("weight") is not None else 1.0,
        "source": g("source"),
        # ⚠️ Java 驱动的 rel.startNodeElementId() 在 Python 驱动上不存在，
        # 只能从 start_node 代理节点取 element_id（Bolt 层自带，无需同查节点）
        "startNodeElementId": _node_element_id(rel, "start"),
        "endNodeElementId": _node_element_id(rel, "end"),
        "properties": {k: _plain(x) for k, x in props.items()},
    }


def _node_element_id(rel: Any, side: str) -> str | None:
    try:
        node = rel.start_node if side == "start" else rel.end_node
        return node.element_id if node is not None else None
    except Exception:  # noqa: BLE001
        return None


def _plain(v: Any) -> Any:
    """Neo4j temporal/数值类型 → JSON 可序列化标量。"""
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    return str(v)
