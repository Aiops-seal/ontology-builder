"""SO 层的断言：basic 与 derived 不能混。

这组测试钉的不是存储，是「口径不许被压扁」这件事：
basic 的定义由表结构决定、数据能验；derived 的口径由人定、数据验不了。
两者混起来，口径就会静默消失——`same_store_gmv` 曾经与 `gmv` 的表达式一模一样。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ontology_builder import store

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "ontology.v2.json"


def _raw() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _obj(raw: dict, name: str) -> dict:
    return next(o for o in raw["objects"] if o["name"] == name)


def _add(raw: dict, **item) -> dict:
    raw["objects"].append(item)
    return raw


def test_fixture_has_three_derived_metrics():
    """三个派生指标都在，且都带 inputs。三个正好覆盖三类口径分歧。"""
    state = store.state_from_json(FIXTURE)
    derived = {
        name: body["fields"]
        for (otype, name), body in state["objects"].items()
        if otype == "metric" and body["fields"]["kind"]["value"] == "derived"
    }
    assert set(derived) == {"avg_order_amount", "refund_rate", "same_store_gmv"}
    for name, fields in derived.items():
        assert fields["inputs"]["value"], f"{name} 的 inputs 是空的"


def test_same_store_gmv_is_bare_gmv_plus_scope():
    """同店 GMV 的 expr 就是单个 `gmv`，一个字都不加——差别全在 scope 上。

    这是范围口径最危险的形状：scope 一丢，它无声退化成 gmv，名字还叫「同店」。
    旧版写成 SUM(pay_amount)，与 gmv 的表达式一模一样，就是这个病。
    """
    raw = _raw()
    fields = _obj(raw, "same_store_gmv")["fields"]
    assert fields["expr"] == "gmv"
    assert fields["inputs"] == ["metric:gmv"]
    assert fields["scope"] == "dimension:same_store_set"

    state = store.state_from_dict(raw)
    dims = {n: b for (t, n), b in state["objects"].items() if t == "dimension"}
    assert "same_store_set" in dims
    assert dims["same_store_set"]["fields"]["definition"]["value"], "同店集合得有定义，不能只是个名字"


def test_metric_without_kind_is_rejected():
    raw = _raw()
    del _obj(raw, "avg_order_amount")["fields"]["kind"]
    with pytest.raises(store.StoreError) as exc:
        store.state_from_dict(raw)
    assert "缺 kind" in str(exc.value)


def test_basic_metric_may_not_have_inputs():
    """basic 直接聚合表列，引用别的指标就说明它是 derived。"""
    raw = _raw()
    _obj(raw, "gmv")["fields"]["inputs"] = ["metric:order_cnt"]
    with pytest.raises(store.StoreError) as exc:
        store.state_from_dict(raw)
    assert "basic" in str(exc.value)


def test_derived_metric_must_have_inputs():
    raw = _raw()
    _obj(raw, "refund_rate")["fields"]["inputs"] = []
    with pytest.raises(store.StoreError) as exc:
        store.state_from_dict(raw)
    assert "inputs 是空的" in str(exc.value)


def test_inputs_must_point_to_existing_metric():
    raw = _raw()
    _obj(raw, "refund_rate")["fields"]["inputs"] = ["metric:refund_amount", "metric:营收"]
    with pytest.raises(store.StoreError) as exc:
        store.state_from_dict(raw)
    assert "metric:营收" in str(exc.value)
    assert "不静默忽略" in str(exc.value)


def test_inputs_may_not_reference_table_columns():
    """derived 只能引用指标。引用表列等于绕过口径——那正是 old same_store_gmv 的病。"""
    raw = _raw()
    _obj(raw, "avg_order_amount")["fields"]["inputs"] = ["table:dwd_order_di"]
    with pytest.raises(store.StoreError) as exc:
        store.state_from_dict(raw)
    assert "metric:名字" in str(exc.value)


def test_derived_cycle_is_rejected():
    raw = _raw()
    _add(
        raw,
        type="metric",
        name="loop_a",
        fields={"kind": "derived", "expr": "loop_b", "inputs": ["metric:loop_b"]},
    )
    _add(
        raw,
        type="metric",
        name="loop_b",
        fields={"kind": "derived", "expr": "loop_a", "inputs": ["metric:loop_a"]},
    )
    with pytest.raises(store.StoreError) as exc:
        store.state_from_dict(raw)
    assert "成环" in str(exc.value)


def test_derived_chain_is_readable_from_edges(tmp_path):
    """派生链在图上能走通：same_store_gmv → gmv → dwd_order_di。

    这条链上三段是**三种不同的关系**，v0.2 用 `derived_from` 一个词全包了：
    指标→指标（inputs）、指标→表（computed_from）、指标→维度（restricted_to）。
    拆开之后，每一段的守卫才能各管各的。
    """
    conn = store.connect(tmp_path / "o.db")
    store.init(conn)
    store.commit(conn, store.state_from_json(FIXTURE), note="t", at="2026-11-27T09:00:00")
    state = store.load(conn)

    edges = set(state["edges"])
    assert ("metric:same_store_gmv", "inputs", "metric:gmv") in edges
    assert ("metric:gmv", "computed_from", "table_object:dwd_order_di") in edges
    assert ("metric:same_store_gmv", "restricted_to", "dimension:same_store_set") in edges
    # 事件到指标不是「派生」：它拿这个指标当判定依据，不产生新数值
    assert (
        "event:single_store_refund_spike",
        "evidenced_by",
        "metric:refund_rate",
    ) in edges
    conn.close()
