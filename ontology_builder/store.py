"""SQLite 存储层：本体是行，json 是导出物。

三条设计决定，改之前先读 SPEC：

1. **每次签发是一份完整快照**，不是增量。数据量在万级，全量复制比
   bitemporal 便宜得多，而且「propose 跑两遍输出一样」天然成立。
2. **候选与本体物理分表**。probe / mine 手里只有 candidate 表的写权限，
   改本体只能走 commit —— 这不是流程约定，是表结构。
3. **字段级确认**。一行一个字段，确认标记挂在字段上，不挂在对象上。
   整条打标会把机器猜的东西洗成人签的。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from .route import route_for

ONTOLOGY_VERSION = "2.0"

# 谓词受控词表。自由字符串等于没约束，词表一散，边的语义就散了。
# 加词要改这里并写进 SPEC，不能靠调用方随手写一个新字符串。
# 受控词表。每条必须能说出两件事：**两端允许什么类型**、**什么时候允许用**（守卫）。
#
# 说得出守卫的谓词才有公理；说不出的是结构边。结构边也不是自动免批——免不免看
# SPEC §4.3 的白名单，跟守卫在不在数据里是两件事。
#
# `src` / `dst` 为空列表表示不限类型——只给策略层这种还没建完的用。
# 别的谓词都要写死两端：不写死，`event -[derived_from]-> metric` 这种混用会静默成立
# （v0.2 就是这么让一个词撑出三种语义的）。
SEED_PREDICATES: dict[str, dict[str, Any]] = {
    "joins_on": {
        "layer": "structural",
        "src": ["table_object"],
        "dst": ["table_object"],
        "note": "物理 join 路径。守卫全是数据能验的（类型相容、包含率），没有公理",
    },
    "belongs_to": {
        "layer": "structural",
        "src": ["table_object", "dimension"],
        "dst": ["business_object"],
        "note": "归属，多对一。基数由数据验",
    },
    "computed_from": {
        "layer": "semantic",
        "src": ["metric", "dimension"],
        "dst": ["table_object"],
        "note": "basic 指标的取数来源。守卫：目标必须是表对象——指向另一个指标是 inputs 的事",
    },
    "inputs": {
        "layer": "semantic",
        "src": ["metric", "dimension"],
        "dst": ["metric", "dimension"],
        "note": "derived 指标的输入。守卫：不成环、分子分母同口径、源必须已确认",
    },
    "restricted_to": {
        "layer": "semantic",
        "src": ["metric"],
        "dst": ["dimension"],
        "note": "限定，指标算在某个维度圈定的范围上。守卫：范围维度必须已确认",
    },
    "same_as": {
        "layer": "semantic",
        "src": ["term"],
        "dst": ["dimension", "metric"],
        "note": "跨体系对齐：业务口的术语和数仓的维度/指标是同一个东西。同体系别名走 aliases 字段，不建边",
    },
    "evidenced_by": {
        "layer": "strategic",
        "src": ["event"],
        "dst": ["metric"],
        "note": "事件的判定依据。守卫：阈值必须写成可评估的条件",
    },
    "based_on": {
        "layer": "strategic",
        "src": [],
        "dst": [],
        "note": "策略对象引用实体或事件。7S 没建完，两端暂不限制",
    },
}

METRIC_KINDS = ("basic", "derived")
DIMENSION_KINDS = ("basic", "derived")

_DDL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS predicate (
    name      TEXT PRIMARY KEY,
    layer     TEXT NOT NULL DEFAULT 'semantic',   -- structural / semantic / strategic
    src_types TEXT NOT NULL DEFAULT '[]',          -- json 数组，空 = 不限
    dst_types TEXT NOT NULL DEFAULT '[]',
    note      TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS version (
    id           INTEGER PRIMARY KEY,
    domain       TEXT NOT NULL DEFAULT '',
    published_at TEXT NOT NULL,
    note         TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL,
    is_current   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS object (
    version_id INTEGER NOT NULL,
    id         TEXT NOT NULL,          -- "type:name"
    type       TEXT NOT NULL,
    name       TEXT NOT NULL,
    PRIMARY KEY (version_id, id),
    FOREIGN KEY (version_id) REFERENCES version(id)
);

CREATE TABLE IF NOT EXISTS field (
    version_id   INTEGER NOT NULL,
    object_id    TEXT NOT NULL,
    name         TEXT NOT NULL,
    value_json   TEXT NOT NULL,
    confirmed_by TEXT,
    confirmed_at TEXT,
    source_json  TEXT,
    PRIMARY KEY (version_id, object_id, name),
    FOREIGN KEY (version_id) REFERENCES version(id)
);

CREATE TABLE IF NOT EXISTS edge (
    version_id INTEGER NOT NULL,
    src        TEXT NOT NULL,
    predicate  TEXT NOT NULL,
    dst        TEXT NOT NULL,
    PRIMARY KEY (version_id, src, predicate, dst),
    FOREIGN KEY (version_id) REFERENCES version(id),
    FOREIGN KEY (predicate) REFERENCES predicate(name)
);

-- 候选：probe / mine 直接写这张表，写不进 object / field / edge。
CREATE TABLE IF NOT EXISTS candidate (
    id           INTEGER PRIMARY KEY,
    kind         TEXT NOT NULL,        -- object / grain / foreign_key
    route        TEXT NOT NULL DEFAULT 'sign',  -- exempt / sign，见 SPEC §4.3
    evidence_form TEXT NOT NULL DEFAULT '',     -- ddl_* / data_*，决定能不能免
    payload_json TEXT NOT NULL,
    evidence_json TEXT,
    proposed_at  TEXT NOT NULL,
    state        TEXT NOT NULL DEFAULT 'pending'
);
"""


