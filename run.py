#!/usr/bin/env python
"""便捷启动脚本：python run.py [serve|agents|ask|collab|...]

等价于 `python -m a2a_hub.cli <args>`，方便不装包直接跑。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from a2a_hub.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
