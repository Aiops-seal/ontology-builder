"""让 `python -m pytest` 之外的方式（比如 IDE 直接点某个测试）也能 import bench。

pytest 会把 conftest.py 所在目录放进 sys.path，所以这个文件在这里主要是占位。
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
