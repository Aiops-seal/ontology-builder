"""probe 的断言。

钉三件事：

1. **判据是必要条件，不是答案。** 一条数据上成立的包含关系，名字不同就不该出（或出了也只能 sign）。
2. **声明与推断分开。** DDL 里写了 foreign key 的走 exempt，推断出来的走 sign。
3. **答案在仓库外面。** TPC-DS 的 102 条官方外键，probe 一条都不许看。

最后一条最容易破：往 `probe` 里传 `--foreign-keys tpcds_ri.sql`，recall 立刻变 100%，
然后你会以为判据很准。那个数是假的。
"""

from __future__ import annotations

import sqlite3

import pytest

import bench.tpcds as T
from ontology_builder.probe import (
    infer,
    parse_ddl,
    parse_foreign_keys,
    profile_sqlite,
)
from ontology_builder.probe.schema import attach_foreign_keys

from ontology_builder.route import decide_route


def _route(c, *, occupied: bool = False) -> str:
    """空库上的 route。probe 自己不判——位置要查库，见 SPEC §4.3。"""
    return decide_route(occupied=occupied, kind=c.kind, evidence_form=c.evidence_form)


DDL = """
create table dim_store (
    store_id   integer      not null,
    city       varchar(20)              ,
    primary key (store_id)
);
create table fact_order (
    order_id    integer       not null,
    store_id    integer                 ,
    order_amt   decimal(10,2)           ,
    primary key (order_id)
);
create table fact_no_pk (
    event_id   integer,
    note       varchar(20)
);
create table dim_guard (
    guard_key  integer not null,
    guard_name varchar(8),
    primary key (guard_key)
);
"""


def _db(path, schema_sql: str, inserts: list[str] | None = None):
    conn = sqlite3.connect(str(path))
    conn.executescript(schema_sql)
    for stmt in inserts or []:
        conn.execute(stmt)
    conn.commit()
    conn.close()
    return path


@pytest.fixture()
def schema():
    return parse_ddl(DDL)


# ------------------------------------------------------------------ 解析


def test_parse_ddl_gets_columns_pk_and_types(schema):
    assert set(schema.tables) == {"dim_store", "fact_order", "fact_no_pk", "dim_guard"}
    store = schema["dim_store"]
    assert [c.name for c in store.columns] == ["store_id", "city"]
    assert store.pk == ["store_id"]
    assert store.column("store_id").not_null is True
    assert store.column("city").type_class == "text"
    assert schema["fact_order"].column("order_amt").type_class == "num"


def test_prefix_stops_at_first_underscore():
    """`reason` 那种表：三列都是 r_reason_*，按最后一个下划线截会只剩 `sk`，
    于是它跟全库每一列都配得上。这条断言钉的就是这个坑。"""
    s = parse_ddl(
        "create table reason (r_reason_sk integer, r_reason_id char(16), r_reason_desc char(10),"
        " primary key (r_reason_sk));"
    )
    assert s["reason"].prefix == "r_"
    s2 = parse_ddl("create table store_sales (ss_item_sk integer, ss_ticket_number integer);")
    assert s2["store_sales"].prefix == "ss_"


def test_parse_foreign_keys_and_attach(schema):
    fks = parse_foreign_keys(
        "alter table fact_order add constraint fk1 foreign key (store_id) references dim_store (store_id);"
    )
    assert len(fks) == 1 and fks[0].is_single
    attach_foreign_keys(schema, fks)
    assert schema["fact_order"].fks[0].ref_table == "dim_store"

    with pytest.raises(ValueError):
        attach_foreign_keys(
            schema, parse_foreign_keys("alter table 不存在的表 add constraint x foreign key (a) references b (c);")
        )


# ------------------------------------------------------------------ 画像


def test_profile_counts_nulls_and_normalizes_numbers(tmp_path, schema):
    db = _db(
        tmp_path / "d.db",
        DDL,
        [
            "insert into dim_store values (1,'上海'),(2,'北京'),(3,NULL)",
            "insert into fact_order values (1,1,10),(2,1,10.0),(3,2.5,20)",
            "insert into fact_no_pk values (1,'a')",
            "insert into dim_guard values (1,'x')",
        ],
    )
    prof = profile_sqlite(db, schema)
    store = prof.table("dim_store")
    assert store.rows == 3
    assert store.column("city").null_count == 1
    assert store.column("city").distinct == 2
    # int 1 与 float 1.0 要归一，不然包含率会把同一批数字判成不相交
    assert prof.table("fact_order").column("store_id").values == {1.0, 2.5}


def test_profile_is_strict_about_mismatch(tmp_path, schema):
    """schema 与库对不上就报错——拿错 DDL 是最常见的一种。"""
    db = _db(tmp_path / "d.db", "create table dim_store (store_id integer, city varchar(20));")
    with pytest.raises(ValueError) as exc:
        profile_sqlite(db, schema)
    assert "对不上" in str(exc.value)
    prof = profile_sqlite(db, schema, strict=False)
    assert set(prof.tables) == {"dim_store"}


