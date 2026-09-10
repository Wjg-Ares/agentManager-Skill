"""worker 启动时登记自己。

会话名重开即变，所以每次开新窗口都要重登记一次 —— role 是主键，重复执行是 UPDATE。
"""

from __future__ import annotations

import argparse

from .. import config, ledger, liveness, registry
from ..errors import UsageError
from ..render import Result, join, next_steps, section

NAME = "register"
HELP = "把本会话登记成某个角色（worker-1 / worker-2 / ... / main）"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("role", help="角色名，如 worker-1")
    parser.add_argument(
        "--session-id", help="指定 session_id，缺省取环境变量 CLAUDE_CODE_SESSION_ID"
    )
    parser.add_argument("--session-name", help="指定会话名，缺省自动探测")


def run(ctx, args) -> Result:
    conn = ctx.connect()
    registry.validate_role(args.role)

    session_id = args.session_id or ctx.session_id
    if not session_id:
        raise UsageError(
            "拿不到本会话的 session_id",
            hint="确认在 Claude Code 会话里执行；也可用 --session-id 手工指定",
        )

    live = liveness.self_session(session_id) if not args.session_name else None
    session_name = args.session_name or (live.name if live else session_id[:8])
    reg = registry.register(
        conn,
        role=args.role,
        session_id=session_id,
        session_name=session_name,
        pid=live.pid if live else None,
        cwd=str(ctx.project_root) if ctx.project_root else None,
    )

    task = ledger.open_task_of(conn, reg.role)
    body = section(
        f"✓ 本会话已登记为 {reg.role}",
        [
            f"  会话名     {reg.session_name}（别人给你发消息用这个地址）",
            f"  session_id {reg.session_id}",
            f"  当前任务   {'#%d %s' % (task.id, task.title) if task else '无'}",
        ],
    )

    if reg.role == config.MAIN_ROLE:
        steps = ["用 /am-status 看全局；直接跟用户对话即可"]
    elif task is not None:
        steps = [
            f"你手上还有任务 #{task.id}，继续做即可",
            "改代码前先声明文件： python pool.py declare <文件...>",
        ]
    else:
        steps = [
            "等主 agent 派活。派到后会有跨会话消息通知你",
            "改代码前先声明文件： python pool.py declare <文件...>",
            "构建和 git 写操作已被 hook 拦住，那是主 agent 的活",
        ]
    return Result(
        text=join(body, next_steps(steps)),
        data={
            "role": reg.role,
            "session_name": reg.session_name,
            "session_id": reg.session_id,
            "current_task": task.id if task else None,
        },
    )
