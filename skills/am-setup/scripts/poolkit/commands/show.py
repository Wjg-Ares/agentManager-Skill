"""按 ID 查完整产出。

主 agent 平时只吃五行摘要，**需要细节时才调这条** —— 全文进上下文是有代价的，
所以这个动作必须是显式的、按需的（§5.1）。
"""

from __future__ import annotations

import argparse
from pathlib import Path

from .. import ledger
from ..errors import UsageError
from ..render import Result, deliverable_block, deliverable_dict, join, section

NAME = "show"
HELP = "按交付 ID 查完整产出（主 agent 按需调用）"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("deliverable_id", type=int, nargs="?", help="交付 ID")
    parser.add_argument("--task", type=int, help="改为查某任务的最新一轮交付")
    parser.add_argument(
        "--summary-only", action="store_true", help="只看摘要，不要正文"
    )
    parser.add_argument(
        "--out", help="把完整产出写到文件而不是打印（正文很长时用，省上下文）"
    )


def run(ctx, args) -> Result:
    conn = ctx.connect()

    if args.task is not None:
        item = ledger.latest_deliverable(conn, args.task)
        if item is None:
            raise UsageError(f"任务 #{args.task} 还没有任何交付")
    elif args.deliverable_id is not None:
        item = ledger.require_deliverable(conn, args.deliverable_id)
    else:
        raise UsageError("给出交付 ID，或用 --task <任务ID> 查最新一轮")

    blocks = [section("", deliverable_block(item))]

    if args.summary_only:
        return Result(
            text=join(*blocks),
            data=deliverable_dict(item, with_content=False),
        )

    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(item.content, encoding="utf-8")
        blocks.append(f"完整产出已写入 {path}（{len(item.content)} 字），未打印到这里")
        return Result(
            text=join(*blocks),
            data={**deliverable_dict(item), "written_to": str(path)},
        )

    blocks.append(section("完整产出：", ["", item.content]))
    return Result(
        text=join(*blocks), data=deliverable_dict(item, with_content=True)
    )