class StoreError(Exception):
    """存储层的不合法操作。"""


# ---------------------------------------------------------------- 连接与建库


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init(conn: sqlite3.Connection) -> list[str]:
    """建库 + 同步词表。返回**留着但已从词表里删掉**的谓词名。

    那些词还有边在用，删了会破坏历史版本，所以不删——但要让调用方知道，
    否则「词表收窄了」这件事只在代码里发生，库里看不出来。
    """
    conn.executescript(_DDL)
    # 老库补列。`CREATE TABLE IF NOT EXISTS` 不会给已存在的表加字段，
    # 少了这一句，拿旧库跑 probe 会在 INSERT 时报「no column named route」——
    # 报错还算好的，最怕的是有人把 route 塞进 payload 绕过它。
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(candidate)")}
    if "route" not in cols:
        conn.execute("ALTER TABLE candidate ADD COLUMN route TEXT NOT NULL DEFAULT 'sign'")
    if "evidence_form" not in cols:
        conn.execute("ALTER TABLE candidate ADD COLUMN evidence_form TEXT NOT NULL DEFAULT ''")
    pcols = {r["name"] for r in conn.execute("PRAGMA table_info(predicate)")}
    for col, decl in (
        ("layer", "TEXT NOT NULL DEFAULT 'semantic'"),
        ("src_types", "TEXT NOT NULL DEFAULT '[]'"),
        ("dst_types", "TEXT NOT NULL DEFAULT '[]'"),
    ):
        if col not in pcols:
            conn.execute(f"ALTER TABLE predicate ADD COLUMN {col} {decl}")

    for name, spec in SEED_PREDICATES.items():
        # REPLACE 而不是 IGNORE：词表在代码里，库里那份是副本。
        # 不覆盖就会留下「代码改了、库没改」的两份真相。
        conn.execute(
            "INSERT OR REPLACE INTO predicate (name, layer, src_types, dst_types, note) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                name,
                spec["layer"],
                json.dumps(spec["src"]),
                json.dumps(spec["dst"]),
                spec["note"],
            ),
        )

    stale: list[str] = []
    for r in conn.execute("SELECT name FROM predicate").fetchall():
        name = r["name"]
        if name in SEED_PREDICATES:
            continue
        used = conn.execute(
            "SELECT COUNT(*) AS c FROM edge WHERE predicate = ?", (name,)
        ).fetchone()["c"]
        if used:
            stale.append(name)
        else:
            conn.execute("DELETE FROM predicate WHERE name = ?", (name,))
    conn.commit()
    return stale


