#!/usr/bin/env python3
"""多 Agent 协作编排账本 —— CLI 入口。

薄入口：只管好 sys.path，其余全在 poolkit 里。

    python pool.py status
    python pool.py --json check-edit src/Foo.cs

对外的 slash 命令是 /am-setup、/am-status、/am-worker、/am-approve；
这里是它们底下的 CLI，也可以直接用。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from poolkit.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
