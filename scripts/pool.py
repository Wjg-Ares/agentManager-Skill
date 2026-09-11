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

# Windows 控制台默认是 GBK（cp936），输出里的 ✓ ✗ ⚠ 会直接抛 UnicodeEncodeError ——
# 命令其实已经执行成功了，只是结果打不出来，看着像失败。
# Claude Code 按 UTF-8 读子进程输出，所以这里统一强制 UTF-8，不依赖用户去设
# PYTHONIOENCODING。errors="replace" 保证最坏情况也只是某个字符显示成 ?，不会崩。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):  # 非标准流（管道、被重定向）时忽略
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent))

from poolkit.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
