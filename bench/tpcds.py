"""验 probe 用的脚手架，不是库的一部分。

`bench/` 和 `ontology_builder/` 分开放，是因为这两块东西的性质不同：
库里放的是能被别人拿走用的东西，这里放的是**只有这个实验需要的**造数器和打分器。
把 TPC-DS 塞进包里，等于让每个用 probe 的人都拖着一份用不上的 benchmark。

三件事：

1. `build(db)`     照 DDL 与官方约束造一份迷你实例（造数据的人知道答案）
2. `answer_key()`  读 `tpcds_ri.sql` 拿到官方外键——**这份不喂给 probe**
3. `score(...)`    算 recall / precision

见 SPEC §6.1：这两个数不能说成「准确率」。数据是照答案造的，
所以它只能证明判据往哪个方向偏，不能证明在真库上能到这个数。
"""

from __future__ import annotations

import random
import sqlite3
from pathlib import Path

from ontology_builder.probe.schema import ForeignKey, Schema, parse_ddl, parse_foreign_keys

HERE = Path(__file__).resolve().parent
DDL_PATH = HERE.parent / "fixtures" / "schema" / "tpcds.sql"
RI_PATH = HERE.parent / "fixtures" / "schema" / "tpcds_ri.sql"

# 迷你实例的行数。比例照着 TPC-DS sf=1 的量级缩，不是随便填的：
# 维度表要小到能看，事实表要大到底数够——外键列覆盖不全的话，
# 包含率会掉下来，那是数据量的问题，不是判据的问题。
ROWS = {
    "dbgen_version": 1,
    "date_dim": 730,
    "time_dim": 100,
    "item": 3000,
    "store": 12,
    "customer": 3000,
    "customer_address": 1500,
    "customer_demographics": 200,
    "household_demographics": 200,
    "promotion": 300,
    "warehouse": 5,
    "ship_mode": 20,
    "reason": 35,
    "income_band": 20,
    "call_center": 6,
    "web_site": 12,
    "web_page": 60,
    "catalog_page": 100,
    "store_sales": 20000,
    "store_returns": 1500,
    "catalog_sales": 10000,
    "catalog_returns": 800,
    "web_sales": 8000,
    "web_returns": 600,
    "inventory": 5000,
}


def _read(path: Path) -> str:
    """读 DDL，缺文件时说清缺哪个、怎么补 —— 别让人对着 FileNotFoundError 猜。"""
    if not path.exists():
        raise FileNotFoundError(
            f"缺 {path}\n"
            f"TPC-DS 的 DDL 不在仓库里（TPC 保留版权，本仓库是 MIT）。\n"
            f"跑 `python -m bench.fetch_tpcds` 看怎么拿，或直接：\n"
            f"    python -m bench.fetch_tpcds --from /path/to/tpcds-kit/tools"
        )
    return path.read_text(encoding="utf-8")


def load_schema() -> Schema:
    return parse_ddl(_read(DDL_PATH))


def load_fks(schema: Schema) -> list[ForeignKey]:
    return [fk for fk in parse_foreign_keys(_read(RI_PATH)) if fk.table in schema]


def answer_key(schema: Schema | None = None) -> set[tuple[str, str, str, str]]:
    schema = schema or load_schema()
    return {
        (fk.table, fk.columns[0], fk.ref_table, fk.ref_columns[0])
        for fk in load_fks(schema)
        if fk.is_single
    }


def _topo(schema: Schema, fks: list[ForeignKey]) -> list[str]:
    """被引用的表先造——子表要拿父表的键值当样本。"""
    deps: dict[str, set[str]] = {n: set() for n in schema.tables}
    for fk in fks:
        if fk.ref_table != fk.table and fk.ref_table in deps and fk.table in deps:
            deps[fk.table].add(fk.ref_table)
    order: list[str] = []
    while len(order) < len(deps):
        ready = sorted(n for n, d in deps.items() if n not in order and d <= set(order))
        if not ready:
            raise RuntimeError(f"外键成环：{sorted(set(deps) - set(order))}")
        order.extend(ready)
    return order


def _unique_rows(table, columns: list[list], pk_idx: list[int], n: int) -> list[list]:
    """丢掉撞主键的整行（保留先出现的那条）。撞得少，所以不用重生成。"""
    seen: set[tuple] = set()
    keep: list[int] = []
    for i in range(n):
        k = tuple(columns[j][i] for j in pk_idx)
        if k in seen:
            continue
        seen.add(k)
        keep.append(i)
    return [[col[i] for i in keep] for col in columns]


