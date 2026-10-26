"""静态校验 ToolCall 实体的列名/类型与 sql/init.sql 的 DDL 是否一致。

为什么要这个：连不上库时（网络/pg_hba 挡了），SQLAlchemy 只有在真正 INSERT
那一刻才会告诉你"column xxx does not exist"。把 DDL 和 ORM 映射放在一起比，
不需要数据库也能提前发现问题。

用法：python -m scripts.tool_schema_check
"""

from __future__ import annotations

import re
from pathlib import Path

from app.models.entities import ToolCall

INIT_SQL = Path(__file__).resolve().parent.parent.parent / "lengbot" / "sql" / "init.sql"


def parse_ddl(sql_text: str, table: str) -> dict[str, str]:
    """从 CREATE TABLE 块里抠出 列名 → 类型。"""
    m = re.search(
        rf"CREATE TABLE {table}\s*\((.*?)\n\);",
        sql_text,
        re.S | re.I,
    )
    if not m:
        raise SystemExit(f"没在 init.sql 里找到 CREATE TABLE {table}")
    cols: dict[str, str] = {}
    for line in m.group(1).splitlines():
        line = line.strip().rstrip(",")
        if not line or line.startswith("--"):
            continue
        if line.upper().startswith(("PRIMARY KEY", "UNIQUE", "CONSTRAINT")):
            continue
        parts = line.split()
        if len(parts) >= 2:
            cols[parts[0].lower()] = parts[1].upper()
    return cols


def main() -> None:
    if not INIT_SQL.exists():
        print(f"跳过：找不到 {INIT_SQL}")
        return

    ddl_cols = parse_ddl(INIT_SQL.read_text(encoding="utf-8", errors="replace"), "tool_calls")
    orm_cols = {c.name: str(c.type).upper() for c in ToolCall.__table__.columns}

    print(f"DDL 列数={len(ddl_cols)}  ORM 列数={len(orm_cols)}")
    print(f"\nDDL : {sorted(ddl_cols)}")
    print(f"ORM : {sorted(orm_cols)}")

    missing = set(ddl_cols) - set(orm_cols)
    extra = set(orm_cols) - set(ddl_cols)
    bad = 0
    if missing:
        bad += 1
        print(f"\n❌ ORM 缺列（INSERT 会漏写，或 SELECT 拿不到）: {sorted(missing)}")
    if extra:
        bad += 1
        print(f"\n❌ ORM 多列（库表里没有，任何读写都会报 column does not exist）: {sorted(extra)}")
    if not missing and not extra:
        print("\n✅ 列集合一致")

    # bigint 主键必须映射成 BigInteger，否则雪花 ID 会被截断
    id_type = str(ToolCall.__table__.c.id.type).upper()
    print(f"\nid 类型: {id_type}（DDL: {ddl_cols.get('id')}）")
    if "BIGINT" not in id_type:
        bad += 1
        print("❌ 主键不是 BIGINT —— 雪花 ID 会被截断成 int32")

    if bad:
        raise SystemExit(1)
    print("\n✅ 静态校验通过")


if __name__ == "__main__":
    main()
