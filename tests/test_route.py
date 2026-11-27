"""免批判定：白名单 + 位置空。

这里钉的是 SPEC §4.3 的白名单，以及「route 在写库那一刻算」这条——同一个候选，
第一次提能免批，第二次提就必须签。
"""

import sqlite3

import pytest

from ontology_builder import route, store


# ------------------------------------------------------------------ 白名单本身


def test_whitelist_has_exactly_three_entries():
    """三条不是凑出来的，是 TPC-DS 上实跑能免的那些情形。加一条要想清楚依据。"""
    assert set(route.EXEMPT_WHITELIST) == {
        ("object", "ddl_table"),
        ("grain", "ddl_pk"),
        ("foreign_key", "ddl_fk"),
    }


@pytest.mark.parametrize("kind,form", sorted(route.EXEMPT_WHITELIST))
def test_whitelisted_forms_are_exempt(kind, form):
    assert route.decide_route(occupied=False, kind=kind, evidence_form=form) == "exempt"


@pytest.mark.parametrize(
    "kind,form",
    [("grain", "data_unique"), ("foreign_key", "data_containment")],
)
def test_inferred_forms_are_sign(kind, form):
    """从数据算出来的统计事实，再硬也不免——两个互不相干的代理键也是 100% 包含。"""
    assert route.decide_route(occupied=False, kind=kind, evidence_form=form) == "sign"


def test_crossed_pair_is_sign():
    """白名单查的是二元组，不是两个独立的集合。"""
    assert (
        route.decide_route(occupied=False, kind="object", evidence_form="data_containment")
        == "sign"
    )


def test_overwriting_a_confirmed_value_never_exempt():
    """位置不空压过白名单：被覆盖的值从库里消失，没地方找回来。"""
    assert (
        route.decide_route(occupied=True, kind="object", evidence_form="ddl_table") == "sign"
    )


def test_unknown_placement_is_sign():
    """落点判不出来（新 kind 没定义往哪写）→ 保守签，不许静默免批。"""
    assert (
        route.decide_route(occupied=None, kind="object", evidence_form="ddl_table") == "sign"
    )


def test_unknown_evidence_form_raises():
    """写错的形态要炸，不能静默判成「不在白名单」——那会让 bug 看起来像判定。"""
    with pytest.raises(ValueError, match="未知证据形态"):
        route.check_form("ddl_whatever")


def test_basis_is_readable():
    """验收第 5 条：每条免批要能指着一句话说话；不在表里就没有依据可指。"""
    for pair in route.EXEMPT_WHITELIST:
        assert route.basis_of(*pair)
    assert route.basis_of("grain", "data_unique") is None


# ------------------------------------------------------------------ 落点


def test_target_of_known_kinds():
    assert route.target_of("object", {"type": "table_object", "name": "x"}) == (
        "object",
        "table_object:x",
    )
    assert route.target_of("grain", {"table": "x"}) == ("field", "table_object:x", "grain")
    assert route.target_of(
        "foreign_key", {"table": "a", "ref_table": "b", "predicate": "joins_on"}
    ) == ("edge", "table_object:a", "joins_on", "table_object:b")


def test_target_of_unknown_kind_is_none():
    assert route.target_of("metric_alias", {"name": "x"}) is None
    # 缺字段也不许猜出一个落点
    assert route.target_of("object", {"name": "x"}) is None


def test_target_of_edge_without_predicate_is_none():
    """谓词不给缺省。缺省成 `joins_on` 等于把「没说」当成「物理边」——那是两件事。"""
    assert route.target_of("foreign_key", {"table": "a", "ref_table": "b"}) is None


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
    first = route.route_for(conn, None, "grain", payload, evidence_form="ddl_pk")
    second = route.route_for(
        conn, store.current_version(conn), "grain", payload, evidence_form="ddl_pk"
    )

    assert first == "exempt"
    assert second == "sign"
    conn.close()


def test_sign_form_does_not_query_the_db():
    """不在白名单直接返回，连接传 None 也不炸——说明它真没查库。"""
    assert (
        route.route_for(None, None, "grain", {"table": "x"}, evidence_form="data_unique")
        == "sign"
    )


def test_add_candidates_writes_route_computed_from_db(tmp_path):
    """`add_candidates` 写库时算 route，不读候选自带的字段——候选根本没有那个字段。"""
    conn = _conn(tmp_path)

    class _C:
        kind = "grain"
        payload = {"table": "x"}
        evidence = {}
        evidence_form = "ddl_pk"

    assert store.add_candidates(conn, [_C()], at="2026-11-27T10:00:00") == ["exempt"]

    st = store.empty_state("t")
    st["objects"][("table_object", "x")] = {
        "fields": {"grain": _field(["k"], by="owner")}
    }
    store.commit(conn, st, note="t", at="2026-11-27T10:05:00")

    assert store.add_candidates(conn, [_C()], at="2026-11-27T10:06:00") == ["sign"]
    conn.close()


def test_candidate_row_keeps_evidence_form(tmp_path):
    """形态要落库。否则半年后没人解释得清这条当初凭什么免。"""
    conn = _conn(tmp_path)
    store.add_candidate(
        conn, "object", {"type": "table_object", "name": "x"}, "ddl_table", at="2026-11-27"
    )
    row = store.candidates(conn)[0]
    assert row["evidence_form"] == "ddl_table"
    assert row["route"] == "exempt"
    conn.close()


def test_add_candidate_rejects_unknown_form(tmp_path):
    """手写候选也得说清形态；说不清就炸，不能默认成能免。"""
    conn = _conn(tmp_path)
    with pytest.raises(ValueError, match="未知证据形态"):
        store.add_candidate(
            conn, "object", {"type": "table_object", "name": "x"}, "看着像外键"
        )
    conn.close()
