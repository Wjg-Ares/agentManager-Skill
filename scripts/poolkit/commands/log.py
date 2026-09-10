"""审计查询：谁在何时做了什么。

排查「这个文件怎么被改了」「任务为什么进了死信」全靠这张表。
"""

from __future__ import annotations

import argparse

from ..render import Result, section

NAME = "log"
HELP = "查审计记录（--task 过滤任务、--actor 过滤角色、--kind 过滤动作）"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-n", "--limit", type=int, default=30, help="条数，缺省 30")
    parser.add_argument("--task", type=int, help="只看某个任务")
    parser.add_argument("--actor", help="只看某个角色")
    parser.add_argument("--kind", help="只看某类动作，支持前缀，如 task. / declare")


def run(ctx, args) -> Result:
    conn = ctx.connect()

    where, params = ["1=1"], []
    if args.task is not None:
        where.append("task_id=?")
        params.append(args.task)
    if args.actor:
        where.append("actor=?")
        params.append(args.actor)
    if args.kind:
        where.append("kind LIKE ?")
        params.append(f"{args.kind}%")
    params.append(args.limit)

    rows = conn.execute(
        f"SELECT * FROM events WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ?",
        params,
    ).fetchall()

    lines = []
    for row in reversed(rows):
        task = f" #{row['task_id']}" if row["task_id"] else ""
        actor = f" {row['actor']}" if row["actor"] else ""
        detail = f" — {row['detail']}" if row["detail"] else ""
        lines.append(f"  {row['at']}  {row['kind']}{task}{actor}{detail}")

    return Result(
        text=section(f"审计（最近 {len(rows)} 条，早→晚）：", lines or ["  （无）"]),
        data={
            "events": [
                {
                    "id": r["id"],
                    "at": r["at"],
                    "kind": r["kind"],
                    "actor": r["actor"],
                    "task_id": r["task_id"],
                    "detail": r["detail"],
                }
                for r in reversed(rows)
            ]
        },
    )
