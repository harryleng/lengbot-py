"""Neo4j 异步客户端（对照 Java `Neo4jUtil`）。

**连通性判定语义与 Java 一致**：`is_available()` 探活失败返回 False 而不是抛错
—— 查询类端点（subgraph/stats）在 Neo4j 挂掉时降级为空结果，
写入类端点（import/create/delete）先 `ensure_available()` 抛
`Neo4jUnavailable`（Java GRAPH_NEO4J_UNAVAILABLE）。

驱动是全局单例：neo4j 驱动自带连接池，每次新建池既慢又容易撞
最大连接数。测试用 `reset()` 清池换配置。
"""

from __future__ import annotations

import logging

from app.core.config import settings

logger = logging.getLogger(__name__)

STANDALONE_LABEL = "standalone"  # Java Neo4jUtil.STANDALONE_LABEL

_driver = None
_available: bool | None = None


class Neo4jUnavailable(RuntimeError):
    """Neo4j 不可用（连接失败），映射 Java GRAPH_NEO4J_UNAVAILABLE。"""


def _get_driver():
    global _driver
    if _driver is None:
        from neo4j import AsyncGraphDatabase

        _driver = AsyncGraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_username, settings.neo4j_password),
            connection_timeout=5,
            max_connection_pool_size=20,
        )
    return _driver


def reset() -> None:
    """清驱动单例（测试换配置用）。"""
    global _driver, _available
    if _driver is not None:
        import asyncio

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        # close 由调用方在异步上下文里做；这里只置空引用
    _driver = None
    _available = None


async def is_available() -> bool:
    """探活：跑一条 1 RETURN 1。结果缓存 60s，避免每个请求都握手。"""
    global _available
    import time

    if _available is not None and time.monotonic() - getattr(is_available, "_at", 0) < 60:
        return _available
    try:
        await query_read("RETURN 1 AS ok")
        _available = True
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Neo4j] 不可用: %s", exc)
        _available = False
    import time as _t

    is_available._at = _t.monotonic()  # type: ignore[attr-defined]
    return _available


async def ensure_available() -> None:
    if not await is_available():
        raise Neo4jUnavailable("Neo4j 图数据库不可用")


async def query_read(cypher: str, params: dict | None = None) -> list[dict]:
    """执行只读查询，返回 record 列表（dict 形式，key 同 Cypher 别名）。"""
    driver = _get_driver()
    async with driver.session() as session:
        result = await session.run(cypher, params or {})
        records = []
        async for record in result:
            records.append({key: record[key] for key in record.keys()})
        await result.consume()
        return records


async def query_write(cypher: str, params: dict | None = None) -> list[dict]:
    """执行写查询（MERGE/SET/DELETE 必须走 write 事务，read 路由会拒）。"""
    driver = _get_driver()
    async with driver.session() as session:
        async def _tx(tx):
            result = await tx.run(cypher, params or {})
            records = []
            async for record in result:
                records.append({key: record[key] for key in record.keys()})
            await result.consume()
            return records

        return await session.execute_write(_tx)


async def run(cypher: str, params: dict | None = None) -> None:
    """执行无返回的写语句（Java neo4jUtil.run 同名）。"""
    await query_write(cypher, params)
