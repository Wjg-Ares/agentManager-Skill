"""用户满意：打结束标记、释放文件声明、放 slot，**并把队列待办一并给出来**。

对应 slash 命令 `/am-approve`。

队列推进绑定在这条命令上是刻意的（§4.3）：用户说「满意」的那一刻他本来就在场，
把选项直接摆在他面前，就不需要轮询、定时器或任何额外的提醒机制。
实现时不要另起一套通知。
"""

from __future__ import annotations

import argparse

from .. import ledger
from ..render import Result, join, section, task_line

NAME = "approve"
HELP = "用户满意：结束任务、放 slot、释放声明，并列出队列待办与选项"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("task_id", type=int)


def run(ctx, args) -> Result:
    conn = ctx.connect()
    result = ledger.approve(conn, task_id=args.task_id)
    task = result.task

    head = (
        f"✓ #{task.id}「{task.title}」已通过"
        f"（{result.freed_role} 空出来了，释放 {result.released} 条文件声明）"
    )

    blocks = [
        section(
            "接下来该主 agent 做的：",
            [
                "  1. 构建验证这次改动（worker 交付时是「未编译验证」的）",
                f"  2. 通知 {result.freed_role} 可以 /clear 清上下文了",
            ],
        )
    ]

    # 队列联动
    if result.queue:
        head_task = result.queue[0]
        target = result.free_slots[0] if result.free_slots else None
        lines = [f"  {i}. {task_line(t, with_status=False)}"
                 for i, t in enumerate(result.queue, 1)]
        blocks.append(section(f"队列里还有 {len(result.queue)} 条待办：", lines))

        options = [
            f"  1  执行 #{head_task.id}"
            + (f" —— 派给 {target}" if target else "（当前没有空闲 slot，需先注册新 worker）"),
            f"  2  删除 #{head_task.id} 这条待办",
            "  3  改优先级后重排",
            "  0  先不处理",
        ]
        blocks.append(section("请用户选择：", options))

        cmds = [
            f"  选 1 → python pool.py dispatch {head_task.id}"
            + (f" --to {target}" if target else ""),
            f"  选 2 → python pool.py queue --cancel {head_task.id}",
            f"  选 3 → python pool.py queue --priority {head_task.id} <数字，越小越先>",
            "  选 0 → 什么都不做",
        ]
        blocks.append(section("对应命令（用户选完再执行，别抢跑）：", cmds))
    else:
        blocks.append(section("队列：", ["  （空，没有待办）"]))

    return Result(
        text=join(head, *blocks),
        data={
            "task_id": task.id,
            "freed_role": result.freed_role,
            "released_claims": result.released,
            "queue": [
                {"id": t.id, "title": t.title, "priority": t.priority}
                for t in result.queue
            ],
            "free_slots": result.free_slots,
            "options": ["execute", "delete", "reprioritize", "defer"],
            "next_task_id": result.queue[0].id if result.queue else None,
        },
    )
