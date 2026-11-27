"""命令行入口。

    python -m ontology_builder init      onto.db
    python -m ontology_builder import    onto.db fixtures/ontology.v2.json
    python -m ontology_builder show      onto.db
    python -m ontology_builder export    onto.db out.json [--version 1]
    python -m ontology_builder candidate onto.db --kind probe --payload '{"a":1}'
    python -m ontology_builder probe onto.db --schema ddl.sql --data data.db
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

from . import store


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _open(args) -> "store.sqlite3.Connection":
    path = Path(args.db)
    fresh = not path.exists()
    conn = store.connect(path)
    stale = store.init(conn)
    if fresh:
        print(f"新建库 {path}（谓词词表 {len(store.SEED_PREDICATES)} 个）")
    if stale:
        print(
            "注意：这些谓词已从词表删掉，但历史版本还有边在用，没有删："
            + "、".join(stale)
            + "。要清就得先改那些边"
        )
    return conn


def cmd_init(args) -> int:
    _open(args).close()
    return 0


def cmd_import(args) -> int:
    conn = _open(args)
    state = store.state_from_json(args.json)
    vid, created = store.commit(conn, state, note=f"import {Path(args.json).name}", at=_now())
    n_obj = len(state["objects"])
    n_edge = len(state["edges"])
    tail = "新版本" if created else "内容与当前版本相同，未产生新版本"
    print(f"version {vid} · {n_obj} 个对象 · {n_edge} 条边 · {tail}")
    return 0


def cmd_show(args) -> int:
    conn = _open(args)
    vid = store.current_version(conn)
    if vid is None:
        print("库里还没有任何版本")
        return 1
    state = store.load(conn, vid)
    rows = conn.execute(
        "SELECT id, published_at, note, is_current FROM version ORDER BY id"
    ).fetchall()
    print("版本：")
    for r in rows:
        mark = "*" if r["is_current"] else " "
        print(f"  {mark} v{r['id']}  {r['published_at'] or '(未记时间)'}  {r['note']}")
    print()
    print(f"当前 v{vid}：{len(state['objects'])} 个对象 · {len(state['edges'])} 条边")
    for (otype, name) in sorted(state["objects"]):
        fields = state["objects"][(otype, name)]["fields"]
        signed = [k for k in fields if fields[k]["by"]]
        print(f"  {otype:<16} {name:<28} {len(fields)} 字段，其中 {len(signed)} 个已确认")
    if state["edges"]:
        print("边：")
        for (s, p, d) in state["edges"]:
            print(f"  {s} -[{p}]-> {d}")
    n_cand = conn.execute("SELECT count(*) AS c FROM candidate").fetchone()["c"]
    print(f"\n候选表 {n_cand} 条（候选进不了本体，只有 commit 能写）")
    return 0


def cmd_export(args) -> int:
    conn = _open(args)
    path = store.export(conn, args.out, args.version)
    print(f"已导出 {path}（{path.stat().st_size} 字节）")
    return 0


def cmd_candidate(args) -> int:
    conn = _open(args)
    payload = json.loads(args.payload)
    evidence = json.loads(args.evidence) if args.evidence else None
    cid = store.add_candidate(
        conn, args.kind, payload, evidence=evidence, at=_now(), route=args.route
    )
    route = conn.execute("SELECT route FROM candidate WHERE id = ?", (cid,)).fetchone()["route"]
    print(f"候选 #{cid} 已记（kind={args.kind}，route={route}）。它不会进本体，除非走 commit")
    return 0


def cmd_probe(args) -> int:
    from . import probe

    conn = _open(args)
    schema = probe.parse_ddl(Path(args.schema).read_text(encoding="utf-8"))
    if args.foreign_keys:
        # 只有「别人已经声明过外键」的库才传这个。声明式的走 exempt，
        # 推断式的走 sign —— 同一个库里两种都能有，route 分辨它们。
        probe.attach_foreign_keys(
            schema, probe.parse_foreign_keys(Path(args.foreign_keys).read_text(encoding="utf-8"))
        )
    profile = probe.profile_sqlite(args.data, schema)
    aliases = {}
    for item in args.alias or []:
        k, sep, v = item.partition("=")
        if not sep:
            print(f"错误：--alias 要写成 缩写=全称，实际是 {item!r}", file=sys.stderr)
            return 2
        aliases[k] = v

    cands = probe.infer(
        schema,
        profile,
        require_name_match=not args.no_name,
        name_aliases=aliases or None,
    )
    route_list = store.add_candidates(conn, cands, at=_now())

    counts = Counter(c.kind for c in cands)
    routes = Counter(route_list)
    print(f"probe：{len(schema)} 张表 · {len(profile.tables)} 张有数据 · {len(route_list)} 条候选")
    print(f"  route：exempt {routes.get('exempt', 0)} 条（§4.3 三问全过，人只复核）"
          f" / sign {routes.get('sign', 0)} 条（至少要人签一次）")
    for kind in ("object", "grain", "foreign_key"):
        if counts.get(kind):
            signed = sum(1 for c, r in zip(cands, route_list) if c.kind == kind and r == "sign")
            print(f"  {kind:<12} {counts[kind]:>4} 条，其中 {signed} 条要签")

    fks = [(c, r) for c, r in zip(cands, route_list) if c.kind == "foreign_key"]
    if fks:
        print("\n外键候选（前 10 条，按包含率与目标表）：")
        for c, r in fks[:10]:
            p, e = c.payload, c.evidence
            mark = "声明" if c.declared else "推断"
            print(
                f"  {p['table']}.{p['column']} -> {p['ref_table']}.{p['ref_column']}  "
                f"[{mark}/{r}] 包含率 {e.get('containment')} "
                f"目标唯一 {e.get('dst_unique')}"
            )
    if args.report:
        Path(args.report).write_text(
            json.dumps([c.as_row() for c in cands], ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"\n候选明细已写到 {args.report}")
    print("\n候选都在 candidate 表，本体没动——改本体只能走 commit。")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ontology-builder", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    for name, fn, help_text in (
        ("init", cmd_init, "建库"),
        ("show", cmd_show, "看版本与当前内容"),
    ):
        sp = sub.add_parser(name, help=help_text)
        sp.add_argument("db")
        sp.set_defaults(func=fn)

    sp = sub.add_parser("import", help="从 v2 本体 json 签发一个版本")
    sp.add_argument("db")
    sp.add_argument("json")
    sp.set_defaults(func=cmd_import)

    sp = sub.add_parser("export", help="把某个版本导出成 json")
    sp.add_argument("db")
    sp.add_argument("out")
    sp.add_argument("--version", type=int, default=None)
    sp.set_defaults(func=cmd_export)

    sp = sub.add_parser("candidate", help="记一条候选（写不进本体）")
    sp.add_argument("db")
    sp.add_argument("--kind", required=True, help="probe / mine")
    sp.add_argument("--payload", required=True, help="候选内容的 json")
    sp.add_argument("--evidence", default=None, help="证据 json；declared=true 表示声明式证据")
    sp.add_argument(
        "--route",
        default=None,
        choices=("exempt", "sign"),
        help="不传就按 §4.3 三问自动判（要查库）",
    )
    sp.set_defaults(func=cmd_candidate)

    sp = sub.add_parser("probe", help="schema + 数据 → 表/列/外键/grain 候选（只写 candidate 表）")
    sp.add_argument("db")
    sp.add_argument("--schema", required=True, help="建表语句 .sql")
    sp.add_argument("--data", required=True, help="真数据的 sqlite 库（只读打开）")
    sp.add_argument("--foreign-keys", default=None, help="可选：已声明外键的 .sql（走 exempt）")
    sp.add_argument("--alias", action="append", help="命名缩写表，写成 addr=address，可重复给")
    sp.add_argument("--no-name", action="store_true", help="关掉名字判据（实验用，precision 会掉到 6%%）")
    sp.add_argument("--report", default=None, help="把候选明细写成 json")
    sp.set_defaults(func=cmd_probe)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except store.StoreError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
