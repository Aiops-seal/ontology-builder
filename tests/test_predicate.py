"""谓词层：每个词必须说得出两端什么类型、什么时候允许用。

v0.2 的 7 个词里，`derived_from` 一个词干了三种语义（指标→表、指标→指标、事件→指标），
`belongs_to` 一条边都没用，`describes` 和它同义。这里钉住拆完之后的形状。
"""

import json
import sqlite3
from pathlib import Path

import pytest

from ontology_builder import store

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "ontology.v2.json"


def _state(objects, edges):
    st = store.empty_state("t")
    for otype, name in objects:
        st["objects"][(otype, name)] = {"fields": {}}
    st["edges"] = list(edges)
    return st


def _init(tmp_path):
    conn = store.connect(tmp_path / "o.db")
    store.init(conn)
    return conn


# ---------------------------------------------------------------- 词表本身


def test_every_predicate_says_its_layer_and_ends(tmp_path):
    """八个词，每个都带 layer；除了策略层那个没建完的，两端类型必须写死。"""
    conn = _init(tmp_path)
    rows = {r["name"]: r for r in conn.execute("SELECT * FROM predicate")}
    assert set(rows) == set(store.SEED_PREDICATES)
    assert len(rows) == 8

    for name, r in rows.items():
        assert r["layer"] in ("structural", "semantic", "strategic"), name
        if name == "based_on":  # 7S 没建完，两端暂不限制
            assert json.loads(r["src_types"]) == []
            continue
        assert json.loads(r["src_types"]), f"{name} 没写 src 类型"
        assert json.loads(r["dst_types"]), f"{name} 没写 dst 类型"
        assert r["note"], f"{name} 没写守卫"
    conn.close()


def test_no_dead_predicates_in_fixture(tmp_path):
    """每个词都至少有一条边在用，也没有一条边用着不存在的词。

    死词不是「留着将来用」——它会让词表看起来比实际宽，而宽出来的部分没人验。
    """
    conn = _init(tmp_path)
    store.commit(conn, store.state_from_json(FIXTURE), note="t", at="2026-11-27T09:00:00")
    used = {p for (_, p, _) in store.load(conn)["edges"]}
    assert used == set(store.SEED_PREDICATES)
    conn.close()


def test_derived_from_is_gone(tmp_path):
    """老词必须真的消失。留着它等于留了一条绕过三种守卫的路。"""
    conn = _init(tmp_path)
    names = {r["name"] for r in conn.execute("SELECT name FROM predicate")}
    assert "derived_from" not in names
    assert "describes" not in names
    conn.close()


# ---------------------------------------------------------------- 类型校验


def test_event_cannot_compute_from_metric(tmp_path):
    """v0.2 让 `event -[derived_from]-> metric` 一路走通，因为两端从来没被问过。"""
    conn = _init(tmp_path)
    st = _state(
        [("event", "spike"), ("metric", "rate")],
        [("event:spike", "computed_from", "metric:rate")],
    )
    with pytest.raises(store.StoreError, match="只允许"):
        store.commit(conn, st, note="t", at="2026-11-27T09:00:00")
    conn.close()


def test_metric_to_metric_must_use_inputs(tmp_path):
    """指标指向指标是 inputs，不是 computed_from——后者只接表。"""
    conn = _init(tmp_path)
    st = _state(
        [("metric", "a"), ("metric", "b")],
        [("metric:a", "computed_from", "metric:b")],
    )
    with pytest.raises(store.StoreError, match="computed_from 只允许"):
        store.commit(conn, st, note="t", at="2026-11-27T09:00:00")
    conn.close()


def test_dangling_edge_is_rejected(tmp_path):
    """端点不在本体里的边，图上走不通，也没法审。"""
    conn = _init(tmp_path)
    st = _state([("metric", "a")], [("metric:a", "inputs", "metric:ghost")])
    with pytest.raises(store.StoreError, match="不在本体里"):
        store.commit(conn, st, note="t", at="2026-11-27T09:00:00")
    conn.close()


def test_failed_validation_leaves_no_version(tmp_path):
    """校验失败不能留下一个空壳版本——它会让下一次 commit 从错的地方续号。"""
    conn = _init(tmp_path)
    bad = _state([("metric", "a")], [("metric:a", "inputs", "metric:ghost")])
    with pytest.raises(store.StoreError):
        store.commit(conn, bad, note="t", at="2026-11-27T09:00:00")
    assert store.current_version(conn) is None
    assert conn.execute("SELECT COUNT(*) AS c FROM version").fetchone()["c"] == 0
    conn.close()


# ---------------------------------------------------------------- 改谓词 = 换一条边


def test_changing_predicate_makes_a_new_version(tmp_path):
    """同一对端点换个谓词，必须是一条边作废、另一条边新建，而不是原地改名。

    这条靠两件事保证：(src, predicate, dst) 一起进主键，且内容哈希把谓词算进去。
    少了任何一个，改谓词都会被幂等逻辑当成「没变」而静默丢掉。
    """
    conn = _init(tmp_path)
    a = _state(
        [("metric", "m"), ("table_object", "t")],
        [("metric:m", "computed_from", "table_object:t")],
    )
    v1, created1 = store.commit(conn, a, note="v1", at="2026-11-27T09:00:00")

    b = _state(
        [("metric", "m"), ("table_object", "t"), ("dimension", "d")],
        [("metric:m", "restricted_to", "dimension:d")],
    )
    v2, created2 = store.commit(conn, b, note="v2", at="2026-11-27T09:01:00")
    assert created1 and created2 and v2 == v1 + 1

    assert store.load(conn, v1)["edges"] == [("metric:m", "computed_from", "table_object:t")]
    assert store.load(conn, v2)["edges"] == [("metric:m", "restricted_to", "dimension:d")]
    conn.close()


# ---------------------------------------------------------------- 老库迁移


def test_old_predicate_table_gets_new_columns(tmp_path):
    """v0.2 的 predicate 表只有 name / note，init 必须把三列补上再灌词。"""
    db = tmp_path / "old.db"
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE predicate (name TEXT PRIMARY KEY, note TEXT NOT NULL DEFAULT '')")
    conn.execute("INSERT INTO predicate (name, note) VALUES ('derived_from', '老词')")
    conn.commit()
    conn.close()

    conn = store.connect(db)
    stale = store.init(conn)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(predicate)")}
    assert {"layer", "src_types", "dst_types"} <= cols
    # 没有边引用的老词直接清掉，不返回
    assert "derived_from" not in {r["name"] for r in conn.execute("SELECT name FROM predicate")}
    assert stale == []
    conn.close()


def test_predicate_still_used_by_old_version_is_kept_and_reported(tmp_path):
    """词表收窄了、但历史版本还有边在用——不能删（那是既成事实），但要报出来。

    静默保留最糟：库里多一个词，代码里没有，谁都不知道该按哪套规矩走。
    """
    conn = _init(tmp_path)
    store.commit(
        conn,
        _state(
            [("metric", "a"), ("metric", "b")],
            [("metric:a", "inputs", "metric:b")],
        ),
        note="v1",
        at="2026-11-27T09:00:00",
    )
    conn.execute(
        "INSERT INTO predicate (name, layer, src_types, dst_types, note) "
        "VALUES ('legacy_pred', 'semantic', '[]', '[]', '老词')"
    )
    conn.execute(
        "UPDATE edge SET predicate = 'legacy_pred' WHERE predicate = 'inputs'"
    )
    conn.commit()

    stale = store.init(conn)
    assert stale == ["legacy_pred"]
    assert "legacy_pred" in {r["name"] for r in conn.execute("SELECT name FROM predicate")}
    conn.close()
