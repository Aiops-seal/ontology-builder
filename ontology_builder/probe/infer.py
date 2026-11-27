"""判据：把画像变成候选。

三条判据，只有前两条是必要条件：

    C1 包含率   |src ∩ dst| / |src| = 1
    C2 目标唯一 distinct(dst 非空) == count(dst 非空)
    C3 名字同源 去掉各表前缀后，后缀相同

**C3 是筛子，不是判据。** 单靠 C1∧C2 会把任意两张表的代理键配成外键——TPC-DS 的
实测在 README 里。

**这里不判 route，只声明两件事**：`declared`（证据是不是声明式的）和 `impact`
（落点会不会越出这一处）。第三问「位置原本是不是空的」要查库，`infer` 看不见，
由 `store.add_candidates` 算。见 SPEC §4.3。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from ..route import impact_of
from .profile import Profile
from .schema import Schema, Table


@dataclass(frozen=True)
class Candidate:
    kind: str  # object / grain / foreign_key
    payload: dict[str, Any]
    evidence: dict[str, Any]
    declared: bool  # 证据来自 DDL / 已有本体，还是从数据里算出来的

    @property
    def impact(self) -> str:
        return impact_of(self.kind, self.payload)

    def key(self) -> tuple:
        return (self.kind, json.dumps(self.payload, sort_keys=True, ensure_ascii=False))

    def as_row(self) -> dict[str, Any]:
        """report 里的样子。没有 `route`——route 要查库，这里给不出来。"""
        return {
            "kind": self.kind,
            "payload": self.payload,
            "evidence": self.evidence,
            "declared": self.declared,
            "impact": self.impact,
        }


def _unique(profile_col, rows: int) -> bool:
    """C2：这一列在非空行上是不是唯一。

    空行不算——外键列允许为空（TPC-DS 的 `ss_customer_sk` 可以缺）。
    但整列全空时不算唯一：`0 == 0` 会让它通过，那是数学，不是数据。
    """
    if profile_col is None:
        return False
    nonnull = rows - profile_col.null_count
    return nonnull >= 2 and profile_col.distinct == nonnull


def _stem(table: Table, column: str, aliases: dict[str, str] | None = None) -> str:
    prefix = table.prefix
    stem = column[len(prefix):] if prefix and column.startswith(prefix) else column
    if not aliases:
        return stem
    return "_".join(aliases.get(tok, tok) for tok in stem.split("_"))


def _name_match(
    src_table: Table,
    src_col: str,
    dst_table: Table,
    dst_col: str,
    aliases: dict[str, str] | None = None,
) -> bool:
    """去掉各自表前缀之后，一个是不是另一个的后缀。

    为什么要允许「后缀」而不要求全等：`call_center.cc_closed_date_sk` 指向
    `date_dim.d_date_sk`，去掉前缀是 `closed_date_sk` 和 `date_sk`——**不相等**。
    TPC-DS 的命名是「限定词 + 实体 + _sk」，限定词因人而异，实体才是同一个。
    所以要求全等会把 102 条真外键挡掉一大半（实测见 README）。

    **`aliases` 是缩写表，必须由调用方传，库里不内置。** 它是某个组织自己的
    命名习惯（`addr` = `address`、`cdemo` = `demo`），换个客户就是另一张表。
    写进代码等于把 TPC-DS 的习惯当成通用规律——一个库里硬编码的别名表，
    在别人的库上只会静默地一条都匹配不上。

    这条放宽也带来代价：`customer_demographics.cd_demo_sk` 和
    `household_demographics.hd_demo_sk` 去掉前缀都是 `demo_sk`，两条 1..200 的
    键互相包含——名字救不了它，只能靠人签。这是 sign 存在的理由。
    """
    a = _stem(src_table, src_col, aliases)
    b = _stem(dst_table, dst_col, aliases)
    if not a or not b:
        return False
    if a == b:
        return True
    return a.endswith("_" + b) or b.endswith("_" + a)


def infer(
    schema: Schema,
    profile: Profile,
    *,
    min_containment: float = 1.0,
    require_name_match: bool = True,
    require_pk_target: bool = False,
    name_aliases: dict[str, str] | None = None,
) -> list[Candidate]:
    """出候选。同一个输入跑两遍，输出逐字节一致（排序在 `_sorted`）。

    `require_name_match` / `require_pk_target` 关掉是给实验用的，量每条判据
    各自挡掉多少（README 里那张表）。默认两个都按「名字开、PK 关」——
    PK 关掉是有意的：**「外键必须指向主键」这件事 TPC-DS 成立，真库不成立**，
    很多老库的外键指向唯一索引而不是主键。默认打开它等于把判据写死在一个特例上。
    """
    out: list[Candidate] = []
    out.extend(_objects(schema, profile))
    out.extend(_grains(schema, profile))

    declared = _declared_fks(schema, profile)
    out.extend(declared)
    out.extend(
        _inferred_fks(
            schema,
            profile,
            min_containment=min_containment,
            require_name_match=require_name_match,
            require_pk_target=require_pk_target,
            name_aliases=name_aliases,
            skip={(c.payload["table"], c.payload["column"], c.payload["ref_table"], c.payload["ref_column"]) for c in declared},
        )
    )
    return _sorted(out)


# ------------------------------------------------------------------ 表 / 列


def _objects(schema: Schema, profile: Profile) -> list[Candidate]:
    out = []
    for name in schema.names():
        tp = profile.table(name)
        if tp is None:
            continue
        table = schema[name]
        out.append(
            Candidate(
                kind="object",
                payload={
                    "type": "table_object",
                    "name": name,
                    "fields": {
                        "row_count": tp.rows,
                        "columns": [
                            {"name": c.name, "type": c.type, "not_null": c.not_null}
                            for c in table.columns
                        ],
                    },
                },
                evidence={"rows": tp.rows, "columns": len(table.columns), "source": "ddl+data"},
                declared=True,
            )
        )
    return out


def _grains(schema: Schema, profile: Profile) -> list[Candidate]:
    """grain：这行数据的粒度是哪几列。

    DDL 写了 PK → 声明式（只在证据里记一句「数据里也确实唯一」）。
    DDL 没写 → 只认单列唯一，证据是推断的（`declared=False`）。**组合键不搜**：两两
    组合已经是 C(n,2)，三列以上爆炸，而且组合唯一的列对里绝大多数是巧合（金额+日期
    的组合几乎总唯一）
    """
    out = []
    for name in schema.names():
        tp = profile.table(name)
        if tp is None:
            continue
        table = schema[name]
        if table.pk:
            cols = [tp.column(c) for c in table.pk]
            ok = all(_unique(c, tp.rows) for c in cols)
            out.append(
                Candidate(
                    kind="grain",
                    payload={"table": name, "columns": table.pk},
                    evidence={
                        "declared": True,
                        "rows": tp.rows,
                        "pk_unique_in_data": ok,
                    },
                    declared=True,
                )
            )
            continue
        if tp.rows < 2:
            continue
        for col in table.columns:
            cp = tp.column(col.name)
            if cp is None or cp.null_count:
                continue
            if _unique(cp, tp.rows) and cp.distinct > 1:
                out.append(
                    Candidate(
                        kind="grain",
                        payload={"table": name, "columns": [col.name]},
                        evidence={
                            "declared": False,
                            "rows": tp.rows,
                            "distinct": cp.distinct,
                        },
                        declared=False,
                    )
                )
    return out


# ------------------------------------------------------------------ 外键


def _fk_payload(table: str, column: str, ref_table: str, ref_column: str) -> dict[str, Any]:
    return {
        "table": table,
        "column": column,
        "ref_table": ref_table,
        "ref_column": ref_column,
        "predicate": "joins_on",
        "field": "joins",
    }


def _declared_fks(schema: Schema, profile: Profile) -> list[Candidate]:
    out = []
    for name in schema.names():
        table = schema[name]
        tp = profile.table(name)
        for fk in table.fks:
            if not fk.is_single:
                continue  # 组合外键 v0.1 不做，见 SPEC §4.6
            src = tp.column(fk.columns[0]) if tp else None
            dst_tp = profile.table(fk.ref_table)
            dst = dst_tp.column(fk.ref_columns[0]) if dst_tp else None
            containment = None
            if src is not None and dst is not None and src.values and dst.values:
                containment = len(src.values & dst.values) / len(src.values)
            out.append(
                Candidate(
                    kind="foreign_key",
                    payload=_fk_payload(name, fk.columns[0], fk.ref_table, fk.ref_columns[0]),
                    evidence={
                        "declared": True,
                        "containment": containment,
                        "dst_unique": _unique(dst, dst_tp.rows) if dst_tp and dst else None,
                    },
                    declared=True,
                )
            )
    return out


def _inferred_fks(
    schema: Schema,
    profile: Profile,
    *,
    min_containment: float,
    require_name_match: bool,
    require_pk_target: bool,
    name_aliases: dict[str, str] | None,
    skip: set[tuple[str, str, str, str]],
) -> list[Candidate]:
    # C2 是必要条件 → 目标只可能是「唯一列」。这一步把双向配对从 300×300 压到 300×25，
    # 而且不是优化技巧：不唯一的列本来就不可能当外键目标，扫它没有意义。
    targets: list[tuple[str, str]] = []
    for name in schema.names():
        tp = profile.table(name)
        if tp is None:
            continue
        for col in schema[name].columns:
            if require_pk_target and col.name not in schema[name].pk:
                continue
            cp = tp.column(col.name)
            if _unique(cp, tp.rows):
                targets.append((name, col.name))

    out = []
    for name in schema.names():
        tp = profile.table(name)
        if tp is None:
            continue
        table = schema[name]
        for col in table.columns:
            src = tp.column(col.name)
            if src is None or not src.values:
                continue  # 全空，或 distinct 超过上限——都不参与包含率
            for ref_table, ref_col in targets:
                if ref_table == name:
                    continue
                dst_tp = profile.table(ref_table)
                dst = dst_tp.column(ref_col)
                if dst is None or not dst.values:
                    continue
                if schema[ref_table].column(ref_col).type_class != col.type_class:
                    continue
                name_match = _name_match(table, col.name, schema[ref_table], ref_col, name_aliases)
                if require_name_match and not name_match:
                    continue
                containment = len(src.values & dst.values) / len(src.values)
                if containment < min_containment:
                    continue
                if (name, col.name, ref_table, ref_col) in skip:
                    continue
                out.append(
                    Candidate(
                        kind="foreign_key",
                        payload=_fk_payload(name, col.name, ref_table, ref_col),
                        evidence={
                            "declared": False,
                            "containment": round(containment, 6),
                            "src_distinct": src.distinct,
                            "src_null_rate": round(src.null_count / tp.rows, 4) if tp.rows else None,
                            "dst_distinct": dst.distinct,
                            "dst_rows": dst_tp.rows,
                            "dst_unique": True,  # 进到这里就已经过了 C2
                            "name_match": name_match,
                        },
                        declared=False,
                    )
                )
    return out


def _sorted(cands: list[Candidate]) -> list[Candidate]:
    order = {"object": 0, "grain": 1, "foreign_key": 2}
    return sorted(cands, key=lambda c: (order.get(c.kind, 9), c.key()))
