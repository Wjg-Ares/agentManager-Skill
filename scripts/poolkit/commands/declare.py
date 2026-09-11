"""worker 声明将要改动的文件。

支持增量追加 —— 干到一半发现要动新文件，再调一次就行，不必一开始想全。
撞车时输出里直接给出持有者的会话名和该说什么，worker 拿到就能去谈（§6.4）。
"""

from __future__ import annotations

import argparse

from .. import claims as claims_mod
from .. import ledger, registry
from ..errors import NotRegistered, UsageError
from ..render import Result, join, next_steps, section

NAME = "declare"
HELP = "声明本次要改的文件（即上锁）；撞车会告诉你去找谁谈"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "paths", nargs="+", help="文件路径，可多个。第一个参数是纯数字时视为任务 ID"
    )
    parser.add_argument("--task", type=int, help="任务 ID，缺省用本会话当前在办的任务")
    parser.add_argument(
        "--scope", help="选填：要改的方法 / region。只在协商时给对方看，不参与锁判定"
    )


def run(ctx, args) -> Result:
    conn = ctx.connect()

    paths = list(args.paths)
    task_id = args.task
    # 允许 `declare 12 a.cs b.cs` 这种写法
    if task_id is None and paths and paths[0].isdigit():
        task_id = int(paths.pop(0))
    if not paths:
        raise UsageError("没给出任何文件路径")

    me = registry.by_session(conn, ctx.session_id) if ctx.session_id else None
    if me is None:
        raise NotRegistered()

    if task_id is None:
        task = ledger.open_task_of(conn, me.role)
        if task is None:
            raise UsageError(
                f"{me.role} 当前没有在办任务",
                hint="等主 agent 派活，或用 --task 明确指定任务 ID",
            )
        task_id = task.id

    result = claims_mod.declare(
        conn, task_id=task_id, worker=me.role, paths=paths, scope=args.scope
    )

    data = {
        "task_id": task_id,
        "worker": me.role,
        "granted": [c.display_path for c in result.granted],
        "already_mine": [c.display_path for c in result.already_mine],
        "conflicts": [
            {
                "path": c.display_path,
                "holder": c.holder,
                "address": c.holder_address,
                "holder_task": c.holder_task,
                "holder_scope": c.holder_scope,
            }
            for c in result.conflicts
        ],
        "ok": result.ok,
    }

    if result.ok:
        lines = [f"  ✓ {c.display_path}" for c in result.granted]
        lines += [f"  · {c.display_path}（早先已声明）" for c in result.already_mine]
        head = f"✓ 已为任务 #{task_id} 声明 {len(result.granted)} 个文件"
        steps = next_steps(
            [
                "可以动手改了。改到一半要碰新文件就再 declare 一次",
                f"交付： python pool.py deliver {task_id} --changed \"...\" ...",
            ]
        )
        return Result(text=join(head, section("", lines), steps), data=data)

    # 撞车：一个都没拿到（整批回滚，不留半成功状态）
    lines = []
    for conflict in result.conflicts:
        addr = conflict.holder_address or conflict.holder
        scope = f"，声明范围：{conflict.holder_scope}" if conflict.holder_scope else ""
        lines.append(
            f"  ✗ {conflict.display_path}\n"
            f"      被 {conflict.holder} 占着（任务 #{conflict.holder_task}{scope}）\n"
            f"      去找 `{addr}` 谈"
        )
    head = f"✗ 声明失败，{len(result.conflicts)} 个文件被别人占着（本次一个都没锁，不存在半成功）"
    steps = next_steps(
        [
            "直接给持有者发跨会话消息：说明你要改哪个方法、为什么",
            "假冲突（改的是不同 region）→ 请对方执行： "
            f"python pool.py handoff <文件> --to {me.role}",
            "真冲突 → 等对方交付并通过审批，锁会自动释放",
            "谈不拢 → 报主 agent 仲裁",
        ]
    )
    return Result(text=join(head, section("", lines), steps), data=data, exit_code=5)
