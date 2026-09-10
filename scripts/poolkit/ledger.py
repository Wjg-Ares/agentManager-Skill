"""任务与队列：CRUD、状态流转、优先级。

账本必须落库。主 agent 会 compact，一旦失忆就不知道派了什么活给谁，调度即崩 ——
这里是重建状态的唯一来源。

队列不是单独的结构：``tasks`` 表里 ``status='queued'`` 的行就是队列。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from . import claims as claims_mod
from . import db, registry
from .errors import Conflict, InvalidState, NotFound, UsageError
from .models import (
    Claim,
    Deliverable,
    Slot,
    Summary,
    Task,
    TaskStatus,
    WorkerStatus,
    utcnow,
)

# --------------------------------------------------------------------------
# 读
# --------------------------------------------------------------------------


def get(conn: sqlite3.Connection, task_id: int) -> Task | None:
    row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    return Task.from_row(row) if row else None


def require(conn: sqlite3.Connection, task_id: int) -> Task:
    task = get(conn, task_id)
    if task is None:
        raise NotFound(f"任务 #{task_id} 不存在")
    return task


def queue(conn: sqlite3.Connection) -> list[Task]:
    """队列，按优先级升序、同优先级按创建时间。走 ``ix_tasks_queue`` 部分索引。"""
    rows = conn.execute(
        "SELECT * FROM tasks WHERE status='queued' ORDER BY priority, created_at, id"
    ).fetchall()
    return [Task.from_row(r) for r in rows]


def open_task_of(conn: sqlite3.Connection, worker: str) -> Task | None:
    """某 worker 当前仍占着 slot 的任务。

    ``assigned`` / ``delivered`` / ``rejected`` 都算占着 —— 用户没说满意，
    slot 就一直占着，这正是保护追问上下文的机制（§4.1）。
    """
    row = conn.execute(
        "SELECT * FROM tasks WHERE assignee=? AND status IN "
        "('assigned','delivered','rejected') ORDER BY assigned_at LIMIT 1",
        (worker,),
    ).fetchone()
    return Task.from_row(row) if row else None


def dead_letters(conn: sqlite3.Connection) -> list[Task]:
    rows = conn.execute(
        "SELECT * FROM tasks WHERE dead_letter=1 AND status NOT IN ('approved','cancelled') "
        "ORDER BY id"
    ).fetchall()
    return [Task.from_row(r) for r in rows]


def slots(conn: sqlite3.Connection) -> list[Slot]:
    """全部 worker 槽位的聚合视图，`status` 命令的主要输出。

    含 ``max_workers`` 里尚未注册的空位，让主 agent 一眼看出还能开几个窗口。
    """
    max_workers = db.get_int_setting(conn, "max_workers")
    registrations = {r.role: r for r in registry.workers(conn)}
    active_claims: dict[str, list[Claim]] = {}
    for claim in claims_mod.active_all(conn):
        active_claims.setdefault(claim.worker, []).append(claim)

    result: list[Slot] = []
    roles = [f"worker-{i}" for i in range(1, max_workers + 1)]
    # 注册过但超出上限的角色也要显示，否则改小 max_workers 会让它们凭空消失
    roles += [r for r in sorted(registrations) if r not in roles]
    for role in roles:
        result.append(
            Slot(
                role=role,
                registration=registrations.get(role),
                task=open_task_of(conn, role),
                claims=active_claims.get(role, []),
            )
        )
    return result


def free_slots(conn: sqlite3.Connection) -> list[Slot]:
    return [s for s in slots(conn) if s.is_free]


# --------------------------------------------------------------------------
# 写：创建与派发
# --------------------------------------------------------------------------


def create(
    conn: sqlite3.Connection,
    *,
    title: str,
    detail: str | None = None,
    priority: int | None = None,
) -> Task:
    """建任务，一律先进队列。派不派得出去由 dispatch 决定。"""
    if not title.strip():
        raise UsageError("任务标题不能为空")
    now = utcnow()
    prio = priority if priority is not None else db.get_int_setting(conn, "default_priority")
    with db.transaction(conn):
        cur = conn.execute(
            "INSERT INTO tasks(title, detail, status, priority, created_at) "
            "VALUES (?,?,'queued',?,?)",
            (title.strip(), detail, prio, now),
        )
        task_id = int(cur.lastrowid or 0)
        db.log_event(
            conn, kind="task.create", now=now, task_id=task_id, detail=title.strip()
        )
    return require(conn, task_id)


def dispatch(conn: sqlite3.Connection, *, task_id: int, worker: str) -> Task:
    """派活：打开始标记。

    slot 是否空闲在**同一个事务里**复查一遍 —— 「查到空闲」和「写入派发」之间
    存在窗口，隔着窗口做判断就是在赌（§4.1 不用瞬时 status 做调度依据的原因）。
    """
    registry.validate_role(worker)
    now = utcnow()
    with db.transaction(conn):
        task = require(conn, task_id)
        if task.status not in (TaskStatus.QUEUED, TaskStatus.FAILED):
            raise InvalidState(
                f"任务 #{task_id} 当前是 {task.status}，不能派发",
                hint="只有 queued / failed 的任务能派发",
            )
        reg = registry.get(conn, worker)
        if reg is None:
            raise NotFound(
                f"{worker} 未注册",
                hint=f"先在那个窗口执行 /am-worker register {worker}",
            )
        if reg.status is not WorkerStatus.ALIVE:
            raise Conflict(f"{worker} 已标记为死亡", hint="先执行 reap 回收，或让它重新注册")
        busy = open_task_of(conn, worker)
        if busy is not None:
            raise Conflict(
                f"{worker} 正在做 #{busy.id}（{busy.status}）",
                hint="用户对 #%d 说满意后 slot 才会释放" % busy.id,
            )
        conn.execute(
            "UPDATE tasks SET status='assigned', assignee=?, assigned_at=?, "
            "attempts=attempts+1 WHERE id=?",
            (worker, now, task_id),
        )
        registry.touch(conn, worker, now=now)
        db.log_event(
            conn,
            kind="task.dispatch",
            now=now,
            actor=worker,
            task_id=task_id,
            detail=f"→ {worker}（{reg.session_name}）",
        )
    return require(conn, task_id)


# --------------------------------------------------------------------------
# 写：交付与审批
# --------------------------------------------------------------------------


def deliver(
    conn: sqlite3.Connection,
    *,
    task_id: int,
    worker: str,
    summary: Summary,
    content: str,
) -> Deliverable:
    """worker 交付。完整产出落库，返回的 ID 才是发给主 agent 的东西。"""
    now = utcnow()
    with db.transaction(conn):
        task = require(conn, task_id)
        if task.assignee != worker:
            raise Conflict(
                f"任务 #{task_id} 派给的是 {task.assignee}，不是 {worker}"
            )
        if task.status not in (TaskStatus.ASSIGNED, TaskStatus.REJECTED):
            raise InvalidState(f"任务 #{task_id} 当前是 {task.status}，不能交付")

        round_no = int(
            conn.execute(
                "SELECT COALESCE(MAX(round),0)+1 FROM deliverables WHERE task_id=?",
                (task_id,),
            ).fetchone()[0]
        )
        cur = conn.execute(
            """
            INSERT INTO deliverables(task_id, worker, round, s_changed, s_impact,
                                     s_risk, s_confirm, s_files, content, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                task_id,
                worker,
                round_no,
                summary.changed,
                summary.impact,
                summary.risk,
                summary.confirm,
                summary.files,
                content,
                now,
            ),
        )
        deliverable_id = int(cur.lastrowid or 0)
        conn.execute(
            "UPDATE tasks SET status='delivered', delivered_at=? WHERE id=?", (now, task_id)
        )
        registry.touch(conn, worker, now=now)
        db.log_event(
            conn,
            kind="task.deliver",
            now=now,
            actor=worker,
            task_id=task_id,
            detail=f"deliverable #{deliverable_id}（第 {round_no} 轮）",
        )
    return require_deliverable(conn, deliverable_id)


