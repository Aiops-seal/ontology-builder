"""免批判定：三问全过才 exempt。

这里钉的是 SPEC §4.3 的三问，以及「route 在写库那一刻算」这条——同一个候选，
第一次提能免批，第二次提就必须签。
"""

import sqlite3

import pytest

from ontology_builder import route, store


# ------------------------------------------------------------------ 三问本身


def test_three_questions_all_pass():
    assert route.decide_route(occupied=False, impact="local", declared=True) == "exempt"


def test_overwriting_a_confirmed_value_never_exempt():
    """第一问「否」压过一切：被覆盖的值从库里消失，没地方找回来。"""
    assert route.decide_route(occupied=True, impact="local", declared=True) == "sign"


def test_structural_change_never_exempt():
    """第二问「否」：新增一条语义边，位置确实是空的，但它改可达性。"""
    assert route.decide_route(occupied=False, impact="structural", declared=True) == "sign"


def test_inferred_evidence_never_exempt():
    """第三问「否」：从数据算出来的只是统计事实，不是答案。"""
    assert route.decide_route(occupied=False, impact="local", declared=False) == "sign"


def test_unknown_placement_is_sign():
    """落点判不出来（新 kind 没定义往哪写）→ 保守签，不许静默免批。"""
    assert route.decide_route(occupied=None, impact="local", declared=True) == "sign"


# ------------------------------------------------------------------ impact


@pytest.mark.parametrize("kind,payload", [
    ("object", {"type": "table_object", "name": "dim_store"}),
    ("grain", {"table": "dim_store", "columns": ["store_id"]}),
    ("foreign_key", {"table": "a", "ref_table": "b", "predicate": "joins_on"}),
])
def test_local_impacts(kind, payload):
    assert route.impact_of(kind, payload) == "local"


@pytest.mark.parametrize("predicate", sorted(route.SEMANTIC_PREDICATES))
def test_semantic_predicates_are_structural(predicate):
    """语义谓词的边一多，那个对象就不是原来的对象了——`inputs` 从 1 个变 2 个，
    口径就变了。`joins_on` 不在里面：它只是多一条能走的路。"""
    assert (
        route.impact_of(
            "foreign_key", {"table": "a", "ref_table": "b", "predicate": predicate}
        )
        == "structural"
    )


def test_unknown_kind_is_structural_not_local():
    """「判不了」和「局部」是两件事。把前者当后者，是最容易漏的那种漏法。"""
    assert route.impact_of("metric_alias", {}) == "structural"


# ------------------------------------------------------------------ 落点


def test_target_of_known_kinds():
    assert route.target_of("object", {"type": "table_object", "name": "x"}) == (
        "object",
        "table_object:x",
    )
    assert route.target_of("grain", {"table": "x"}) == ("field", "table_object:x", "grain")
    assert route.target_of(
        "foreign_key", {"table": "a", "ref_table": "b"}
    ) == ("edge", "table_object:a", "joins_on", "table_object:b")


def test_target_of_unknown_kind_is_none():
    assert route.target_of("metric_alias", {"name": "x"}) is None
    # 缺字段也不许猜出一个落点
    assert route.target_of("object", {"name": "x"}) is None


# ------------------------------------------------------------------ 查库


def _field(value, by=None):
    return {"value": value, "by": by, "at": "2026-11-27" if by else None, "source": None}


def _conn(tmp_path):
    conn = store.connect(tmp_path / "r.db")
    store.init(conn)
    return conn


def test_empty_library_has_nothing_occupied(tmp_path):
    conn = _conn(tmp_path)
    assert route.is_occupied(conn, None, ("object", "table_object:x")) is False
    assert route.is_occupied(conn, None, ("edge", "a", "joins_on", "b")) is False
    conn.close()


def test_unconfirmed_field_is_not_occupied(tmp_path):
    """有值但没人签 = 草稿态，覆盖它是正常流程。

    把「有值」当「有已签的值」，会让第二批候选全部莫名要签——这是最容易写错的一条。
    """
    conn = _conn(tmp_path)
    st = store.empty_state("t")
    st["objects"][("table_object", "x")] = {"fields": {"grain": _field(["k"])}}
    store.commit(conn, st, note="t", at="2026-11-27T10:00:00")
    vid = store.current_version(conn)

    assert route.is_occupied(conn, vid, ("field", "table_object:x", "grain")) is False
    conn.close()


def test_confirmed_field_is_occupied(tmp_path):
    conn = _conn(tmp_path)
    st = store.empty_state("t")
    st["objects"][("table_object", "x")] = {
        "fields": {"grain": _field(["k"], by="owner")}
    }
    store.commit(conn, st, note="t", at="2026-11-27T10:00:00")
    vid = store.current_version(conn)

    assert route.is_occupied(conn, vid, ("field", "table_object:x", "grain")) is True
    conn.close()


def test_existing_object_and_edge_are_occupied(tmp_path):
    conn = _conn(tmp_path)
    st = store.empty_state("t")
    st["objects"][("table_object", "x")] = {"fields": {}}
    st["objects"][("table_object", "y")] = {"fields": {}}
    st["edges"] = [("table_object:x", "joins_on", "table_object:y")]
    store.commit(conn, st, note="t", at="2026-11-27T10:00:00")
    vid = store.current_version(conn)

    assert route.is_occupied(conn, vid, ("object", "table_object:x")) is True
    assert route.is_occupied(conn, vid, ("edge", "table_object:x", "joins_on", "table_object:y")) is True
    # 换个谓词就是另一条边——「改谓词等于换一条边」
    assert route.is_occupied(conn, vid, ("edge", "table_object:x", "belongs_to", "table_object:y")) is False
    conn.close()


# ------------------------------------------------------------------ 端到端


def test_same_candidate_exempt_then_sign(tmp_path):
    """同一批候选，跑到第二版就变了：位置已经不是我第一次写的地方了。"""
    conn = _conn(tmp_path)
    st = store.empty_state("t")
    st["objects"][("table_object", "x")] = {
        "fields": {"grain": _field(["k"], by="owner")}
    }
    store.commit(conn, st, note="t", at="2026-11-27T10:00:00")

    payload = {"table": "x"}
    first = route.route_for(conn, None, "grain", payload, declared=True)  # 库里还没有版本
    second = route.route_for(conn, store.current_version(conn), "grain", payload, declared=True)

    assert first == "exempt"
    assert second == "sign"
    conn.close()


def test_add_candidates_writes_route_computed_from_db(tmp_path):
    """`add_candidates` 写库时算 route，不读候选自带的字段——候选根本没有那个字段。"""
    conn = _conn(tmp_path)

    class _C:
        kind = "grain"
        payload = {"table": "x"}
        evidence = {"declared": True}
        declared = True

    assert store.add_candidates(conn, [_C()], at="2026-11-27T10:00:00") == ["exempt"]

    st = store.empty_state("t")
    st["objects"][("table_object", "x")] = {
        "fields": {"grain": _field(["k"], by="owner")}
    }
    store.commit(conn, st, note="t", at="2026-11-27T10:05:00")

    assert store.add_candidates(conn, [_C()], at="2026-11-27T10:06:00") == ["sign"]
    conn.close()