# ------------------------------------------------------------------ 判据


def _profile(tmp_path, ddl: str, inserts):
    """按这张 DDL 建库再画像——**不能拿模块级的 DDL**，那会让自定义表全部建不出来。"""
    schema = parse_ddl(ddl)
    db = _db(tmp_path / "x.db", ddl, inserts)
    return schema, profile_sqlite(db, schema)


def test_data_alone_finds_it_but_names_reject_it(tmp_path):
    """数据上成立、名字不成立：关掉名字判据才出得来，而且只能 sign。"""
    schema, prof = _profile(
        tmp_path,
        "create table dim_guard (guard_key integer not null, primary key (guard_key));"
        "create table fact_misc (misc_ref integer, other integer);",
        [
            "insert into dim_guard values (1),(2),(3)",
            "insert into fact_misc values (1,9),(2,8),(3,7)",
        ],
    )
    strict = [c for c in infer(schema, prof) if c.kind == "foreign_key"]
    assert strict == []

    loose = [
        c
        for c in infer(schema, prof, require_name_match=False)
        if c.kind == "foreign_key" and c.payload["table"] == "fact_misc"
    ]
    assert len(loose) == 1
    assert loose[0].payload["column"] == "misc_ref"
    assert _route(loose[0]) == "sign"
    assert loose[0].evidence_form == "data_containment"


def test_containment_is_symmetric_so_both_directions_appear(tmp_path):
    """包含率 + 唯一是对称的：两边值域一样大时，正反两个方向都出候选。

    判据分不出「谁指向谁」——这一条也只能人签。测试把它钉住，是为了防止有人
    为了「候选好看」加一条去重规则，把方向问题藏起来。
    """
    schema, prof = _profile(
        tmp_path,
        "create table a (k integer not null, primary key (k));"
        "create table b (k integer not null, primary key (k));",
        ["insert into a values (1),(2)", "insert into b values (1),(2)"],
    )
    fks = [c for c in infer(schema, prof) if c.kind == "foreign_key"]
    assert {(c.payload["table"], c.payload["ref_table"]) for c in fks} == {("a", "b"), ("b", "a")}


def test_containment_must_be_total(tmp_path):
    """少一个值就不是外键——99% 包含率是脏数据，不是外键。"""
    schema, prof = _profile(
        tmp_path,
        "create table dim_guard (guard_key integer not null, primary key (guard_key));"
        "create table fact_misc (guard_key integer);",
        [
            "insert into dim_guard values (1),(2),(3),(4)",
            "insert into fact_misc values (1),(2),(99)",  # 99 不在父表里
        ],
    )
    assert [c for c in infer(schema, prof) if c.kind == "foreign_key"] == []


def test_target_must_be_unique(tmp_path):
    """目标列不唯一，连候选都不该出——C2 是必要条件。"""
    schema, prof = _profile(
        tmp_path,
        "create table dim_guard (guard_key integer, guard_name varchar(8));"
        "create table fact_misc (guard_key integer);",
        [
            "insert into dim_guard values (1,'a'),(1,'b'),(2,'c')",
            "insert into fact_misc values (1),(2)",
        ],
    )
    fks = [c for c in infer(schema, prof) if c.kind == "foreign_key"]
    # 反向那条（dim_guard.guard_key ⊆ fact_misc.guard_key）本来就成立——
    # 不唯一的是 dim_guard 作为「目标」的时候，C2 挡的是这个方向。
    assert not any(c.payload["ref_table"] == "dim_guard" for c in fks)
    assert [c.payload["ref_table"] for c in fks] == ["fact_misc"]


def test_declared_fk_is_exempt_and_not_duplicated(tmp_path):
    """DDL 声明过的走免批；同一对不再作为推断候选重复出现。"""
    schema = parse_ddl(DDL)
    attach_foreign_keys(
        schema,
        parse_foreign_keys(
            "alter table fact_order add constraint fk1 foreign key (store_id) references dim_store (store_id);"
        ),
    )
    db = _db(
        tmp_path / "y.db",
        DDL,
        [
            "insert into dim_store values (1,'上海'),(2,'北京'),(3,NULL)",
            "insert into fact_order values (1,1,10),(2,2,20),(3,3,30)",
            "insert into fact_no_pk values (1,'a')",
            "insert into dim_guard values (1,'x')",
        ],
    )
    prof = profile_sqlite(db, schema)
    fks = [c for c in infer(schema, prof) if c.kind == "foreign_key" and c.payload["table"] == "fact_order"]
    assert [f.payload["column"] for f in fks] == ["store_id"]
    assert _route(fks[0]) == "exempt"
    assert fks[0].evidence_form == "ddl_fk"