# ---------------------------------------------------------------- state 表示
#
# state = {
#   "domain": str,
#   "objects": {(type, name): {"fields": {name: {"value", "by", "at", "source"}}}},
#   "edges": [(src, predicate, dst), ...],
# }


def empty_state(domain: str = "") -> dict[str, Any]:
    return {"domain": domain, "objects": {}, "edges": []}


def state_from_json(path: str | Path) -> dict[str, Any]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return state_from_dict(raw)


def state_from_dict(raw: dict[str, Any]) -> dict[str, Any]:
    version = raw.get("version")
    if version != ONTOLOGY_VERSION:
        raise StoreError(
            f"本体 version 必须是 {ONTOLOGY_VERSION!r}，实际是 {version!r}；"
            "不做向后兼容——兼容层会掩盖两边看到的本体不一样这件事"
        )

    state = empty_state(raw.get("domain", ""))
    for item in raw.get("objects", []):
        key = (item["type"], item["name"])
        fields: dict[str, dict[str, Any]] = {}
        confirmed = item.get("confirmed") or {}
        for fname, fvalue in (item.get("fields") or {}).items():
            mark = confirmed.get(fname) or {}
            fields[fname] = {
                "value": fvalue,
                "by": mark.get("by"),
                "at": mark.get("at"),
                "source": mark.get("source"),
            }
        state["objects"][key] = {"fields": fields}

    for e in raw.get("edges", []):
        state["edges"].append((e["src"], e["predicate"], e["dst"]))

    state["edges"].sort()
    _validate_kinds(state)
    _validate_derived(state)
    return state


# ------------------------------------------------- 语义校验：kind / inputs / 环


def _field_value(fields: dict[str, Any], name: str) -> Any:
    body = fields.get(name)
    return None if body is None else body["value"]


def _metric_inputs(fields: dict[str, Any]) -> list[str]:
    raw = _field_value(fields, "inputs")
    return list(raw or [])


def _validate_kinds(state: dict[str, Any]) -> None:
    """metric 必须有 kind 且只能是 basic / derived；basic 不许有 inputs。

    basic 与 derived 的区别不是风格问题：basic 的定义由表结构决定、数据能验，
    derived 的口径由人定、数据验不了。这个字段决定一条改动走免批还是走签，
    所以不能省，也不能两头都填。
    """
    for (otype, name), body in state["objects"].items():
        if otype not in ("metric", "dimension"):
            continue
        fields = body["fields"]
        kind = _field_value(fields, "kind")

        if otype == "metric":
            if kind is None:
                raise StoreError(
                    f"metric:{name} 缺 kind。每个指标必须声明是 basic（单列聚合，"
                    "数据能验）还是 derived（口径由人定）"
                )
            if kind not in METRIC_KINDS:
                raise StoreError(
                    f"metric:{name} 的 kind={kind!r} 不合法，只能是 {METRIC_KINDS}"
                )
            if kind == "basic" and _metric_inputs(fields):
                raise StoreError(
                    f"metric:{name} 是 basic，不该有 inputs —— basic 直接聚合表列，"
                    "引用别的指标就说明它是 derived"
                )
            if kind == "derived" and not _metric_inputs(fields):
                raise StoreError(
                    f"metric:{name} 是 derived，但 inputs 是空的。"
                    "derived 必须写清它由哪几个 metric 算出——不然口径无法复现"
                )
        elif kind is not None and kind not in DIMENSION_KINDS:
            raise StoreError(
                f"dimension:{name} 的 kind={kind!r} 不合法，只能是 {DIMENSION_KINDS}"
            )


