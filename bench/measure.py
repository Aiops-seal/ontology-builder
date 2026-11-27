"""量 probe：三组判据的召回与误报。

    python -m bench.measure

答案来自 TPC-DS 官方的 102 条参照完整性约束（`fixtures/schema/tpcds_ri.sql`），
**不喂给 probe**。数据是照着这些约束造的（`bench/tpcds.py`），所以：

- `recall` 能信：漏掉的那条，真在真库上也会漏，因为判据本身不认它。
- `precision` 只能看方向：造数据的时候我知道答案，脏值、历史遗留列这些都没有。
  具体百分比不外推，相对关系可以（SPEC §6.1）。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import bench.tpcds as T
from ontology_builder.probe import infer, profile_sqlite
from ontology_builder.route import decide_route

# 缩写表。**必须由人给**，库里不内置——它是某个组织自己的习惯。
# 这三条能把 33 条漏报全补回来，代价是 cdemo / hdemo 都规范成 demo，
# 两个人口统计维表互相串 22 条。见 README 那张表下面那段。
ALIASES = {"addr": "address", "cdemo": "demo", "hdemo": "demo"}

VARIANTS = [
    ("A 只数据判据（C1∧C2）", dict(require_name_match=False)),
    ("B ＋名字（C3）", dict(require_name_match=True)),
    ("C ＋名字＋缩写表", dict(require_name_match=True, name_aliases=ALIASES)),
]


def run(rows: dict[str, int] | None = None, seed: int = 7) -> list[tuple[str, dict]]:
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "tpcds.db"
        T.build(db, seed=seed, rows=rows)
        schema = T.load_schema()
        prof = profile_sqlite(db, schema)
        key = T.answer_key(schema)
        out = []
        for name, cfg in VARIANTS:
            cands = [c for c in infer(schema, prof, **cfg) if c.kind == "foreign_key"]
            out.append((name, T.score(cands, key)))
        return out


def main() -> int:
    schema = T.load_schema()
    print(f"TPC-DS {len(schema)} 张表，官方外键 {len(T.answer_key(schema))} 条（答案不喂给 probe）")
    print(f"迷你实例行数：store_sales {T.ROWS['store_sales']}，date_dim {T.ROWS['date_dim']}\n")
    print(f"{'判据组合':<22}{'候选':>6}{'命中':>6}{'漏':>5}{'recall':>8}{'precision':>11}")
    print("-" * 60)
    results = run()  # 造一次库，三组判据共用——造一次要 5 秒，不该跑三遍
    for name, r in results:
        print(
            f"{name:<22}{r['candidates']:>6}{r['hits']:>6}{len(r['missing']):>5}"
            f"{r['recall']:>8.3f}{r['precision']:>11.3f}"
        )

    b = results[1][1]
    c = results[2][1]
    print(f"\nB 漏掉的 {len(b['missing'])} 条，全是同一类：命名缩写。")
    for t, col, rt, rc in b["missing"][:3]:
        print(f"  {t}.{col} -> {rt}.{rc}")

    demo = {"customer_demographics", "household_demographics"}
    fps = c["false_positives"]
    cross = [fp for fp in fps if fp[2] in demo and fp[0] != fp[2]]
    others = [fp for fp in fps if fp not in cross]
    print(f"\nC 多的 {len(fps)} 条误报：{len(cross)} 条是 cdemo/hdemo 互串（缩写表把两个维表压成了同一个词），")
    print(f"剩 {len(others)} 条是别的原因，逐条列出——**这几条要人判，脚本不判**：")
    for t, col, rt, rc in others:
        print(f"  {t}.{col} -> {rt}.{rc}")
    print("\n其中 cr_order_number → cs_order_number、sr_ticket_number → ss_ticket_number、")
    print("wp_customer_sk → c_customer_sk 看着像真外键，而官方约束里没有。")
    print("没有动答案去认它们——这正好是 route=sign 的意思：这条要人看，不是脚本说了算。")

    # D 不是测量，是演示：**把答案当输入**，看「免批」那条规矩实际长什么样。
    # 有声明式外键的库就是这个形状：102 条走 exempt，只剩 11 条要人看。
    from ontology_builder.probe.schema import attach_foreign_keys

    declared = T.load_schema()
    attach_foreign_keys(declared, T.load_fks(declared))
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "tpcds.db"
        T.build(db)
        prof = profile_sqlite(db, declared)
        cands = [c for c in infer(declared, prof) if c.kind == "foreign_key"]
    # 空库（这些位置都还没有值）→ 只需要看第二问和第三问。
    exempt = sum(
        1
        for c in cands
        if decide_route(occupied=False, impact=c.impact, declared=c.declared) == "exempt"
    )
    print(
        f"\n[演示，不是测量] 假如这个库自己声明过外键：{len(cands)} 条候选里 "
        f"{exempt} 条走免批，{len(cands) - exempt} 条要人签。"
    )
    print("免批那条规矩的意义全在这里——声明越多，要签字的人越少。")
    print("注意这三问是「全过才免」：外键的 impact 是 local（joins_on 不参与语义推导），")
    print("所以声明式的能免；换成 inputs 这种语义边，声明过也照样要签。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
