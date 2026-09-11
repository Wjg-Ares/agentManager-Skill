"""派活：打开始标记，并把该发给 worker 的原文一并生成好。

命令输出直接给出「复制这段发给 worker-N」，主 agent 不需要自己组织措辞，
省一轮推理也省 token。
"""

from __future__ import annotations

import argparse

from .. import claims as claims_mod
from .. import ledger, registry
from ..errors import Conflict
from ..render import Result, join, next_steps, section, task_dict

NAME = "dispatch"
HELP = "把队列里的任务派给某个 worker（打开始标记）"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("task_id", type=int, help="任务 ID")
    parser.add_argument(
        "--to", help="目标 worker，如 worker-1；缺省自动挑一个空闲的"
    )


def run(ctx, args) -> Result:
    conn = ctx.connect()

    worker = args.to or _pick_free(conn)
    task = ledger.dispatch(conn, task_id=args.task_id, worker=worker)
    reg = registry.require(conn, worker)

    # 派活前把已被别人占住的文件列出来，让 worker 一开始就知道要绕开谁。
    # 冲突在派活阶段先消掉一半，不必全堆到 hook 去拦（§6.2）。
    occupied = [c for c in claims_mod.active_all(conn) if c.worker != worker]

    brief_lines = [
        f"任务 #{task.id}：{task.title}",
    ]
    if task.detail:
        brief_lines.append(task.detail)
    brief_lines += [
        "",
        "开工前先声明你要改的文件：",
        f"  python pool.py declare {task.id} <文件路径...>",
        "交付：",
        f'  python pool.py deliver {task.id} --changed "..." --impact "..." '
        f'--risk "无" --confirm "无" --content-file <产出文件>',
    ]
    if occupied:
        brief_lines += [
            "",
            "以下文件已被别人占着，绕开或先协商：",
            *[f"  {c.display_path} ← {c.worker}" for c in occupied],
        ]

    head = f"✓ #{task.id} 已派给 {worker}（{reg.session_name}）"
    body = section(f"把下面这段发给 `{reg.session_name}`：", ["  " + l if l else "" for l in brief_lines])
    steps = next_steps(
        [
            f"用跨会话消息发给 `{reg.session_name}`",
            "它交付后会回一条五行摘要 + 交付 ID，你只看摘要，需要细节再 python pool.py show <ID>",
        ]
    )
    return Result(
        text=join(head, body, steps),
        data={
            "task": task_dict(task),
            "worker": worker,
            "address": reg.session_name,
            "message": "\n".join(brief_lines),
            "occupied": [
                {"path": c.display_path, "worker": c.worker, "task_id": c.task_id}
                for c in occupied
            ],
        },
    )


def _pick_free(conn) -> str:
    free = ledger.free_slots(conn)
    if not free:
        slots = ledger.slots(conn)
        unregistered = [s.role for s in slots if s.registration is None]
        raise Conflict(
            "没有空闲 slot",
            hint=(
                f"新开窗口注册 {unregistered[0]}" if unregistered
                else "等用户对某个交付说满意，slot 才会释放"
            ),
        )
    return free[0].role
