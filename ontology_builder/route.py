"""免批判定：三问，全过才 exempt。

这个模块只做一件事——把 SPEC §4.3 那三个问题变成可执行的判断。它不产生候选，
也不写库（除了读）。判定的输入全部是事实：落点有没有已确认的值、影响是局部还是
结构、证据是不是声明式的。

**为什么判定不能放在候选生产者里。** 第一问要查库，而 `infer` 只看得见 schema 和
数据，看不见「这一版本体里那个位置是不是已经签过了」。放在生产者里，只能靠调用方
传一个布尔值进来，而那个值没人会老老实实传对。所以：生产者声明 `impact` 和证据
类型（它们自己知道），`route` 在写库那一刻算。
"""

from __future__ import annotations

import sqlite3
from typing import Any

ROUTE_EXEMPT = "exempt"  # 三问全过，跳过人审链，直接进权威状态
ROUTE_SIGN = "sign"  # 其余，进草稿等人签

IMPACT_LOCAL = "local"  # 错了删掉就回来了，不牵动任何已确认的结论
IMPACT_STRUCTURAL = "structural"  # 会改变语义图的可达性

# 语义谓词：多一条，那个对象就不是原来的对象了。`joins_on` 不在里面——它是物理
# 路径，多一条少一条，没有任何指标的口径变意思。
SEMANTIC_PREDICATES = frozenset(
    {
        "inputs",
        "computed_from",
        "belongs_to",
        "restricted_to",
        "evidenced_by",
        "same_as",
        "based_on",
    }
)


def decide_route(*, occupied: bool | None, impact: str, declared: bool) -> str:
    """三问。`occupied=None` 表示查不出来（落点未知的 kind），保守判 sign。

    顺序不是随意的：先判「会不会覆盖已签值」，因为那是**不可逆**的——被覆盖的值
    从库里消失，没有地方能找回来。另外两条错了还能删掉重来。
    """
    if occupied is None:
        return ROUTE_SIGN
    if occupied:
        return ROUTE_SIGN
    if impact != IMPACT_LOCAL:
        return ROUTE_SIGN
    if not declared:
        return ROUTE_SIGN
    return ROUTE_EXEMPT


def impact_of(kind: str, payload: dict[str, Any]) -> str:
    """候选的落点会不会越出这一处。

    只对已知 kind 有答案。未知 kind 返回 `structural`——判不了就当结构性的，
    因为「判不了」和「局部」是两件事，把前者当后者是最容易漏的那种漏法。
    """
    if kind in ("object", "grain"):
        return IMPACT_LOCAL
    if kind == "foreign_key":
        predicate = payload.get("predicate", "joins_on")
        return IMPACT_STRUCTURAL if predicate in SEMANTIC_PREDICATES else IMPACT_LOCAL
    return IMPACT_STRUCTURAL


def target_of(kind: str, payload: dict[str, Any]) -> tuple | None:
    """候选要写到哪个位置。判「位置原本是空的吗」要用它。

    返回 None 表示这个 kind 的落点还没有定义——那种情况下 `is_occupied` 也返回
    None，route 保守判 sign。这是有意的：加一个新 kind 就得同时说清它往哪写，
    不说清就永远签，不会静默免批。
    """
    if kind == "object":
        obj_type = payload.get("type")
        name = payload.get("name")
        if not obj_type or not name:
            return None
        return ("object", f"{obj_type}:{name}")
    if kind == "grain":
        table = payload.get("table")
        if not table:
            return None
        return ("field", f"table_object:{table}", "grain")
    if kind == "foreign_key":
        table, ref = payload.get("table"), payload.get("ref_table")
        if not table or not ref:
            return None
        predicate = payload.get("predicate", "joins_on")
        return ("edge", f"table_object:{table}", predicate, f"table_object:{ref}")
    return None


def is_occupied(conn: sqlite3.Connection, version_id: int | None, target: tuple | None) -> bool | None:
    """那个位置有没有**已确认**的值。

    只算已确认的。库里有一行但没人签过，那是草稿态，覆盖它正是正常流程——把
    「有值」和「有已签的值」混成一件事，会让第二批候选全部莫名要签。
    """
    if target is None:
        return None
    if version_id is None:
        return False  # 库还是空的，任何位置都是空的

    head = target[0]
    if head == "object":
        row = conn.execute(
            "SELECT 1 FROM object WHERE version_id = ? AND id = ?", (version_id, target[1])
        ).fetchone()
        return row is not None
    if head == "field":
        row = conn.execute(
            "SELECT confirmed_by FROM field WHERE version_id = ? AND object_id = ? AND name = ?",
            (version_id, target[1], target[2]),
        ).fetchone()
        return bool(row and row["confirmed_by"])
    if head == "edge":
        row = conn.execute(
            "SELECT 1 FROM edge WHERE version_id = ? AND src = ? AND predicate = ? AND dst = ?",
            (version_id, target[1], target[2], target[3]),
        ).fetchone()
        return row is not None
    return None


def route_for(conn: sqlite3.Connection, version_id: int | None, kind: str, payload: dict[str, Any], *, declared: bool) -> str:
    """生产者的调用入口：它只说 kind / payload / declared，其余全在这里判。"""
    return decide_route(
        occupied=is_occupied(conn, version_id, target_of(kind, payload)),
        impact=impact_of(kind, payload),
        declared=declared,
    )
