"""角色注册：稳定角色名 ↔ 当前会话名 ↔ session_id。

会话名（如 ``cdc-1f``）是启动时随机生成的，重开即变。账本若直接记会话名，
worker 一重启账本立刻失效。这一层就是把稳定的 ``worker-1`` 翻译成当前实际地址。

**本模块不 import liveness** —— 存活探针是慢路径，见 guard.py 顶部说明。
"""

from __future__ import annotations

import sqlite3

from . import config, db
from .errors import NotFound, UsageError
from .models import Registration, WorkerStatus, utcnow


def validate_role(role: str) -> str:
    if not config.ROLE_RE.match(role):
        raise UsageError(
            f"角色名不合法：{role!r}",
            hint="只能是 main 或 worker-1 / worker-2 / worker-3",
        )
    return role


def register(
    conn: sqlite3.Connection,
    *,
    role: str,
    session_id: str,
    session_name: str,
    pid: int | None = None,
    cwd: str | None = None,
) -> Registration:
    """登记（或重新登记）一个角色。幂等：同一角色重复注册是 UPDATE。

    同时清掉该 session_id 在**别的**角色上的残留登记 —— 一个会话只能占一个角色，
    否则 `/clear` 后改注册成别的角色会留下两条指向同一会话的记录。
    """
    validate_role(role)
    now = utcnow()
    with db.transaction(conn):
        conn.execute(
            "DELETE FROM registry WHERE session_id=? AND role<>?", (session_id, role)
        )
        conn.execute(
            """
            INSERT INTO registry(role, session_id, session_name, pid, cwd,
                                 status, registered_at, last_seen_at)
            VALUES (?,?,?,?,?, 'alive', ?, ?)
            ON CONFLICT(role) DO UPDATE SET
                session_id   = excluded.session_id,
                session_name = excluded.session_name,
                pid          = excluded.pid,
                cwd          = excluded.cwd,
                status       = 'alive',
                registered_at= excluded.registered_at,
                last_seen_at = excluded.last_seen_at
            """,
            (role, session_id, session_name, pid, cwd, now, now),
        )
        db.log_event(
            conn,
            kind="register",
            now=now,
            actor=role,
            session_id=session_id,
            detail=f"session_name={session_name}",
        )
    return require(conn, role)


def get(conn: sqlite3.Connection, role: str) -> Registration | None:
    row = conn.execute("SELECT * FROM registry WHERE role=?", (role,)).fetchone()
    return Registration.from_row(row) if row else None


def require(conn: sqlite3.Connection, role: str) -> Registration:
    reg = get(conn, role)
    if reg is None:
        raise NotFound(f"角色 {role} 未注册", hint="该会话需先执行 /am-worker register")
    return reg


def by_session(conn: sqlite3.Connection, session_id: str) -> Registration | None:
    """按 session_id 反查角色 —— guard 热路径靠这个判断「我是谁」。

    走 ``ux_registry_session`` 唯一索引，一次命中。
    """
    row = conn.execute(
        "SELECT * FROM registry WHERE session_id=?", (session_id,)
    ).fetchone()
    return Registration.from_row(row) if row else None


def workers(conn: sqlite3.Connection) -> list[Registration]:
    rows = conn.execute(
        "SELECT * FROM registry WHERE role LIKE 'worker-%' ORDER BY role"
    ).fetchall()
    return [Registration.from_row(r) for r in rows]


def all_roles(conn: sqlite3.Connection) -> list[Registration]:
    rows = conn.execute("SELECT * FROM registry ORDER BY role").fetchall()
    return [Registration.from_row(r) for r in rows]


def touch(conn: sqlite3.Connection, role: str, *, now: str | None = None) -> None:
    """刷新 last_seen_at。调用方须已在事务中。"""
    conn.execute(
        "UPDATE registry SET last_seen_at=? WHERE role=?", (now or utcnow(), role)
    )


def set_status(
    conn: sqlite3.Connection, role: str, status: WorkerStatus, *, now: str
) -> None:
    """标记存活状态。调用方须已在事务中（reap 要和回收动作同一个事务）。"""
    conn.execute(
        "UPDATE registry SET status=?, last_seen_at=? WHERE role=?",
        (str(status), now, role),
    )
    db.log_event(conn, kind=f"registry.{status}", now=now, actor=role)


def address_of(conn: sqlite3.Connection, role: str) -> str | None:
    """拿会话名当跨会话消息的地址用。

    撞锁时拒绝理由里要给出**持有者的会话名**（不是 session_id），
    对方拿到就能直接发消息过去协商，这是 §6.4 协商流程的起点。
    """
    reg = get(conn, role)
    return reg.session_name if reg else None
