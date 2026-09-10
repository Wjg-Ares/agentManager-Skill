"""用户不满意：打回给**同一个** worker。

slot 不放、文件声明不释放 —— 追问的上下文只在原 worker 那里，换人前功尽弃（§5.3）。
"""

from __future__ import annotations

import argparse

from .. import ledger, registry
from ..render import Result, join, next_steps, section

NAME = "reject"
HELP = "用户不满意：把任务打回给原 worker（slot 与声明都不释放）"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("task_id", type=int)
    parser.add_argument("reason", help="用户的追问或不满意的点，会原样转给 worker")


def run(ctx, args) -> Result:
    conn = ctx.connect()
    task = ledger.reject(conn, task_id=args.task_id, reason=args.reason)
    address = registry.address_of(conn, task.assignee or "") or task.assignee

    head = f"✓ #{task.id} 已打回给 {task.assignee}（slot 和文件声明都保持不动）"
    body = section(
        f"把下面这段发给 `{address}`：",
        [
            f"  任务 #{task.id} 被打回，用户的意见：",
            f"  {args.reason}",
            "",
            f"  改完重新交付： python pool.py deliver {task.id} --changed \"...\" ...",
        ],
    )
    steps = next_steps(
        [
            f"用跨会话消息发给 `{address}`",
            "别换人 —— 追问的上下文只在它那里",
        ]
    )
    return Result(
        text=join(head, body, steps),
        data={
            "task_id": task.id,
            "worker": task.assignee,
            "address": address,
            "reason": args.reason,
        },
    )
