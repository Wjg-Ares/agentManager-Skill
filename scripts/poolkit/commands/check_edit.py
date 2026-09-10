"""锁判定的 CLI 入口 —— 人工排查用。

hook 走的是 `hooks/pretooluse.py`，它直接 import guard，省掉一次进程启动。
两边都调同一个 :func:`poolkit.guard.check_edit`，判定逻辑只有一份。
"""

from __future__ import annotations

import argparse

from .. import guard
from ..render import Result

NAME = "check-edit"
HELP = "查某个文件当前能不能改（hook 用的同一套判定）"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("file", help="文件路径")
    parser.add_argument("--session-id", help="缺省取本会话")
    parser.add_argument(
        "--no-auto-claim",
        action="store_true",
        help="只判定，不自动补声明（排查时用这个，免得查一下就把锁占了）",
    )


def run(ctx, args) -> Result:
    conn = ctx.connect()
    decision = guard.check_edit(
        conn,
        session_id=args.session_id or ctx.session_id,
        file_path=args.file,
        auto_claim=not args.no_auto_claim,
    )
    if decision.allowed:
        text = "✓ 可以改" + (f"\n  {decision.note}" if decision.note else "")
    else:
        text = f"✗ 不能改\n{decision.reason}"
    return Result(
        text=text,
        data={"file": args.file, **decision.to_dict()},
        exit_code=0 if decision.allowed else 5,
    )
