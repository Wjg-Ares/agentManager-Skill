"""存活探针：回收死掉的 worker 与它卡住的 slot。

**这是慢路径**（要调 `claude agents --json`，约 1 秒），所以只有这条命令能碰
liveness —— hook 的热路径一律只查本地 SQLite。

探不到不等于都死了：探针失败时一个 slot 都不回收，宁可漏收也不能把活着的
worker 的任务抢走造成双写。
"""

from __future__ import annotations

import argparse

from .. import ledger, liveness
from ..render import Result, join, next_steps, section, task_line

NAME = "reap"
HELP = "存活探针：回收已消失的 worker 与它占着的 slot、文件声明（慢，约 1 秒）"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--list", action="store_true", help="只列出当前活跃会话，不做任何回收"
    )


def run(ctx, args) -> Result:
    conn = ctx.connect()

    if args.list:
        sessions = liveness.list_sessions()
        if sessions is None:
            return Result(
                text="✗ 探针不可用：找不到 claude 可执行文件或调用失败",
                data={"probed": False, "sessions": []},
                exit_code=1,
            )
        lines = [
            f"  {s.name}  {s.session_id[:8]}  {s.status}  {s.cwd or ''}" for s in sessions
        ]
        return Result(
            text=section(f"活跃会话（{len(sessions)} 个）：", lines or ["  （无）"]),
            data={
                "probed": True,
                "sessions": [
                    {"name": s.name, "session_id": s.session_id, "status": s.status,
                     "cwd": s.cwd}
                    for s in sessions
                ],
            },
        )

    result = liveness.reap(conn)
    if not result.probed:
        return Result(
            text=f"⚠ {result.note}",
            data={"probed": False},
            exit_code=1,
        )

    if not result.dead_roles:
        head = "✓ 所有已注册的 worker 都还活着，没有需要回收的"
    else:
        head = (
            f"✓ 回收完成：{', '.join(result.dead_roles)} 已消失，"
            f"释放 {result.released_claims} 条文件声明"
        )

    blocks = []
    if result.recovered_tasks:
        tasks = [ledger.require(conn, tid) for tid in result.recovered_tasks]
        blocks.append(
            section(
                "回收的任务（已置为 failed，可重排）：",
                [f"  {task_line(t)}" for t in tasks],
            )
        )

    overdue = liveness.overdue_tasks(conn)
    if overdue:
        blocks.append(
            section(
                "超时但 worker 还活着的（不自动回收，交给你判断）：",
                [f"  #{tid} 已 {mins} 分钟" for tid, mins in overdue],
            )
        )

    steps = []
    if result.recovered_tasks:
        first = result.recovered_tasks[0]
        steps.append(f"重排： python pool.py queue --requeue {first}")
    if result.dead_roles:
        steps.append(
            f"补位：新开窗口后在里面执行 /am-worker register {result.dead_roles[0]}"
        )
    return Result(
        text=join(head, *blocks, next_steps(steps)),
        data={
            "probed": True,
            "dead_roles": result.dead_roles,
            "recovered_tasks": result.recovered_tasks,
            "released_claims": result.released_claims,
            "overdue": [{"task_id": t, "minutes": m} for t, m in overdue],
        },
    )