@dataclass(slots=True)
class ApproveResult:
    """审批通过的结果。

    带上队列现状是刻意的：§4.3 要求每次用户说满意，主 agent 都顺带查一次队列。
    把它编进这条命令的返回值，就不需要另起一套轮询或提醒机制。
    """

    task: Task
    released: int  # 释放了几条声明
    freed_role: str | None  # 腾出来的 slot
    queue: list[Task] = field(default_factory=list)
    free_slots: list[str] = field(default_factory=list)


def approve(conn: sqlite3.Connection, *, task_id: int) -> ApproveResult:
    """用户满意：打结束标记、释放该任务的全部文件声明、放 slot。"""
    now = utcnow()
    with db.transaction(conn):
        task = require(conn, task_id)
        if task.status is not TaskStatus.DELIVERED:
            raise InvalidState(
                f"任务 #{task_id} 当前是 {task.status}，不能审批通过",
                hint="只有 delivered 的任务能 approve",
            )
        conn.execute(
            "UPDATE tasks SET status='approved', approved_at=? WHERE id=?", (now, task_id)
        )
        released = claims_mod.release_task(conn, task_id, by="approve", now=now)
        db.log_event(
            conn,
            kind="task.approve",
            now=now,
            actor=task.assignee,
            task_id=task_id,
            detail=f"释放 {released} 条声明",
        )
    return ApproveResult(
        task=require(conn, task_id),
        released=released,
        freed_role=task.assignee,
        queue=queue(conn),
        free_slots=[s.role for s in free_slots(conn)],
    )


