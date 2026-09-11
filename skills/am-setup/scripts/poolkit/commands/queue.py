"""队列查看与维护：调优先级、删待办、重排、解死信。

`approve` 输出里那四个选项对应的命令都在这条下面。
"""

from __future__ import annotations

import argparse

from .. import ledger
from ..errors import UsageError
from ..render import Result, join, next_steps, queue_block, section, task_dict

NAME = "queue"
HELP = "查看队列；--priority 调优先级、--cancel 删待办、--requeue 重排、--revive 解死信"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--priority",
        nargs=2,
        metavar=("任务ID", "优先级"),
        help="调整优先级，数字越小越先出队",
    )
    parser.add_argument("--cancel", type=int, metavar="任务ID", help="删掉一条待办（软删除）")
    parser.add_argument("--requeue", type=int, metavar="任务ID", help="把回收下来的任务放回队列")
    parser.add_argument("--revive", type=int, metavar="任务ID", help="解除死信标记")
    parser.add_argument("--reason", default="", help="配合 --cancel，记进审计")


def run(ctx, args) -> Result:
    conn = ctx.connect()
    actions: list[str] = []

    if args.priority:
        raw_id, raw_priority = args.priority
        if not raw_id.isdigit() or not raw_priority.lstrip("-").isdigit():
            raise UsageError("用法： --priority <任务ID> <优先级数字>")
        task = ledger.set_priority(
            conn, task_id=int(raw_id), priority=int(raw_priority)
        )
        actions.append(f"✓ #{task.id} 优先级已改为 {task.priority}")

    if args.cancel is not None:
        task = ledger.cancel(conn, task_id=args.cancel, reason=args.reason or "用户删除待办")
        actions.append(f"✓ #{task.id}「{task.title}」已删除（软删除，审计里查得到）")

    if args.requeue is not None:
        task = ledger.requeue(conn, task_id=args.requeue)
        actions.append(f"✓ #{task.id} 已放回队列")

    if args.revive is not None:
        task = ledger.revive(conn, task_id=args.revive)
        actions.append(f"✓ #{task.id} 已解除死信，可以重排了")

    pending = ledger.queue(conn)
    free = [s.role for s in ledger.free_slots(conn)]

    blocks = []
    if actions:
        blocks.append("\n".join(actions))
    blocks.append(section(f"队列（{len(pending)} 条）：", queue_block(pending, limit=20)))

    steps: list[str] = []
    if pending and free:
        steps.append(
            f"队首 #{pending[0].id} 可以派： python pool.py dispatch {pending[0].id} --to {free[0]}"
        )
    elif pending:
        steps.append("slot 全占着，等用户审批后再派")
    return Result(
        text=join(*blocks, next_steps(steps)),
        data={
            "queue": [task_dict(t) for t in pending],
            "free_slots": free,
            "actions": actions,
        },
    )
