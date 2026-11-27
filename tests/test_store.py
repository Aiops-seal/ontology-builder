"""存储层的断言。

这里钉的不是「代码能跑」，是四条契约：
一句内容相同就不产生新版本 / 旧版本读得回来 / 确认是字段级的 / 候选写不进本体。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ontology_builder import store

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "ontology.v2.json"


@pytest.fixture()
def conn(tmp_path):
    c = store.connect(tmp_path / "onto.db")
    store.init(c)
    yield c
    c.close()


def _import(conn, path=FIXTURE, note="test"):
    state = store.state_from_json(path)
    return store.commit(conn, state, note=note, at="2026-11-27T09:00:00")


def test_roundtrip_state_identical(conn, tmp_path):
    """json → 库 → json，state 一模一样。"""
    before = store.state_from_json(FIXTURE)
    vid, created = _import(conn)
    assert created and vid == 1
    after = store.load(conn, vid)
    assert store.state_hash(before) == store.state_hash(after)

    out = store.export(conn, tmp_path / "out.json")
    reloaded = store.state_from_json(out)
    assert store.state_hash(before) == store.state_hash(reloaded)


def test_export_is_byte_stable(conn, tmp_path):
    """同一份 state 导出两次，字节一致——不然每次 commit 都会多一个假版本。"""
    _import(conn)
    a = store.export(conn, tmp_path / "a.json")
    b = store.export(conn, tmp_path / "b.json")
    assert a.read_bytes() == b.read_bytes()


def test_same_content_does_not_bump_version(conn):
    """内容哈希相同就不产生新版本：重试多少次，版本号只递增一次。"""
    vid1, created1 = _import(conn)
    vid2, created2 = _import(conn)
    assert (vid1, created1) == (1, True)
    assert (vid2, created2) == (1, False)


def test_new_version_keeps_history(conn):
    """改一个字段 → 新版本；旧版本仍然读得回来，且内容没被改。"""
    _import(conn)
    old = store.load(conn, 1)

    new_state = store.load(conn, 1)
    new_state["objects"][("metric", "gmv")]["fields"]["expr"]["value"] = "SUM(paid_amt)"
    vid2, created = store.commit(conn, new_state, note="改 gmv 表达式", at="2026-11-27T10:00:00")
    assert created and vid2 == 2

    assert store.current_version(conn) == 2
    assert store.load(conn, 1)["objects"][("metric", "gmv")]["fields"]["expr"]["value"] == "SUM(pay_amount)"
    assert store.load(conn, 2)["objects"][("metric", "gmv")]["fields"]["expr"]["value"] == "SUM(paid_amt)"


def test_confirmation_is_per_field(conn):
    """改 aliases 的确认，do_not 的确认与人不受影响。"""
    _import(conn)
    state = store.load(conn)
    gmv = state["objects"][("metric", "gmv")]["fields"]

    gmv["aliases"]["by"] = "someone_else"
    gmv["aliases"]["at"] = "2026-11-28"
    vid, _ = store.commit(conn, state, note="换人复核别名", at="2026-11-28T09:00:00")

    after = store.load(conn, vid)["objects"][("metric", "gmv")]["fields"]
    assert after["aliases"]["by"] == "someone_else"
    assert after["do_not"]["by"] == "aiops"
    assert after["do_not"]["at"] == "2026-11-27"

    exported = store.state_to_dict(store.load(conn, vid))
    gmv_json = next(o for o in exported["objects"] if o["name"] == "gmv")
    assert set(gmv_json["confirmed"]) == {"aliases", "do_not"}
    assert gmv_json["confirmed"]["aliases"]["by"] == "someone_else"


def test_only_one_current_version(conn):
    _import(conn)
    state = store.load(conn)
    state["objects"][("metric", "gmv")]["fields"]["expr"]["value"] = "SUM(x)"
    store.commit(conn, state, note="v2")
    rows = conn.execute("SELECT id FROM version WHERE is_current = 1").fetchall()
    assert len(rows) == 1
    assert rows[0]["id"] == 2


def test_predicate_must_be_in_vocabulary(conn):
    """谓词不在词表里就拒——自由字符串等于没约束。"""
    state = store.state_from_json(FIXTURE)
    state["edges"].append(("metric:gmv", "随便写的谓词", "dimension:channel"))
    with pytest.raises(store.StoreError) as exc:
        store.commit(conn, state, note="bad edge")
    assert "受控词表" in str(exc.value)


def test_candidate_cannot_reach_ontology(conn):
    """候选写进 candidate 表，本体一点不动。"""
    _import(conn)
    before = store.state_hash(store.load(conn))
    store.add_candidate(
        conn,
        "probe",
        {"type": "table_object", "name": "fake_table", "fields": {"grain": "x"}},
        evidence={"inclusion_rate": 0.0},
        at="2026-11-27T09:30:00",
    )
    assert store.state_hash(store.load(conn)) == before
    assert conn.execute("SELECT count(*) AS c FROM candidate").fetchone()["c"] == 1
    assert conn.execute(
        "SELECT count(*) AS c FROM object WHERE id = 'table_object:fake_table'"
    ).fetchone()["c"] == 0


def test_version_1_1_is_rejected(conn, tmp_path):
    """旧版本号直接拒，不做兼容层。"""
    old = tmp_path / "old.json"
    old.write_text(
        json.dumps({"version": "1.1", "domain": "retail", "tables": [], "metrics": []}),
        encoding="utf-8",
    )
    with pytest.raises(store.StoreError) as exc:
        store.state_from_json(old)
    assert "2.0" in str(exc.value)


def test_policy_layer_is_storable(conn):
    """7S 的节点和跨层边能存能取——容器这一步先成立。"""
    _import(conn)
    state = store.load(conn)
    types = {t for (t, _) in state["objects"]}
    assert {"decision", "event", "term"} <= types
    assert ("decision:refund_review", "based_on", "event:single_store_refund_spike") in state["edges"]