def test_grain_declared_exempt_inferred_signed(tmp_path):
    schema, prof = _profile(
        tmp_path,
        DDL,
        [
            "insert into dim_store values (1,'上海'),(2,'北京')",
            "insert into fact_order values (1,1,10),(2,2,20)",
            "insert into fact_no_pk values (1,'a'),(2,'b')",
            "insert into dim_guard values (1,'x')",
        ],
    )
    grains = {(c.payload["table"], tuple(c.payload["columns"])): c for c in infer(schema, prof) if c.kind == "grain"}
    assert _route(grains[("dim_store", ("store_id",))]) == "exempt"
    # 没有 PK 的表：单列唯一只能靠推断 → sign
    assert _route(grains[("fact_no_pk", ("event_id",))]) == "sign"
    # 只有一行、又没有 PK 时「唯一」是 0 == 0 算出来的，不该出候选
    assert ("dim_guard", ("guard_key",)) in grains  # 这句是声明的 PK，走 exempt
    assert _route(grains[("dim_guard", ("guard_key",))]) == "exempt"


def test_one_row_table_has_no_inferred_grain(tmp_path):
    """一行表的「唯一」是数学，不是数据。DDL 没声明 PK 时不许出候选。"""
    schema, prof = _profile(
        tmp_path,
        "create table single_row (a integer, b integer);",
        ["insert into single_row values (1,2)"],
    )
    assert [c for c in infer(schema, prof) if c.kind == "grain"] == []


def test_object_carries_columns_and_rows(tmp_path):
    schema, prof = _profile(
        tmp_path,
        DDL,
        [
            "insert into dim_store values (1,'上海')",
            "insert into fact_order values (1,1,10)",
            "insert into fact_no_pk values (1,'a')",
            "insert into dim_guard values (1,'x')",
        ],
    )
    objs = {c.payload["name"]: c for c in infer(schema, prof) if c.kind == "object"}
    assert _route(objs["dim_store"]) == "exempt"
    assert objs["dim_store"].payload["fields"]["row_count"] == 1
    assert [c["name"] for c in objs["dim_store"].payload["fields"]["columns"]] == ["store_id", "city"]


def test_infer_is_byte_stable(tmp_path):
    """propose 跑两遍输出一模一样——这条是 seal 幂等的前提。"""
    schema, prof = _profile(
        tmp_path,
        DDL,
        [
            "insert into dim_store values (1,'上海'),(2,'北京')",
            "insert into fact_order values (1,1,10),(2,2,20)",
            "insert into fact_no_pk values (1,'a'),(2,'b')",
            "insert into dim_guard values (1,'x')",
        ],
    )
    a = [c.as_row() for c in infer(schema, prof)]
    b = [c.as_row() for c in infer(schema, prof)]
    assert a == b


# ------------------------------------------------------------------ 外部答案
#
# TPC-DS 的两份 DDL 不在仓库里（TPC 保留版权，本仓库是 MIT）。
# 没有它们时这一组跳过，其余 67 个照跑。补法见 `python -m bench.fetch_tpcds`。

needs_tpcds = pytest.mark.skipif(
    not (T.DDL_PATH.exists() and T.RI_PATH.exists()),
    reason="缺 TPC-DS 的 DDL（跑 `python -m bench.fetch_tpcds` 看怎么拿）",
)


@pytest.fixture(scope="module")
def tpcds(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("tpcds")
    db = T.build(tmp / "tpcds.db")
    schema = T.load_schema()
    return schema, profile_sqlite(db, schema), T.answer_key(schema)


@needs_tpcds
def test_tpcds_data_only_is_useless(tpcds):
    """只靠 C1∧C2：102 条全找到，但混在近两千条里——precision 个位数百分比。"""
    schema, prof, key = tpcds
    cands = [c for c in infer(schema, prof, require_name_match=False) if c.kind == "foreign_key"]
    r = T.score(cands, key)
    assert r["recall"] == 1.0
    assert r["precision"] < 0.10
    assert r["candidates"] > 1000


@needs_tpcds
def test_tpcds_with_names_trades_recall_for_precision(tpcds):
    """加名字：precision 上到 0.86，代价是漏掉三分之一（全是命名缩写）。"""
    schema, prof, key = tpcds
    cands = [c for c in infer(schema, prof) if c.kind == "foreign_key"]
    r = T.score(cands, key)
    assert r["precision"] > 0.8
    assert 0.6 < r["recall"] < 0.8
    assert all(_route(c) == "sign" for c in cands)


@needs_tpcds
def test_tpcds_with_alias_table_recovers_recall(tpcds):
    """一张三条的缩写表把漏报补回来，代价是两个维表互串 24 条。"""
    schema, prof, key = tpcds
    aliases = {"addr": "address", "cdemo": "demo", "hdemo": "demo"}
    cands = [c for c in infer(schema, prof, name_aliases=aliases) if c.kind == "foreign_key"]
    r = T.score(cands, key)
    assert r["recall"] == 1.0
    assert r["precision"] > 0.7
    cross = [fp for fp in r["false_positives"] if fp[2] in ("customer_demographics", "household_demographics")]
    assert len(cross) == 24
