"""worker 交付。

完整产出落库，命令返回的是**五行摘要 + 交付 ID** —— 那才是发给主 agent 的东西。
主 agent 只吃摘要，绝不读全文；需要细节时凭 ID 去查（§5.1）。
这是防止主 agent 被 3 路完整产出撑爆的核心措施。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .. import claims as claims_mod
from .. import ledger, registry
from ..errors import NotRegistered, UsageError
from ..models import Summary
from ..render import Result, join, next_steps, section

NAME = "deliver"
HELP = "交付：完整产出落库，返回五行摘要 + 交付 ID"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("task_id", type=int)
    parser.add_argument("--changed", required=True, help="改了什么（一到两行）")
    parser.add_argument("--impact", required=True, help="影响面：涉及的文件/接口/表")
    parser.add_argument("--risk", default="无", help="风险点，无则写「无」")
    parser.add_argument("--confirm", default="无", help="待用户确认，无则写「无」")
    parser.add_argument("--files", help="涉及文件，缺省自动填本任务的活跃声明")
    parser.add_argument("--content", help="完整产出正文")
    parser.add_argument(
        "--content-file", help="从文件读完整产出（和 --content 二选一；都不给则读 stdin）"
    )


def run(ctx, args) -> Result:
    conn = ctx.connect()

    me = registry.by_session(conn, ctx.session_id) if ctx.session_id else None
    if me is None:
        raise NotRegistered()

    content = _read_content(args)
    if not content.strip():
        raise UsageError(
            "完整产出是空的",
            hint="用 --content / --content-file 给出正文，或从 stdin 管进来",
        )

    files = args.files
    if not files:
        active = claims_mod.active_for_task(conn, args.task_id)
        files = "、".join(c.display_path for c in active) if active else "无"

    summary = Summary(
        changed=args.changed.strip(),
        impact=args.impact.strip(),
        risk=(args.risk or "无").strip(),
        confirm=(args.confirm or "无").strip(),
        files=files,
    )
    item = ledger.deliver(
        conn, task_id=args.task_id, worker=me.role, summary=summary, content=content
    )

    main_addr = registry.address_of(conn, "main") or "主 agent"
    message = (
        f"任务 #{args.task_id} 已交付。交付 ID：{item.id}\n" + summary.render()
    )

    head = f"✓ 已交付，交付 ID {item.id}（第 {item.round} 轮），完整产出已落库 {len(content)} 字"
    body = section(f"把下面这段发给主 agent（`{main_addr}`），**不要发完整产出**：",
                   ["  " + line for line in message.splitlines()])
    steps = next_steps(
        [
            f"用跨会话消息发给 `{main_addr}`",
            "等用户审批。通过后你的 slot 和文件声明会自动释放，那时可以 /clear 清上下文",
            "被打回则在本会话继续改（上下文还在，别换人）",
        ]
    )
    return Result(
        text=join(head, body, steps),
        data={
            "deliverable_id": item.id,
            "task_id": item.task_id,
            "round": item.round,
            "message": message,
            "content_length": len(content),
            "main_address": main_addr,
        },
    )


def _read_content(args) -> str:
    if args.content and args.content_file:
        raise UsageError("--content 和 --content-file 只能给一个")
    if args.content:
        return args.content
    if args.content_file:
        path = Path(args.content_file)
        if not path.exists():
            raise UsageError(f"找不到文件：{path}")
        return path.read_text(encoding="utf-8", errors="replace")
    if not sys.stdin.isatty():
        return sys.stdin.read()
    raise UsageError(
        "没给完整产出",
        hint="用 --content-file <文件>，或把正文管给 stdin",
    )
