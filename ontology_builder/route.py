"""免批判定：白名单 + 一处不可逆。

这个模块只做两件事——查表（这条候选在不在白名单里）、查库（它要写的位置是不是
已经签过了）。它不产生候选，也不写库。

**为什么判定不能放在候选生产者里。** 位置要查库，而 `infer` 只看得见 schema 和
数据，看不见「这一版本体里那个位置是不是已经签过了」。而且放行的规则如果也住在
生产者里，那个模块就开始自我审查——只产肯定能过的候选。所以：生产者只报
`evidence_form`（枚举值，有据可查），能不能免由这张表说了算。
"""

from __future__ import annotations

import sqlite3
from typing import Any

ROUTE_EXEMPT = "exempt"  # 在白名单里 + 位置空 → 跳过人审链，直接进权威状态
ROUTE_SIGN = "sign"  # 其余，进草稿等人签

# 证据形态。这是生产者的全部发言权，它说不了「我可以免」。
# `ddl_*` 是照抄建表语句，本体只换个地方存；`data_*` 是从数据里算出来的统计事实。
EVIDENCE_FORMS = frozenset(
    {"ddl_table", "ddl_pk", "ddl_fk", "data_unique", "data_containment"}
)

# 免批白名单：(kind, evidence_form) -> 凭什么可以免。
#
# 三条的共性是**零新语义**：删掉这条候选，本体里没有任何已确认的结论会变意思。
# 判据是「建表语句里是不是已经写着」，不是「证据硬不硬」——`data_containment` 的
# 证据可以非常硬（包含率 100%），它依然要签，因为两个互不相干的代理键也是这样。
#
# 这份表**每条都要带依据**：它会自我强化，第一次签过的情形第二次就免了。半年后
# 没人说得清能不能删，表就退化成台账。
EXEMPT_WHITELIST: dict[tuple[str, str], str] = {
    ("object", "ddl_table"): "DDL 里有这张表，本体只是给它一个 id",
    ("grain", "ddl_pk"): "DDL 写了 PRIMARY KEY，抄一遍",
    ("foreign_key", "ddl_fk"): "DDL 写了 FOREIGN KEY，抄一遍",
}


def check_form(evidence_form: str) -> None:
    """不认识的形态直接报错，不当成「不在白名单」。

    写错的形态被判成 sign，会让 bug 看起来像判定——那是这一版最想避免的一类静默。
    """
    if evidence_form not in EVIDENCE_FORMS:
        raise ValueError(
            f"未知证据形态 {evidence_form!r}；允许：{sorted(EVIDENCE_FORMS)}"
        )


def whitelisted(kind: str, evidence_form: str) -> bool:
    return (kind, evidence_form) in EXEMPT_WHITELIST


def basis_of(kind: str, evidence_form: str) -> str | None:
    """这条免批的依据。验收第 5 条要能指着它说话；不在表里返回 None。"""
    return EXEMPT_WHITELIST.get((kind, evidence_form))


def decide_route(*, occupied: bool | None, kind: str, evidence_form: str) -> str:
    """两问。`occupied=None` 表示查不出来（落点未知的 kind），保守判 sign。

    顺序不是随意的：先判「会不会覆盖已签值」，因为那是**不可逆**的——被覆盖的值
    从库里消失，没有地方能找回来。白名单说错了还能改表重判。
    """
    if occupied is None or occupied:
        return ROUTE_SIGN
    if not whitelisted(kind, evidence_form):
        return ROUTE_SIGN
    return ROUTE_EXEMPT


def target_of(kind: str, payload: dict[str, Any]) -> tuple | None:
    """候选要写到哪个位置。判「位置原本是空的吗」要用它。

    返回 None 表示这个 kind 的落点还没有定义——那种情况下 `is_occupied` 也返回
    None，route 保守判 sign。这是有意的：加一个新 kind 就得同时说清它往哪写，
    不说清就永远签，不会静默免批。

    **谓词不给缺省值。** 缺省成 `joins_on` 等于把「没说」当成「物理边」——而
    「没说」和「物理边」是两件事，把前者当后者是最容易漏的那种漏法。
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
        predicate = payload.get("predicate")
        if not (table and ref and predicate):
            return None
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


def route_for(
    conn: sqlite3.Connection,
    version_id: int | None,
    kind: str,
    payload: dict[str, Any],
    *,
    evidence_form: str,
) -> str:
    """生产者的调用入口：它只说 kind / payload / evidence_form，其余全在这里判。

    不在白名单的直接返回，**不用查库**——省一次查询，也让「白名单先于位置」这条
    顺序在代码里看得见。
    """
    check_form(evidence_form)
    if not whitelisted(kind, evidence_form):
        return ROUTE_SIGN
    return decide_route(
        occupied=is_occupied(conn, version_id, target_of(kind, payload)),
        kind=kind,
        evidence_form=evidence_form,
    )
