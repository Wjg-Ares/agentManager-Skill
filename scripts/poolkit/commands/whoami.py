"""我是谁、我手上是什么活、我占着哪些文件。

worker 会话 compact 之后调一次就恢复了 —— 状态在库里，不靠记。
"""

from __future__ import annotations

import argparse

from .. import claims as claims_mod
from .. import ledger, registry
from ..render import Result, join, next_steps, section, task_line

NAME = "whoami"
HELP = "本会话的角色、当前任务、已声明的文件"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--session-id", help="缺省取本会话")


def run(ctx, args) -> Result:
    conn = ctx.connect()
    session_id = args.session_id or ctx.session_id
    me = registry.by_session(conn, session_id) if session_id else None

    if me is None:
        # 没登记的会话多半是刚开的 worker 窗口。把各编号的占用情况摆出来，
        # 并把建议敲的那条命令写全 —— 编号由用户定，程序只负责让他不用猜。
        slots = ledger.slots(conn)
        occupied = [s for s in slots if s.registration is not None]
        free = [s.role for s in slots if s.registration is None]

        lines = []
        for slot in slots:
            reg = slot.registration
            if reg is None:
                lines.append(f"  {slot.role}  空着")
            elif reg.status.value != "alive":
                lines.append(f"  {slot.role}  {reg.session_name}（已死，reap 后可复用）")
            else:
                lines.append(f"  {slot.role}  {reg.session_name} 占着")

        steps = []
        if free:
            steps.append(f"python pool.py register {free[0]}    ← 建议这个")
            if len(free) > 1:
                steps.append(f"（也可以换成 {'、'.join(free[1:])}）")
        else:
            steps.append(
                "编号都占着了。确认有窗口已关就先 python pool.py reap 回收，"
                "或 python pool.py config set max_workers <更大的数>"
            )
        steps.append("只是路过、不参与编排 → 什么都不用做，正常改代码即可")

        return Result(
            text=join(
                "本会话还没登记角色，不受编排规则约束。",
                section(f"当前 slot（已占 {len(occupied)}/{len(slots)}）：", lines),
                next_steps(steps),
            ),
            data={
                "registered": False,
                "session_id": session_id,
                "free_roles": free,
                "suggested": free[0] if free else None,
            },
        )

    task = ledger.open_task_of(conn, me.role)
    mine = [c for c in claims_mod.active_all(conn) if c.worker == me.role]

    lines = [
        f"  角色       {me.role}",
        f"  会话名     {me.session_name}（别人找你用这个地址）",
        f"  当前任务   {task_line(task) if task else '无'}",
        f"  已声明     {len(mine)} 个文件",
    ]
    lines += [f"      {c.display_path}" + (f"（{c.scope}）" if c.scope else "") for c in mine]

    steps: list[str] = []
    if task is None:
        steps.append("等主 agent 派活")
    elif task.status.value == "assigned":
        steps += [
            "改代码前先声明文件： python pool.py declare <文件...>",
            f"做完交付： python pool.py deliver {task.id} --changed \"...\" --impact \"...\"",
        ]
    elif task.status.value == "delivered":
        steps.append("已交付，等用户审批。通过后 slot 与声明会自动释放")
    elif task.status.value == "rejected":
        steps.append(f"被打回了，改完重新交付： python pool.py deliver {task.id} ...")

    return Result(
        text=join(section(f"你是 {me.role}", lines), next_steps(steps)),
        data={
            "registered": True,
            "role": me.role,
            "session_name": me.session_name,
            "session_id": me.session_id,
            "task": {"id": task.id, "title": task.title, "status": str(task.status)}
            if task
            else None,
            "claims": [c.display_path for c in mine],
        },
    )
