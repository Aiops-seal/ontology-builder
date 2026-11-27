"""把 TPC-DS 的两份 DDL 放到 `fixtures/schema/` 下。

**这个脚本不替你下载。** TPC 对这份规范保留版权，下载要先接受他们的许可，
所以仓库里不放原文（本仓库是 MIT），脚本只做两件事：告诉你从哪拿、帮你从
本地已经有的地方复制过来。

```bash
python -m bench.fetch_tpcds                        # 看现在缺什么、去哪拿
python -m bench.fetch_tpcds --from ~/tpcds-kit/tools   # 从本地目录复制
```

要两份文件：

- `tpcds.sql`     —— 25 张表的建表语句（probe 的输入）
- `tpcds_ri.sql`  —— 102 条参照完整性约束（**答案**，不喂给 probe）

拿法有两条，任选：

1. 从 TPC 官网下：https://www.tpc.org/tpcds/ → 最新规范的 tools 包，
   解压后 `tools/tpcds.sql` 与 `tools/tpcds_ri.sql`。
2. 你本地已经有 tpcds-kit（很多人为了别的事 clone 过），用 `--from` 指过去。

拷进来之后 `python -m pytest` 会从 66 passed / 3 skipped 变成 69 passed，
`python -m bench.measure` 也能跑了。
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEST = HERE.parent / "fixtures" / "schema"

FILES = ("tpcds.sql", "tpcds_ri.sql")

_GUIDE = """\
缺这两份文件（TPC 保留版权，本仓库不放原文）：

  fixtures/schema/tpcds.sql      25 张表建表语句，probe 的输入
  fixtures/schema/tpcds_ri.sql   102 条参照完整性约束，**答案**，不喂给 probe

两条路，任选：

  1. 从官网下  https://www.tpc.org/tpcds/  → 最新规范的 tools 包
     解压后把 tools/tpcds.sql 和 tools/tpcds_ri.sql 拷到 fixtures/schema/

  2. 本地已经有了（比如 clone 过 tpcds-kit），一条命令搞定：
     python -m bench.fetch_tpcds --from /path/to/tpcds-kit/tools

为什么不在仓库里：这两份是 TPC 的规范文本，用之前要接受他们的许可，
而本仓库是 MIT。放一起是两种许可打架，不是"注明来源"能解决的。
"""


def _looks_right(path: Path) -> bool:
    """粗验：TPC-DS 的 DDL 里有 Legal Notice，也有 store_sales 这张表。"""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return "Legal Notice" in text and "store_sales" in text


def status() -> dict[str, bool]:
    return {name: (DEST / name).exists() for name in FILES}


def copy_from(src: Path) -> int:
    """从 `src` 复制两份文件过去，返回实际复制了几个。"""
    if not src.is_dir():
        print(f"不是目录：{src}", file=sys.stderr)
        return 0
    DEST.mkdir(parents=True, exist_ok=True)
    copied = 0
    for name in FILES:
        candidate = src / name
        if not candidate.exists():
            print(f"  跳过 {name}（{src} 里没有）")
            continue
        if not _looks_right(candidate):
            print(f"  跳过 {name}（不像 TPC-DS 的 DDL：缺 Legal Notice 或 store_sales）")
            continue
        shutil.copyfile(candidate, DEST / name)
        print(f"  复制 {name} → {DEST / name}")
        copied += 1
    return copied


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m bench.fetch_tpcds",
        description="把 TPC-DS 的两份 DDL 放到 fixtures/schema/（不下载，只复制 + 指路）",
    )
    ap.add_argument("--from", dest="src", type=Path, help="本地 tpcds-kit/tools 目录")
    args = ap.parse_args(argv)

    if args.src:
        copied = copy_from(args.src)
        if copied == 0:
            print("\n一个都没复制成。看下面：\n")
            print(_GUIDE)
            return 1

    state = status()
    missing = [name for name, ok in state.items() if not ok]
    for name, ok in state.items():
        print(f"  {'✓' if ok else '✗'} {DEST / name}")

    if missing:
        if not args.src:
            print()
            print(_GUIDE)
        return 1

    print("\n齐了。`python -m pytest -q` 应该是 69 passed。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
