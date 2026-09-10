"""存活探针（**慢路径**）。

要调 `claude agents --json` 子进程，实测 1 秒以上。只准 `reap` 与 `register`
这类低频命令调用 —— **guard.py 不准 import 本模块**，见 guard.py 顶部说明。

worker 崩溃后开始标记永远等不到结束标记，slot 会被永久占用；死掉的 worker
留下的文件声明也会挡住所有人。这里就是那条兜底路径。
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass, field
from datetime import timedelta

from . import claims as claims_mod
from . import db, ledger, registry
from .models import TaskStatus, WorkerStatus, parse_ts, utcnow

#: 子进程超时。探针本身卡住比探不出来更糟。
PROBE_TIMEOUT_S = 20


@dataclass(slots=True)
class LiveSession:
    session_id: str
    name: str
    pid: int | None
    cwd: str | None
    status: str


def list_sessions() -> list[LiveSession] | None:
    """当前活着的 Claude Code 会话。探测失败返回 None（区别于「一个都没有」）。

    返回 None 时调用方**必须放弃回收** —— 探不到不等于都死了，
    据此回收会把活着的 worker 的任务抢走，造成双写。
    """
    exe = os.environ.get("CLAUDE_CODE_EXECPATH") or shutil.which("claude")
    if not exe:
        return None
    try:
        proc = subprocess.run(
            [exe, "agents", "--json"],
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_S,
            encoding="utf-8",
            errors="replace",
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    try:
        raw = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, list):
        return None
    return [
        LiveSession(
            session_id=str(item.get("sessionId", "")),
            name=str(item.get("name", "")),
            pid=item.get("pid"),
            cwd=item.get("cwd"),
            status=str(item.get("status", "")),
        )
        for item in raw
        if isinstance(item, dict) and item.get("sessionId")
    ]


def self_session(session_id: str) -> LiveSession | None:
    """本会话在活跃列表里的那一条。

    session_id 由环境变量 ``CLAUDE_CODE_SESSION_ID`` 直接可得，
    但**会话名只能从这里查** —— 所以注册时查一次存进 registry，
    之后 guard 走库，热路径不再碰 CLI。
    """
    sessions = list_sessions()
    if sessions is None:
        return None
    for item in sessions:
        if item.session_id == session_id:
            return item
    return None


def session_name_of(session_id: str) -> str | None:
    item = self_session(session_id)
    return item.name if item else None


@dataclass(slots=True)
class ReapResult:
    probed: bool  # 探针是否成功
    dead_roles: list[str] = field(default_factory=list)
    timed_out: list[int] = field(default_factory=list)  # 超时回收的任务
    recovered_tasks: list[int] = field(default_factory=list)
    released_claims: int = 0
    note: str = ""


def reap(conn: sqlite3.Connection) -> ReapResult:
    """回收死掉的 worker 与卡死的任务。

    两条独立的回收依据：

    1. **进程已死** —— 注册的 session_id 不在活跃会话列表里
    2. **超时未交付** —— 派活超过 ``deliver_timeout_min`` 仍是 assigned

    第 2 条只对**已确认死亡**的 worker 生效。原 worker 还活着就抢走它的任务
    会造成双写，那正是这套锁要防的事（§9「重派前须确认原 worker 确已死亡」）。
    """
    result = ReapResult(probed=False)
    sessions = list_sessions()
    if sessions is None:
        result.note = "探针不可用（找不到 claude 可执行文件或调用失败），本次不回收任何 slot"
        return result

    result.probed = True
    alive_ids = {s.session_id for s in sessions}
    timeout_min = db.get_int_setting(conn, "deliver_timeout_min")
    now = utcnow()
    now_dt = parse_ts(now)

    with db.transaction(conn):
        for reg in registry.workers(conn):
            if reg.session_id in alive_ids:
                if reg.status is not WorkerStatus.ALIVE:
                    registry.set_status(conn, reg.role, WorkerStatus.ALIVE, now=now)
                registry.touch(conn, reg.role, now=now)
                continue

            # 确认已死
            if reg.status is WorkerStatus.ALIVE:
                registry.set_status(conn, reg.role, WorkerStatus.DEAD, now=now)
            result.dead_roles.append(reg.role)

            task = ledger.open_task_of(conn, reg.role)
            if task is not None and task.status is not TaskStatus.DELIVERED:
                # 已交付的不回收：产出已经落库了，等用户审批即可
                ledger.fail(conn, task_id=task.id, reason=f"{reg.role} 会话已消失")
                result.recovered_tasks.append(task.id)
                if _overdue(task.assigned_at, now_dt, timeout_min):
                    result.timed_out.append(task.id)

            result.released_claims += claims_mod.release_worker(
                conn, reg.role, by="reap", now=now
            )

        db.log_event(
            conn,
            kind="reap",
            now=now,
            detail=f"死亡角色 {result.dead_roles or '无'}，回收任务 {result.recovered_tasks or '无'}",
        )
    return result


def overdue_tasks(conn: sqlite3.Connection) -> list[tuple[int, int]]:
    """超时但 worker 还活着的任务，返回 [(task_id, 已耗分钟)]。

    只报不动手 —— worker 活着就说明它可能只是在干慢活，抢任务的判断留给用户。
    """
    timeout_min = db.get_int_setting(conn, "deliver_timeout_min")
    now_dt = parse_ts(utcnow())
    out: list[tuple[int, int]] = []
    rows = conn.execute(
        "SELECT id, assigned_at FROM tasks WHERE status='assigned' AND assigned_at IS NOT NULL"
    ).fetchall()
    for row in rows:
        elapsed = now_dt - parse_ts(row["assigned_at"])
        if elapsed >= timedelta(minutes=timeout_min):
            out.append((row["id"], int(elapsed.total_seconds() // 60)))
    return out


def _overdue(assigned_at: str | None, now_dt, timeout_min: int) -> bool:
    if not assigned_at:
        return False
    return (now_dt - parse_ts(assigned_at)) >= timedelta(minutes=timeout_min)
