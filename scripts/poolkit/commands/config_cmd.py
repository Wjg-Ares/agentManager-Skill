"""读写 settings 表：并发上限、超时阈值、重派次数。

配置放库里而不是 settings.json：跟着项目走、一条命令就改、不污染用户的
Claude Code 配置。
"""

from __future__ import annotations

import argparse

from .. import config as cfg
from .. import db
from ..errors import UsageError
from ..models import utcnow
from ..render import Result, join, section

NAME = "config"
HELP = "查看/修改配置（max_workers、deliver_timeout_min、max_attempts、default_priority）"

_LABELS = {
    "max_workers": "并发 worker 上限",
    "deliver_timeout_min": "派活后多久未交付算卡死（分钟）",
    "max_attempts": "重派几次后进死信",
    "default_priority": "新任务的默认优先级（越小越先）",
}


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("action", nargs="?", choices=["get", "set"], default="get")
    parser.add_argument("key", nargs="?")
    parser.add_argument("value", nargs="?")


def run(ctx, args) -> Result:
    conn = ctx.connect()

    if args.action == "set":
        if not args.key or args.value is None:
            raise UsageError("用法： python pool.py config set <键> <值>")
        if args.key not in cfg.DEFAULT_SETTINGS:
            raise UsageError(
                f"未知配置项：{args.key}",
                hint="可用：" + "、".join(cfg.DEFAULT_SETTINGS),
            )
        if not args.value.lstrip("-").isdigit():
            raise UsageError(f"{args.key} 要求整数，收到 {args.value!r}")
        now = utcnow()
        with db.transaction(conn):
            db.set_setting(conn, args.key, args.value, now)
            db.log_event(
                conn, kind="config.set", now=now, detail=f"{args.key}={args.value}"
            )
        return Result(
            text=f"✓ {args.key} 已改为 {args.value}（{_LABELS[args.key]}）",
            data={"key": args.key, "value": args.value},
        )

    settings = db.all_settings(conn)
    if args.key:
        if args.key not in settings:
            raise UsageError(f"未知配置项：{args.key}")
        return Result(
            text=f"{args.key} = {settings[args.key]}",
            data={args.key: settings[args.key]},
        )

    lines = [
        f"  {key:<22} {value:<6} {_LABELS.get(key, '')}"
        for key, value in sorted(settings.items())
    ]
    return Result(
        text=join(
            section("当前配置：", lines),
            "改： python pool.py config set <键> <值>",
        ),
        data=settings,
    )
