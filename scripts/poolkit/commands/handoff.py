"""交接一把锁。

协商判定为**假冲突**（两人改的是同一文件的不同 region）时，由持有者执行，
把锁让给对方。真冲突不要用这条 —— 等审批通过后锁会自动释放。
"""

from __future__ import annotations

import argparse

from .. import claims as claims_mod
from .. import config, ledger, registry
from ..errors import NotFound, UsageError
from ..render import Result, join, next_steps

NAME = "handoff"
HELP = "把某个文件的锁交接给另一个 worker（协商判定为假冲突时用）"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("file", help="文件路径")
    parser.add_argument("--to", required=True, help="接手的 worker，如 worker-2")
    parser.add_argument("--task", type=int, help="接手方的任务 ID，缺省用它当前在办的")
    parser.add_argument("--scope", help="接手方要改的方法 / region")


def run(ctx, args) -> Result:
    conn = ctx.connect()
    registry.validate_role(args.to)

    key = config.normalize_path(args.file)
    current = claims_mod.active_for(conn, key)

    me = registry.by_session(conn, ctx.session_id) if ctx.session_id else None
    if current is not None and me is not None and current.worker != me.role:
        raise UsageError(
            f"{args.file} 的锁在 {current.worker} 手上，不在你（{me.role}）手上",
            hint="交接要由持有者执行；你要抢的话请先和它协商",
        )

    task_id = args.task
    if task_id is None:
        task = ledger.open_task_of(conn, args.to)
        if task is None:
            raise NotFound(
                f"{args.to} 当前没有在办任务",
                hint="用 --task 指定它的任务 ID",
            )
        task_id = task.id

    claim = claims_mod.handoff(
        conn, file_path=args.file, to_worker=args.to, to_task=task_id, scope=args.scope
    )
    address = registry.address_of(conn, args.to) or args.to

    head = (
        f"✓ {args.file} 的锁已交给 {args.to}（任务 #{task_id}）"
        + (f"，声明范围：{args.scope}" if args.scope else "")
    )
    steps = next_steps(
        [
            f"告诉 `{address}` 锁已交接，它可以动手了",
            "你自己**不要再改这个文件**，否则会覆盖对方的改动",
            "你要改回来就再谈一次，别直接抢",
        ]
    )
    return Result(
        text=join(head, steps),
        data={
            "claim_id": claim.id,
            "path": claim.display_path,
            "from": current.worker if current else None,
            "to": args.to,
            "task_id": task_id,
            "address": address,
        },
    )