def _validate_derived(state: dict[str, Any]) -> None:
    """derived 的 inputs 必须指向本版本内存在的 metric，且不能成环。

    两条都是「导入时就拒」，不等到算的时候才炸。
    """
    graph: dict[str, list[str]] = {}
    for (otype, name), body in state["objects"].items():
        if otype != "metric":
            continue
        for ref in _metric_inputs(body["fields"]):
            rtype, sep, rname = ref.partition(":")
            if sep != ":" or rtype != "metric":
                raise StoreError(
                    f"metric:{name} 的 inputs 里有 {ref!r}，必须写成 'metric:名字'。"
                    "derived 只能引用指标，不能直接引用表列——那等于偷偷绕过口径"
                )
            if (rtype, rname) not in state["objects"]:
                raise StoreError(
                    f"metric:{name} 引用了不存在的 {ref!r}。"
                    "未知输入等于错误，不静默忽略"
                )
        graph[name] = [r.partition(":")[2] for r in _metric_inputs(body["fields"])]

    WHITE, GRAY, BLACK = 0, 1, 2
    color = {n: WHITE for n in graph}

    def visit(node: str, path: list[str]) -> None:
        color[node] = GRAY
        for nxt in graph.get(node, []):
            if color.get(nxt) == GRAY:
                chain = " → ".join(path + [node, nxt])
                raise StoreError(f"派生指标成环：{chain}")
            if color.get(nxt, WHITE) == WHITE:
                visit(nxt, path + [node])
        color[node] = BLACK

    for node in graph:
        if color[node] == WHITE:
            visit(node, [])


def state_to_dict(state: dict[str, Any]) -> dict[str, Any]:
    """规范化排序，保证同一份 state 导出的字节完全一致。"""
    objects = []
    for (otype, name) in sorted(state["objects"]):
        fields = state["objects"][(otype, name)]["fields"]
        plain = {k: fields[k]["value"] for k in sorted(fields)}
        confirmed = {
            k: {kk: vv for kk, vv in (
                ("by", fields[k]["by"]),
                ("at", fields[k]["at"]),
                ("source", fields[k]["source"]),
            ) if vv is not None}
            for k in sorted(fields)
            if fields[k]["by"] is not None
        }
        item: dict[str, Any] = {"type": otype, "name": name, "fields": plain}
        if confirmed:
            item["confirmed"] = confirmed
        objects.append(item)

    return {
        "version": ONTOLOGY_VERSION,
        "domain": state.get("domain", ""),
        "objects": objects,
        "edges": [
            {"src": s, "predicate": p, "dst": d}
            for (s, p, d) in sorted(state["edges"])
        ],
    }


