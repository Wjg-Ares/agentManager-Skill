"""禁用命令判定的 CLI 入口 —— 人工排查用。见 check_edit 的说明。"""

from __future__ import annotations

import argparse

from .. import guard
from ..render import Result

NAME = "check-bash"
HELP = "查某条 Bash 命令本会话能不能跑（hook 用的同一套判定）"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("command", help="完整命令串")
    parser.add_argument("--session-id", help="缺省取本会话")


def run(ctx, args) -> Result:
    conn = ctx.connect()
    decision = guard.check_bash(
        conn,
        session_id=args.session_id or ctx.session_id,
        command=args.command,
    )
    text = "✓ 可以执行" if decision.allowed else f"✗ 禁止执行\n{decision.reason}"
    return Result(
        text=text,
        data={"command": args.command, **decision.to_dict()},
        exit_code=0 if decision.allowed else 5,
    )
