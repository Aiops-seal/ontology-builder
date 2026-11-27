"""probe：宽表 schema + 数据 → 表 / 列 / 外键 / grain 候选。

三条判据住在 infer.py，**只有前两条是必要条件**（包含率、目标唯一），
第三条（名字同源）只用来收窄。所以 probe 出来的外键永远走签，不走免批——
详见 SPEC §4.3 与 §4.6。

    from ontology_builder.probe import parse_ddl, profile_sqlite, infer
    schema = parse_ddl(Path("fixtures/schema/tpcds.sql").read_text())
    prof = profile_sqlite("tpcds.db", schema)
    for c in infer(schema, prof):
        ...
"""

from .infer import Candidate, infer
from .profile import ColumnProfile, Profile, TableProfile, profile_sqlite
from .schema import (
    Column,
    ForeignKey,
    Schema,
    Table,
    attach_foreign_keys,
    parse_ddl,
    parse_foreign_keys,
)

__all__ = [
    "Candidate",
    "Column",
    "ColumnProfile",
    "ForeignKey",
    "Profile",
    "Schema",
    "Table",
    "TableProfile",
    "attach_foreign_keys",
    "infer",
    "parse_ddl",
    "parse_foreign_keys",
    "profile_sqlite",
]