def _value(col, rng: random.Random):
    """非键列的值。**按名字猜，不按语义算**——这里只需要「像那么回事」。"""
    n = col.name.lower()
    if col.type_class == "num":
        if n.endswith("_sk"):
            return rng.randint(1, 500)
        if any(k in n for k in ("price", "cost", "amt", "tax", "profit", "total", "paid")):
            return round(rng.uniform(1, 1000), 2)
        if "quantity" in n or n.endswith("_qty"):
            return rng.randint(1, 100)
        if n.endswith("_year"):
            return rng.randint(1998, 2002)
        if n.endswith("_moy"):
            return rng.randint(1, 12)
        if n.endswith("_dom"):
            return rng.randint(1, 28)
        if n.endswith("_qoy"):
            return rng.randint(1, 4)
        if n.endswith("_dow"):
            return rng.randint(0, 6)
        if n.endswith("_week"):
            return rng.randint(1, 52)
        if n.endswith("_id"):
            return rng.randint(1000, 9999)
        return rng.randint(0, 1000)
    if col.type_class == "date":
        if col.type.lower().startswith("time"):
            return f"{rng.randint(0, 23):02d}:{rng.randint(0, 59):02d}:{rng.randint(0, 59):02d}"
        return f"{rng.randint(1998, 2002)}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}"
    return f"{rng.choice(['AAAA', 'BBBB', 'CCCC', 'DDDD'])}{rng.randint(10000, 99999)}"


def build(db: str | Path, *, seed: int = 7, rows: dict[str, int] | None = None) -> Path:
    """造库。已有同名文件会被删掉重建——**只许对临时路径调用**。"""
    path = Path(db)
    if path.exists():
        path.unlink()
    schema = load_schema()
    fks = load_fks(schema)
    counts = {**ROWS, **(rows or {})}
    rng = random.Random(seed)

    pools: dict[tuple[str, str], list] = {}
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(DDL_PATH.read_text(encoding="utf-8"))
        for name in _topo(schema, fks):
            table = schema[name]
            n = counts.get(name, 20)
            pk = set(table.pk)
            fk_of = {
                fk.columns[0]: (fk.ref_table, fk.ref_columns[0])
                for fk in fks
                if fk.table == name and fk.is_single
            }
            columns: list[list] = []
            for c in table.columns:
                if c.name in pk and c.name not in fk_of:
                    columns.append([i + 1 for i in range(n)])
                elif c.name in fk_of:
                    ref = pools.get(fk_of[c.name])
                    if not ref:
                        raise RuntimeError(f"{name}.{c.name} 的父表键值还没造出来：{fk_of[c.name]}")
                    p_null = 0.0 if c.not_null else 0.02
                    columns.append([None if rng.random() < p_null else rng.choice(ref) for _ in range(n)])
                else:
                    p_null = 0.0 if c.not_null else 0.05
                    columns.append([None if rng.random() < p_null else _value(c, rng) for _ in range(n)])
            # 组合主键全是外键时（inventory 的三列），逐列采样会撞主键。
            # 这里改成整行生成 + 主键去重：撞了就重来，最多重来 20 轮。
            pk_idx = [i for i, c in enumerate(table.columns) if c.name in pk]
            if len(pk_idx) > 1 and all(c.name in fk_of for c in table.columns if c.name in pk):
                columns = _unique_rows(table, columns, pk_idx, n)
            conn.executemany(
                f'INSERT INTO "{name}" ({", ".join(chr(34) + c.name + chr(34) for c in table.columns)}) '
                f'VALUES ({", ".join("?" * len(table.columns))})',
                list(zip(*columns)) if columns else [],
            )
            for c, vals in zip(table.columns, columns):
                pools[(name, c.name)] = sorted({v for v in vals if v is not None})
        conn.commit()
    finally:
        conn.close()
    return path


def score(cands, key: set[tuple[str, str, str, str]]) -> dict:
    got = {
        (c.payload["table"], c.payload["column"], c.payload["ref_table"], c.payload["ref_column"])
        for c in cands
        if c.kind == "foreign_key"
    }
    hit = got & key
    return {
        "key": len(key),
        "candidates": len(got),
        "hits": len(hit),
        "recall": len(hit) / len(key) if key else 0.0,
        "precision": len(hit) / len(got) if got else 0.0,
        "missing": sorted(key - got),
        "false_positives": sorted(got - key),
    }