def state_hash(state: dict[str, Any]) -> str:
    canonical = json.dumps(
        state_to_dict(state), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 读写版本


def current_version(conn: sqlite3.Connection) -> int | None:
    row = conn.execute(
        "SELECT id FROM version WHERE is_current = 1 ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return int(row["id"]) if row else None


def load(conn: sqlite3.Connection, version_id: int | None = None) -> dict[str, Any] | None:
    vid = version_id if version_id is not None else current_version(conn)
    if vid is None:
        return None
    if conn.execute("SELECT 1 FROM version WHERE id = ?", (vid,)).fetchone() is None:
        raise StoreError(f"版本 {vid} 不存在")

    state = empty_state(
        conn.execute("SELECT domain FROM version WHERE id = ?", (vid,)).fetchone()["domain"]
    )

    objects: dict[tuple[str, str], dict[str, Any]] = {}
    for r in conn.execute(
        "SELECT id, type, name FROM object WHERE version_id = ? ORDER BY id", (vid,)
    ):
        objects[(r["type"], r["name"])] = {"fields": {}}
    for r in conn.execute(
        "SELECT object_id, name, value_json, confirmed_by, confirmed_at, source_json "
        "FROM field WHERE version_id = ? ORDER BY object_id, name",
        (vid,),
    ):
        otype, _, oname = r["object_id"].partition(":")
        objects[(otype, oname)]["fields"][r["name"]] = {
            "value": json.loads(r["value_json"]),
            "by": r["confirmed_by"],
            "at": r["confirmed_at"],
            "source": json.loads(r["source_json"]) if r["source_json"] else None,
        }

    edges = [
        (r["src"], r["predicate"], r["dst"])
        for r in conn.execute(
            "SELECT src, predicate, dst FROM edge WHERE version_id = ?", (vid,)
        )
    ]

    state["objects"] = objects
    state["edges"] = sorted(edges)
    return state


def _validate_edges(conn: sqlite3.Connection, state: dict[str, Any]) -> None:
    """谓词在词表里、两端类型在允许范围里、端点真的存在。

    v0.2 只查了第一条。于是 `event -[derived_from]-> metric` 一路走通——
    它两端从来没被问过。词表里写了「两端是什么」，没人读，那句就是注释。
    """
    preds = {r["name"]: r for r in conn.execute("SELECT * FROM predicate")}
    types = {f"{t}:{n}": t for (t, n) in state["objects"]}
    for src, pred, dst in state["edges"]:
        spec = preds.get(pred)
        if spec is None:
            raise StoreError(
                f"谓词 {pred!r} 不在受控词表里。加词要改 SEED_PREDICATES 并写进 SPEC，"
                "不能靠调用方随手写一个新字符串"
            )
        for end, oid, key in (("src", src, "src_types"), ("dst", dst, "dst_types")):
            allowed = json.loads(spec[key])
            if not allowed:
                continue
            otype = types.get(oid)
            if otype is None:
                raise StoreError(
                    f"{src} -[{pred}]-> {dst} 的 {end} 端 {oid!r} 不在本体里；"
                    "悬空边在图上走不通，也不能被审"
                )
            if otype not in allowed:
                raise StoreError(
                    f"{src} -[{pred}]-> {dst}：{end} 端是 {otype}，但 {pred} 只允许 {allowed}。"
                    "要么换谓词，要么这本来就不是同一种关系"
                )


def commit(
    conn: sqlite3.Connection, state: dict[str, Any], note: str = "", at: str = ""
) -> tuple[int, bool]:
    """签发一份新版本。返回 (version_id, 是否真的新建)。

    幂等：内容哈希与当前版本相同就不产生新版本——重试多少次，版本号只递增一次。
    """
    # 校验必须在任何写入之前。放进事务中间，校验失败时 version 行已经躺在那儿了，
    # 调用方一 commit 就落下一个没有边的版本。
    _validate_edges(conn, state)

    digest = state_hash(state)
    cur = current_version(conn)
    if cur is not None:
        row = conn.execute("SELECT content_hash FROM version WHERE id = ?", (cur,)).fetchone()
        if row["content_hash"] == digest:
            return cur, False

    new_id = (cur or 0) + 1
    conn.execute("UPDATE version SET is_current = 0 WHERE is_current = 1")
    conn.execute(
        "INSERT INTO version (id, domain, published_at, note, content_hash, is_current) "
        "VALUES (?, ?, ?, ?, ?, 1)",
        (new_id, state.get("domain", ""), at or "", note, digest),
    )

    for (otype, name), body in state["objects"].items():
        oid = f"{otype}:{name}"
        conn.execute(
            "INSERT INTO object (version_id, id, type, name) VALUES (?, ?, ?, ?)",
            (new_id, oid, otype, name),
        )
        for fname, f in body["fields"].items():
            conn.execute(
                "INSERT INTO field (version_id, object_id, name, value_json, "
                "confirmed_by, confirmed_at, source_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    new_id,
                    oid,
                    fname,
                    json.dumps(f["value"], ensure_ascii=False, sort_keys=True),
                    f.get("by"),
                    f.get("at"),
                    json.dumps(f["source"], ensure_ascii=False, sort_keys=True)
                    if f.get("source")
                    else None,
                ),
            )

    for (src, pred, dst) in state["edges"]:
        conn.execute(
            "INSERT INTO edge (version_id, src, predicate, dst) VALUES (?, ?, ?, ?)",
            (new_id, src, pred, dst),
        )

    conn.commit()
    return new_id, True


def export(conn: sqlite3.Connection, out: str | Path, version_id: int | None = None) -> Path:
    state = load(conn, version_id)
    if state is None:
        raise StoreError("库里还没有任何版本")
    path = Path(out)
    path.write_text(
        json.dumps(state_to_dict(state), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------- 候选


def add_candidate(
    conn: sqlite3.Connection,
    kind: str,
    payload: dict[str, Any],
    evidence_form: str,
    *,
    evidence: dict[str, Any] | None = None,
    at: str = "",
    route: str | None = None,
) -> int:
    """手写一条候选。

    `route=None` 就按 SPEC §4.3 算：`(kind, evidence_form)` 在白名单里、位置又空，
    才 exempt。传了 `route` 就用传的——但只在「这条候选我知道得比白名单多」的
    时候才该传。

    `evidence_form` 必填：手写候选也得说清证据是什么形态。说不清就该去签，不是
    免批——这个参数没有默认值，就是不让它默认成「能免」。
    """
    if route is None:
        route = route_for(
            conn, current_version(conn), kind, payload, evidence_form=evidence_form
        )
    if route not in ("exempt", "sign"):
        raise StoreError(
            f"route 只能是 exempt / sign，实际是 {route!r}。谁来签是候选的一部分，不能猜"
        )
    cur = conn.execute(
        "INSERT INTO candidate (kind, route, evidence_form, payload_json, evidence_json, proposed_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            kind,
            route,
            evidence_form,
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            json.dumps(evidence, ensure_ascii=False, sort_keys=True) if evidence else None,
            at,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def add_candidates(conn: sqlite3.Connection, cands, at: str = "") -> list[str]:
    """一批候选。probe 的输出走这里——一条一条 add 会让「全进全不进」变成半截状态。

    `route` 在这一刻算，不在生产者里算：位置要查当前版本有没有已确认的值。
    所以同一批候选用在不同的库上跑，route 可能不同——**那是对的**，同一个改动
    第一次提是新增，第二次提就是改一个签过的东西。
    """
    vid = current_version(conn)
    rows = []
    for c in cands:
        route = route_for(conn, vid, c.kind, c.payload, evidence_form=c.evidence_form)
        rows.append(
            (
                c.kind,
                route,
                c.evidence_form,
                json.dumps(c.payload, ensure_ascii=False, sort_keys=True),
                json.dumps(c.evidence, ensure_ascii=False, sort_keys=True),
                at,
            )
        )
    conn.executemany(
        "INSERT INTO candidate (kind, route, evidence_form, payload_json, evidence_json, proposed_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return [r[1] for r in rows]


def candidates(conn: sqlite3.Connection, kind: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT id, kind, route, evidence_form, payload_json, evidence_json, proposed_at, state FROM candidate"
    args: tuple = ()
    if kind:
        sql += " WHERE kind = ?"
        args = (kind,)
    return [
        {
            "id": r["id"],
            "kind": r["kind"],
            "route": r["route"],
            "evidence_form": r["evidence_form"],
            "payload": json.loads(r["payload_json"]),
            "evidence": json.loads(r["evidence_json"]) if r["evidence_json"] else None,
            "proposed_at": r["proposed_at"],
            "state": r["state"],
        }
        for r in conn.execute(sql + " ORDER BY id", args)
    ]
