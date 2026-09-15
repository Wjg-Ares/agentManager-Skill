"""全景：slot / 待审批 / 队列 / 声明，外加一份「现在该做什么」。

对应 slash 命令 `/am-status`。主 agent compact 之后调一次这个就完全恢复调度状态 ——
账本落库的意义就在这里。

这条命令的输出自带下一步指引，SKILL.md 因此不需要写任何分支判断。
"""

from __future__ import annotations

import argparse

from .. import claims as claims_mod
from .. import db, guard, ledger, liveness, registry
from ..render import (
    Result,
    join,
    next_steps,
    queue_block,
    section,
    slot_block,
    slot_dict,
    task_dict,
    task_line,
)

NAME = "status"
HELP = "账本全景：各 slot、待审批、队列、文件声明，并给出下一步"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--claims", action="store_true", help="额外列出全部活跃文件声明"
    )
    parser.add_argument(
        "--check-live",
        action="store_true",
        help="顺带跑一次存活探针（慢路径，约 1 秒）",
    )


def run(ctx, args) -> Result:
    conn = ctx.connect()

    reap_note = ""
    if args.check_live:
        result = liveness.reap(conn)
        if not result.probed:
            reap_note = f"⚠ {result.note}"
        elif result.dead_roles:
            reap_note = (
                f"已回收：{', '.join(result.dead_roles)}"
                f"（任务 {result.recovered_tasks or '无'}，释放 {result.released_claims} 条声明）"
            )

    slots = ledger.slots(conn)
    queue = ledger.queue(conn)
    pending = _pending_approval(conn)
    dead = ledger.dead_letters(conn)
    overdue = liveness.overdue_tasks(conn)

    blocks = [section("Slot：", [line for s in slots for line in slot_block(s)])]

    stuck = _stuck_on_prompt(conn)
    if stuck:
        blocks.append(
            section(
                f"⏸ 卡在权限确认上（{len(stuck)} 个）：",
                [f"  {role} 等 {mins} 分钟了 · {what}" for role, mins, what in stuck],
            )
        )

    if pending:
        lines = []
        for task, deliverable in pending:
            lines.append(f"  {task_line(task, with_status=False)}")
            if deliverable is not None:
                lines.append(
                    f"    交付 #{deliverable.id}（第 {deliverable.round} 轮）"
                    f" · {deliverable.summary.changed}"
                )
        blocks.append(section(f"待审批（{len(pending)} 条）：", lines))

    blocks.append(section(f"队列（{len(queue)} 条）：", queue_block(queue)))

    if dead:
        blocks.append(
            section(
                f"死信（{len(dead)} 条，已停止自动重派）：",
                [f"  {task_line(t)}" for t in dead],
            )
        )
    if overdue:
        timeout_min = db.get_int_setting(conn, "deliver_timeout_min")
        blocks.append(
            section(
                f"超时未交付（阈值 {timeout_min} 分钟）：",
                [f"  #{tid} 已 {mins} 分钟" for tid, mins in overdue],
            )
        )
    if args.claims:
        active = claims_mod.active_all(conn)
        blocks.append(
            section(
                f"活跃声明（{len(active)} 条）：",
                [f"  {c.worker} → {c.display_path}"
                 + (f"（{c.scope}）" if c.scope else "")
                 for c in active]
                or ["  （无）"],
            )
        )

    steps = _next_steps(slots, queue, pending, dead, overdue)
    if stuck:
        # 排最前面：它是唯一一件**只有人能解**的事，而且卡着的那个 worker
        # 在此期间完全不动。主 agent 再怎么调度也绕不过去。
        address = registry.address_of(conn, stuck[0][0]) or stuck[0][0]
        steps.insert(
            0,
            f"⏸ 去 `{address}` 那个窗口按一下确认 —— {stuck[0][0]} 正等着，"
            f"它在被批准之前什么都做不了",
        )
    head = "=== am 状态 ===" + (f"\n{reap_note}" if reap_note else "")

    return Result(
        text=join(head, *blocks, next_steps(steps)),
        data={
            "slots": [slot_dict(s) for s in slots],
            "queue": [task_dict(t) for t in queue],
            "pending_approval": [
                {"task": task_dict(t), "deliverable_id": d.id if d else None}
                for t, d in pending
            ],
            "dead_letters": [task_dict(t) for t in dead],
            "overdue": [{"task_id": tid, "minutes": m} for tid, m in overdue],
            "free_slots": [s.role for s in slots if s.is_free],
        },
    )


