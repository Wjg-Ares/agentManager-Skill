"""文件声明与仲裁。

边界由 worker 自己声明，不由主 agent 划定 —— 主 agent 事前拆到文件级，
成本比自己干还高；worker 干着干着自然就知道要动哪些文件。
声明记录同时就是锁表，不必再维护第二份。

**仲裁交给数据库**：``ux_claims_active`` 这个部分唯一索引保证「同一文件同时
只能有一个活跃声明」，INSERT 冲突即代表已被占用。应用层不做「读出全部声明→
比时间戳→再写入」那一套，那有竞态窗口。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from . import config, db
from .models import Claim, utcnow


@dataclass(slots=True)
class ClaimConflict:
    """一次声明失败：想要的文件被别人占着。"""

    display_path: str
    holder: str  # 持有者角色名
    holder_address: str | None  # 持有者会话名，可直接当消息地址
    holder_task: int
    holder_scope: str | None


@dataclass(slots=True)
class DeclareResult:
    granted: list[Claim]
    already_mine: list[Claim]  # 重复声明，幂等返回
    conflicts: list[ClaimConflict]

    @property
    def ok(self) -> bool:
        return not self.conflicts


def active_for(conn: sqlite3.Connection, file_path: str) -> Claim | None:
    """查某个文件当前的活跃声明。**guard 热路径**，走唯一索引一次命中。

    ``file_path`` 必须是 :func:`config.normalize_path` 处理过的。
    """
    row = conn.execute(
        "SELECT * FROM claims WHERE file_path=? AND released_at IS NULL",
        (file_path,),
    ).fetchone()
    return Claim.from_row(row) if row else None


def active_all(conn: sqlite3.Connection) -> list[Claim]:
    rows = conn.execute(
        "SELECT * FROM claims WHERE released_at IS NULL ORDER BY worker, file_path"
    ).fetchall()
    return [Claim.from_row(r) for r in rows]


def active_for_task(conn: sqlite3.Connection, task_id: int) -> list[Claim]:
    rows = conn.execute(
        "SELECT * FROM claims WHERE task_id=? AND released_at IS NULL ORDER BY file_path",
        (task_id,),
    ).fetchall()
    return [Claim.from_row(r) for r in rows]


def declare(
    conn: sqlite3.Connection,
    *,
    task_id: int,
    worker: str,
    paths: list[str],
    scope: str | None = None,
) -> DeclareResult:
    """声明一批文件。支持增量追加 —— 干到一半发现要动新文件，再调一次即可。

    全部在一个事务里：要么整批拿到，要么一个都不拿。部分成功会让 worker
    处在「以为拿到了其实没有」的状态，比直接失败更危险。

    重复声明自己已持有的文件是**幂等**的，不报错。
    """
    now = utcnow()
    granted: list[Claim] = []
    already: list[Claim] = []
    conflicts: list[ClaimConflict] = []

    try:
        with db.transaction(conn):
            for raw in paths:
                key = config.normalize_path(raw)
                existing = active_for(conn, key)
                if existing is not None:
                    if existing.worker == worker:
                        already.append(existing)
                    else:
                        conflicts.append(
                            ClaimConflict(
                                display_path=raw,
                                holder=existing.worker,
                                holder_address=_address(conn, existing.worker),
                                holder_task=existing.task_id,
                                holder_scope=existing.scope,
                            )
                        )
                    continue
                cur = conn.execute(
                    """
                    INSERT INTO claims(task_id, worker, file_path, display_path,
                                       scope, claimed_at)
                    VALUES (?,?,?,?,?,?)
                    """,
                    (task_id, worker, key, raw, scope, now),
                )
                row = conn.execute(
                    "SELECT * FROM claims WHERE id=?", (cur.lastrowid,)
                ).fetchone()
                granted.append(Claim.from_row(row))

            if conflicts:
                # 整批回滚：不让 worker 处在「以为拿到了其实没有」的半成功状态
                raise _Rollback(
                    DeclareResult(granted=[], already_mine=already, conflicts=conflicts)
                )

            if granted:
                db.log_event(
                    conn,
                    kind="declare",
                    now=now,
                    actor=worker,
                    task_id=task_id,
                    detail=f"{len(granted)} 个文件："
                    + ", ".join(c.display_path for c in granted),
                )
    except _Rollback as rollback:
        return rollback.result

    return DeclareResult(granted=granted, already_mine=already, conflicts=conflicts)


class _Rollback(Exception):
    """内部信号：带着结果回滚事务。"""

    def __init__(self, result: DeclareResult) -> None:
        super().__init__("claim conflict")
        self.result = result


def release_task(
    conn: sqlite3.Connection, task_id: int, *, by: str, now: str | None = None
) -> int:
    """软释放某任务的全部声明，返回释放条数。调用方须已在事务中。

    软删除而非物理删除 —— 事后要能追溯「当时谁占着哪个文件」。
    """
    now = now or utcnow()
    cur = conn.execute(
        "UPDATE claims SET released_at=?, released_by=? "
        "WHERE task_id=? AND released_at IS NULL",
        (now, by, task_id),
    )
    return cur.rowcount


def release_worker(
    conn: sqlite3.Connection, worker: str, *, by: str, now: str | None = None
) -> int:
    """释放某 worker 的全部声明（worker 已死时回收用）。调用方须已在事务中。"""
    now = now or utcnow()
    cur = conn.execute(
        "UPDATE claims SET released_at=?, released_by=? "
        "WHERE worker=? AND released_at IS NULL",
        (now, by, worker),
    )
    return cur.rowcount


def handoff(
    conn: sqlite3.Connection,
    *,
    file_path: str,
    to_worker: str,
    to_task: int,
    scope: str | None = None,
) -> Claim:
    """交接一把锁：协商判定为假冲突（改的是不同 region）时用。

    先软释放旧声明再插新的，两步同一事务 —— 中间有空窗的话第三方会插进来。
    """
    key = config.normalize_path(file_path)
    now = utcnow()
    with db.transaction(conn):
        current = active_for(conn, key)
        if current is not None:
            conn.execute(
                "UPDATE claims SET released_at=?, released_by=? WHERE id=?",
                (now, "handoff", current.id),
            )
        cur = conn.execute(
            """
            INSERT INTO claims(task_id, worker, file_path, display_path, scope, claimed_at)
            VALUES (?,?,?,?,?,?)
            """,
            (to_task, to_worker, key, file_path, scope, now),
        )
        db.log_event(
            conn,
            kind="handoff",
            now=now,
            actor=to_worker,
            task_id=to_task,
            detail=f"{file_path} ← {current.worker if current else '(无持有者)'}",
        )
        row = conn.execute("SELECT * FROM claims WHERE id=?", (cur.lastrowid,)).fetchone()
    return Claim.from_row(row)


def _address(conn: sqlite3.Connection, role: str) -> str | None:
    """持有者的会话名。不 import registry，避免环依赖，这里直接查一行。"""
    row = conn.execute("SELECT session_name FROM registry WHERE role=?", (role,)).fetchone()
    return row["session_name"] if row else None
