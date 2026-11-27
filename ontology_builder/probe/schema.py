"""解析建表语句：表、列、类型、not null、主键、外键。

**只认 DDL 明说的东西。** 这里一行推断都没有——这里判出来的都是「声明」，
按 SPEC §4.3 走免批；推断在 infer.py，那些走签。两个文件分开不是因为代码长，
是因为它们的错能靠谁抓不同：这里错一个正则，测试立刻红；那边错一条判据，
只有数据变了才会显形。

    parse_ddl(sql)            -> Schema（表、列、主键、DDL 里声明的外键）
    parse_foreign_keys(sql)   -> [ForeignKey]（只取 alter table ... foreign key）
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_CREATE = re.compile(r"create\s+table\s+(?:if\s+not\s+exists\s+)?(\w+)\s*\((.*)\)\s*$", re.I | re.S)
_ALTER_FK = re.compile(
    r"alter\s+table\s+(\w+)\s+add\s+(?:constraint\s+\w+\s+)?"
    r"foreign\s+key\s*\(([^)]*)\)\s*references\s+(\w+)\s*\(([^)]*)\)",
    re.I | re.S,
)
_PK = re.compile(r"primary\s+key\s*\(([^)]*)\)", re.I)
_NOT_NULL = re.compile(r"\bnot\s+null\b", re.I)


@dataclass(frozen=True)
class Column:
    name: str
    type: str
    not_null: bool = False

    @property
    def type_class(self) -> str:
        """粗分三类，只用来挡掉「数字列对字符列」这种必然不相交的比较。"""
        t = self.type.lower()
        if any(k in t for k in ("char", "varchar", "text", "string", "clob")):
            return "text"
        if "date" in t or "time" in t:
            return "date"
        return "num"


@dataclass(frozen=True)
class ForeignKey:
    table: str
    columns: tuple[str, ...]
    ref_table: str
    ref_columns: tuple[str, ...]
    name: str = ""

    @property
    def is_single(self) -> bool:
        return len(self.columns) == 1 and len(self.ref_columns) == 1


@dataclass
class Table:
    name: str
    columns: list[Column] = field(default_factory=list)
    pk: list[str] = field(default_factory=list)
    fks: list[ForeignKey] = field(default_factory=list)

    def column(self, name: str) -> Column | None:
        for c in self.columns:
            if c.name == name:
                return c
        return None

    @property
    def prefix(self) -> str:
        """列名前缀（表别名），截到**第一个**下划线。`store_sales` → `ss_`，`item` → `i_`。

        为什么截第一个而不是最后一个：`reason` 的三列是 `r_reason_sk` /
        `r_reason_id` / `r_reason_desc`，公共前缀一直延伸到 `r_reason_`。
        按最后一个下划线截，`r_reason_sk` 去掉前缀只剩 `sk`——`sk` 是
        `s_store_sk`、`i_item_sk` 的后缀，于是 `reason` 会跟全库每一列配上。
        这个 bug 不会报错，只会让 precision 从 0.63 掉到 0.63 以下且原因看不出来。

        命名不统一时它是空串，那时 C3 退化成比全名——**退化了不报错**，
        这是它只能当筛子、不能当判据的又一个理由。
        """
        names = [c.name for c in self.columns]
        if not names:
            return ""
        common = names[0]
        for n in names[1:]:
            i = 0
            while i < min(len(common), len(n)) and common[i] == n[i]:
                i += 1
            common = common[:i]
        cut = common.find("_")
        return common[: cut + 1] if cut > 0 else ""


@dataclass
class Schema:
    tables: dict[str, Table] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.tables)

    def __contains__(self, name: str) -> bool:
        return name in self.tables

    def __getitem__(self, name: str) -> Table:
        return self.tables[name]

    def names(self) -> list[str]:
        return sorted(self.tables)


def _strip_comments(sql: str) -> str:
    return re.sub(r"--[^\n]*", "", sql)


def _split_top_level(body: str) -> list[str]:
    """按逗号切列定义，但不切 `decimal(7,2)` 里的那个逗号。"""
    parts: list[str] = []
    depth = 0
    buf: list[str] = []
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if "".join(buf).strip():
        parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


def _names(group: str) -> list[str]:
    return [c.strip().strip('"`[]') for c in group.split(",") if c.strip()]


def _statements(sql: str) -> list[str]:
    """按分号切语句。

    必须切：`create table` 的 body 用贪婪匹配的话，一条正则会从第一张表一路
    吃到文件最后一个右括号，25 张表被解析成 1 张——而且不报错，因为那张
    巨型表「长得挺正常」。这是这个文件里唯一一个错得很安静的地方。
    """
    return [s.strip() for s in _strip_comments(sql).split(";") if s.strip()]


def parse_ddl(sql: str) -> Schema:
    schema = Schema()
    for stmt in _statements(sql):
        m = _CREATE.match(stmt)
        if not m:
            continue
        name, body = m.group(1), m.group(2)
        table = Table(name=name)
        for line in _split_top_level(body):
            low = line.lower()
            if low.startswith("primary key"):
                pk = _PK.search(line)
                if pk:
                    table.pk = _names(pk.group(1))
                continue
            if low.startswith("foreign key"):
                fk = re.search(
                    r"foreign\s+key\s*\(([^)]*)\)\s*references\s+(\w+)\s*\(([^)]*)\)",
                    line,
                    re.I,
                )
                if fk:
                    table.fks.append(
                        ForeignKey(
                            table=name,
                            columns=tuple(_names(fk.group(1))),
                            ref_table=fk.group(2),
                            ref_columns=tuple(_names(fk.group(3))),
                        )
                    )
                continue
            if low.startswith(("unique", "constraint", "check", "key ")):
                continue
            head = line.split(None, 1)
            if len(head) < 2:
                continue
            col_name, rest = head[0].strip('"`[]'), head[1]
            col_type = rest.split()[0] if rest.split() else ""
            table.columns.append(
                Column(name=col_name, type=col_type, not_null=bool(_NOT_NULL.search(rest)))
            )
        if not table.columns:
            raise ValueError(f"{name} 没解析出任何列，DDL 形状变了，先看这个文件")
        schema.tables[name] = table
    if not schema.tables:
        raise ValueError("没解析出任何 create table，检查 SQL 是不是建表语句")
    return schema


def parse_foreign_keys(sql: str) -> list[ForeignKey]:
    """`alter table ... add constraint ... foreign key ...` 那批。

    TPC-DS 的外键不在 `tpcds.sql` 里，在 `tpcds_ri.sql` 里。**那份文件是答案，
    不能喂给 probe**（SPEC §6.1）——这里的函数是给有声明式外键的库用的。
    """
    out: list[ForeignKey] = []
    for stmt in _statements(sql):
        for m in _ALTER_FK.finditer(stmt):
            out.append(
                ForeignKey(
                    table=m.group(1),
                    columns=tuple(_names(m.group(2))),
                    ref_table=m.group(3),
                    ref_columns=tuple(_names(m.group(4))),
                )
            )
    return out


def attach_foreign_keys(schema: Schema, fks: list[ForeignKey]) -> None:
    """把声明式外键挂到表上。**这是「声明」，不是推断**，挂上去的外键走免批。

    TPC-DS 的这份文件（`tpcds_ri.sql`）在实验里是**答案**，不要挂上去，
    否则 §6.1 的 recall 永远是 100%，量不出任何东西。
    """
    for fk in fks:
        table = schema.tables.get(fk.table)
        if table is None:
            raise ValueError(f"外键声明的表 {fk.table!r} 不在 schema 里，未知输入等于错误")
        if not any(
            f.columns == fk.columns and f.ref_table == fk.ref_table and f.ref_columns == fk.ref_columns
            for f in table.fks
        ):
            table.fks.append(fk)