def reject(conn: sqlite3.Connection, *, task_id: int, reason: str) -> Task:
    """用户不满意：打回给**同一个** worker，slot 不放、声明不释放。

    换人前功尽弃 —— 追问的上下文只在原 worker 那里（§5.3）。
    """
    now = utcnow()
    with db.transaction(conn):
        task = require(conn, task_id)
        if task.status is not TaskStatus.DELIVERED:
            raise InvalidState(f"任务 #{task_id} 当前是 {task.status}，不能打回")
        conn.execute("UPDATE tasks SET status='rejected' WHERE id=?", (task_id,))
        db.log_event(
            conn,
            kind="task.reject",
            now=now,
            actor=task.assignee,
            task_id=task_id,
            detail=reason,
        )
    return require(conn, task_id)


# --------------------------------------------------------------------------
# 写：队列维护
# --------------------------------------------------------------------------


def set_priority(conn: sqlite3.Connection, *, task_id: int, priority: int) -> Task:
    now = utcnow()
    with db.transaction(conn):
        task = require(conn, task_id)
        if task.status.is_terminal:
            raise InvalidState(f"任务 #{task_id} 已 {task.status}，改优先级没有意义")
        conn.execute("UPDATE tasks SET priority=? WHERE id=?", (priority, task_id))
        db.log_event(
            conn,
            kind="task.priority",
            now=now,
            task_id=task_id,
            detail=f"{task.priority} → {priority}",
        )
    return require(conn, task_id)


def cancel(conn: sqlite3.Connection, *, task_id: int, reason: str = "") -> Task:
    """取消任务（队列里的「2 删除这条待办」走这里）。软删除，保留可追溯性。"""
    now = utcnow()
    with db.transaction(conn):
        task = require(conn, task_id)
        if task.status.is_terminal:
            raise InvalidState(f"任务 #{task_id} 已经是 {task.status}")
        conn.execute(
            "UPDATE tasks SET status='cancelled', cancelled_at=? WHERE id=?", (now, task_id)
        )
        released = claims_mod.release_task(conn, task_id, by="cancel", now=now)
        db.log_event(
            conn,
            kind="task.cancel",
            now=now,
            task_id=task_id,
            detail=f"{reason}（释放 {released} 条声明）",
        )
    return require(conn, task_id)


