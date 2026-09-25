"""测试包：把 harness 根目录加入 sys.path，使 `core` / `llm` / `tasks` 可被导入。

测试命令（cwd = harness/）：
```
py -3.12 -m unittest discover -s tests -v
```
"""

from __future__ import annotations

import os
import sys

HARNESS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HARNESS_DIR not in sys.path:
    sys.path.insert(0, HARNESS_DIR)
