"""建任务。一律先进队列，派不派得出去由 dispatch 决定。"""

from __future__ import annotations

import argparse

from .. import ledger
from ..render import Result, join, next_steps, task_dict

NAME = "add"
HELP = "新建任务并入队"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("title", help="一句话任务标题")
    parser.add_argument("--detail", help="完整需求描述，派活时原样转给 worker")
    parser.add_argument(
        "--priority", type=int, help="越小越先出队，缺省 100"
    )


def run(ctx, args) -> Result:
    conn = ctx.connect()
    task = ledger.create(
        conn, title=args.title, detail=args.detail, priority=args.priority
    )
    slots = ledger.slots(conn)
    free = [s for s in slots if s.is_free]
    registered = [s for s in slots if s.registration is not None]
    ahead = [t for t in ledger.queue(conn) if t.id != task.id]

    head = f"✓ 已建任务 #{task.id}：{task.title}（优先级 {task.priority}）"
    if free:
        steps = [
            f"直接派给空闲的 {free[0].role}： "
            f"python pool.py dispatch {task.id} --to {free[0].role}"
        ]
    elif not registered:
        # 「没人上线」和「都忙着」是两回事，混为一谈会让主 agent 以为无人可派，
        # 转头自己动手 —— 那就绕过了账本、锁和审批，整套编排失效。
        first = slots[0].role if slots else "worker-1"
        steps = [
            "**还没有任何 worker 上线**，不是 slot 占满了",
            f"让用户新开一个窗口，在里面执行： /am-worker register {first}",
            "**不要自己做这个任务** —— 你自己干就没有文件锁、没有交付记录、没有审批",
        ]
    else:
        steps = [
            f"slot 全占着，已排队（前面还有 {len(ahead)} 条）",
            "用户对某个交付说满意后 slot 会腾出来，那时再派",
        ]
    return Result(
        text=join(head, next_steps(steps)),
        data={
            "task": task_dict(task),
            "queue_ahead": len(ahead),
            "free_slots": [s.role for s in free],
            "registered_workers": len(registered),
        },
    )