def _stuck_on_prompt(conn) -> list[tuple[str, int, str]]:
    """哪些 worker 正卡在权限确认框上。返回 [(角色, 已等分钟, 在等什么)]。

    判据是「该 worker 的**最后**一条事件是 prompt.pending」。用最后一条而不是
    「有没有过这条」，字条就不需要显式清除 —— worker 一旦动起来（声明、交付、
    甚至下一次 hook 判定）就会产生新事件，旧字条自然失效。

    这是 hook 在弹窗前留下的，因为 worker 卡住之后自己什么都发不出来。
    """
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    stuck: list[tuple[str, int, str]] = []
    for reg in registry.workers(conn):
        row = conn.execute(
            "SELECT kind, detail, at FROM events WHERE actor=? "
            "ORDER BY id DESC LIMIT 1",
            (reg.role,),
        ).fetchone()
        if row is None or row["kind"] != guard.PENDING_KIND:
            continue
        try:
            at = datetime.fromisoformat(row["at"])
            if at.tzinfo is None:
                at = at.replace(tzinfo=timezone.utc)
            minutes = max(0, int((now - at).total_seconds() // 60))
        except ValueError:
            minutes = 0
        stuck.append((reg.role, minutes, row["detail"] or "（未记录）"))
    return stuck


def _pending_approval(conn):
    rows = conn.execute(
        "SELECT * FROM tasks WHERE status='delivered' ORDER BY delivered_at"
    ).fetchall()
    from ..models import Task

    out = []
    for row in rows:
        task = Task.from_row(row)
        out.append((task, ledger.latest_deliverable(conn, task.id)))
    return out


def _next_steps(slots, queue, pending, dead, overdue) -> list[str]:
    """把「该做什么」算出来，而不是留给 agent 推。"""
    steps: list[str] = []

    if pending:
        ids = "、".join(f"#{t.id}" for t, _ in pending)
        steps.append(
            f"请用户审批 {ids}。看细节： python pool.py show <交付ID>；"
            f"用户满意： python pool.py approve <任务ID>；不满意： python pool.py reject <任务ID> \"原因\""
        )

    free = [s.role for s in slots if s.is_free]
    registered = [s for s in slots if s.registration is not None]
    unregistered = [s.role for s in slots if s.registration is None]

    if queue and free:
        steps.append(
            f"队首 #{queue[0].id} 可以派了： python pool.py dispatch {queue[0].id} --to {free[0]}"
        )
    elif not registered:
        # 一个 worker 都没上线。必须说清楚这不是「都忙着」—— 说错了主 agent
        # 会以为无人可派而自己动手，那就绕过了锁和审批，整套编排失效。
        steps.append(
            f"**还没有任何 worker 上线**。让用户新开窗口执行 "
            f"/am-worker register {unregistered[0] if unregistered else 'worker-1'}"
            f"，别自己接活干"
        )
    elif queue:
        steps.append("slot 全占着，队列等 slot 释放；用户对某个交付说满意后会自动腾出来")

    if unregistered and registered and queue:
        steps.append(
            f"想加并发：新开窗口后执行 /am-worker register {unregistered[0]}"
        )

    if overdue:
        steps.append(
            f"#{overdue[0][0]} 超时了，先确认那个 worker 是不是还活着： python pool.py reap"
        )
    if dead:
        steps.append(
            f"死信 #{dead[0].id} 需要你定夺：修好原因后 python pool.py queue --revive {dead[0].id}"
        )

    if not steps:
        steps.append(
            "没有待办。有新需求就 python pool.py add \"标题\" 建任务再派下去，别自己动手做"
        )
    return steps