def fail(conn: sqlite3.Connection, *, task_id: int, reason: str) -> Task:
    """回收一个卡死的任务：释放声明、attempts 超限则进死信。

    调用方须已在事务中 —— reap 要把「标记 worker 死亡」和「回收任务」放进
    同一个事务，否则中间崩溃会留下半回收状态。
    """
    now = utcnow()
    task = require(conn, task_id)
    max_attempts = db.get_int_setting(conn, "max_attempts")
    dead = 1 if task.attempts >= max_attempts else 0
    conn.execute(
        "UPDATE tasks SET status='failed', dead_letter=? WHERE id=?", (dead, task_id)
    )
    released = claims_mod.release_task(conn, task_id, by="reap", now=now)
    db.log_event(
        conn,
        kind="task.fail",
        now=now,
        actor=task.assignee,
        task_id=task_id,
        detail=f"{reason}（释放 {released} 条声明，attempts={task.attempts}"
        + ("，已进死信）" if dead else "）"),
    )
    return require(conn, task_id)


def requeue(conn: sqlite3.Connection, *, task_id: int) -> Task:
    """把回收下来的任务放回队列。死信任务需先由用户明确解除。"""
    now = utcnow()
    with db.transaction(conn):
        task = require(conn, task_id)
        if task.status is not TaskStatus.FAILED:
            raise InvalidState(f"任务 #{task_id} 当前是 {task.status}，不能重排")
        if task.dead_letter:
            raise InvalidState(
                f"任务 #{task_id} 在死信里，已连续失败 {task.attempts} 次",
                hint="确认原因后用 `pool.py queue --revive <id>` 解除死信再重排",
            )
        conn.execute(
            "UPDATE tasks SET status='queued', assignee=NULL, assigned_at=NULL, "
            "delivered_at=NULL WHERE id=?",
            (task_id,),
        )
        db.log_event(conn, kind="task.requeue", now=now, task_id=task_id)
    return require(conn, task_id)


def revive(conn: sqlite3.Connection, *, task_id: int) -> Task:
    """解除死信标记。只有用户能决定一个反复失败的任务还要不要再试。"""
    now = utcnow()
    with db.transaction(conn):
        require(conn, task_id)
        conn.execute(
            "UPDATE tasks SET dead_letter=0, attempts=0 WHERE id=?", (task_id,)
        )
        db.log_event(conn, kind="task.revive", now=now, task_id=task_id)
    return require(conn, task_id)


# --------------------------------------------------------------------------
# deliverables
# --------------------------------------------------------------------------


def get_deliverable(conn: sqlite3.Connection, deliverable_id: int) -> Deliverable | None:
    row = conn.execute(
        "SELECT * FROM deliverables WHERE id=?", (deliverable_id,)
    ).fetchone()
    return Deliverable.from_row(row) if row else None


def require_deliverable(conn: sqlite3.Connection, deliverable_id: int) -> Deliverable:
    item = get_deliverable(conn, deliverable_id)
    if item is None:
        raise NotFound(f"交付记录 #{deliverable_id} 不存在")
    return item


def latest_deliverable(conn: sqlite3.Connection, task_id: int) -> Deliverable | None:
    row = conn.execute(
        "SELECT * FROM deliverables WHERE task_id=? ORDER BY round DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    return Deliverable.from_row(row) if row else None


def deliverables_of(conn: sqlite3.Connection, task_id: int) -> list[Deliverable]:
    rows = conn.execute(
        "SELECT * FROM deliverables WHERE task_id=? ORDER BY round", (task_id,)
    ).fetchall()
    return [Deliverable.from_row(r) for r in rows]
