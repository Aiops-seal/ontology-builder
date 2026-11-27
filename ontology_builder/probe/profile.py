"""列画像：行数、空值、distinct、值集。

**这一层只数数，不判断。** 判断在 infer.py。分开的理由是这一层能被独立验：
给一张三行两列的表，行数、distinct 是能一眼数对的；判据对不对，得靠 TPC-DS 那种
带外部答案的 schema 才看得见。

**只读打开。** `mode=ro` 不是为了安全，是为了让「probe 改不了数据」成为物理事实，
而不是一句注释。`probe` 在本体那边也只有 candidate 表的写权限（SPEC §3），
两处是同一条纪律。

值集有上限：超过 `max_distinct` 的列记 `values=None`，**不参与包含率**。
不是省内存——是那类列本来就不该当外键（自由文本、金额），硬比会算出一堆巧合。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from .schema import Schema


@dataclass
class ColumnProfile:
    name: str
    null_count: int
    distinct: int
    values: frozenset | None  # None = 超过上限，不参与包含率

    def __len__(self) -> int:
        return 0 if self.values is None else len(self.values)


@dataclass
class TableProfile:
    name: str
    rows: int
    columns: dict[str, ColumnProfile] = field(default_factory=dict)

    def column(self, name: str) -> ColumnProfile | None:
        return self.columns.get(name)


@dataclass
class Profile:
    tables: dict[str, TableProfile] = field(default_factory=dict)

    def table(self, name: str) -> TableProfile | None:
        return self.tables.get(name)


def _norm(value):
    """跨列比较前先归一：数字统一成 float，其他原样。

    sqlite 是动态类型，`decimal(7,2)` 列里可能同时存着 12 和 12.0。
    不归一的话，包含率会把同一批数字判成不相交——这是「读起来对、跑起来错」的那类 bug。
    """
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return float(value)
    return value


def profile_sqlite(
    db: str | Path,
    schema: Schema,
    *,
    max_distinct: int = 20000,
    strict: bool = True,
) -> Profile:
    path = Path(db)
    if not path.exists():
        raise FileNotFoundError(f"{path} 不存在")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return _profile(conn, schema, max_distinct=max_distinct, strict=strict)
    finally:
        conn.close()


def _profile(conn: sqlite3.Connection, schema: Schema, *, max_distinct: int, strict: bool) -> Profile:
    present = {
        r[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        if not r[0].startswith("sqlite_")
    }
    missing = sorted(set(schema.tables) - present)
    extra = sorted(present - set(schema.tables))
    if strict and (missing or extra):
        raise ValueError(
            "schema 与库对不上："
            f"库里缺 {missing or '无'}，schema 里没有 {extra or '无'}。"
            "未知输入等于错误——多半是拿错了 DDL，或者库是上一版建的"
        )

    prof = Profile()
    for name in schema.names():
        if name not in present:
            continue
        rows = int(conn.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0])
        tp = TableProfile(name=name, rows=rows)
        for col in schema[name].columns:
            nulls, distinct = conn.execute(
                f'SELECT count(*) - count("{col.name}"), count(DISTINCT "{col.name}") '
                f'FROM "{name}"'
            ).fetchone()
            values = None
            if 0 < distinct <= max_distinct:
                values = frozenset(
                    _norm(r[0])
                    for r in conn.execute(
                        f'SELECT DISTINCT "{col.name}" FROM "{name}" '
                        f'WHERE "{col.name}" IS NOT NULL'
                    )
                )
            tp.columns[col.name] = ColumnProfile(
                name=col.name, null_count=int(nulls), distinct=int(distinct), values=values
            )
        prof.tables[name] = tp
    return prof
