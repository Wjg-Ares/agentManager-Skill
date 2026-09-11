"""连接管理、schema 初始化、user_version 迁移。

零依赖：只用 Python 自带的 sqlite3（实测引擎 3.45.1），用户无需安装任何数据库。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Final

from . import config
from .errors import NotSetUp

# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------

SCHEMA_V1: Final = """
-- 角色注册：role 主键 → worker 重启后重新注册就是一次 UPDATE，天然幂等
CREATE TABLE registry (
  role          TEXT PRIMARY KEY,
  session_id    TEXT NOT NULL,
  session_name  TEXT NOT NULL,
  pid           INTEGER,
  cwd           TEXT,
  status        TEXT NOT NULL DEFAULT 'alive'
                CHECK (status IN ('alive','dead')),
  registered_at TEXT NOT NULL,
  last_seen_at  TEXT NOT NULL
);
-- 一个会话不能同时占两个角色
CREATE UNIQUE INDEX ux_registry_session ON registry(session_id);

-- 任务与队列同表：status='queued' 即在队列中，不另建队列结构
CREATE TABLE tasks (
  id            INTEGER PRIMARY KEY,
  title         TEXT NOT NULL,
  detail        TEXT,
  status        TEXT NOT NULL
                CHECK (status IN ('queued','assigned','delivered','rejected',
                                  'approved','failed','cancelled')),
  priority      INTEGER NOT NULL DEFAULT 100,   -- 越小越先出队
  assignee      TEXT REFERENCES registry(role),
  attempts      INTEGER NOT NULL DEFAULT 0,
  dead_letter   INTEGER NOT NULL DEFAULT 0,
  created_at    TEXT NOT NULL,
  assigned_at   TEXT,     -- 开始标记（§4.1 双标记之一）
  delivered_at  TEXT,
  approved_at   TEXT,     -- 结束标记（§4.1 双标记之二）
  cancelled_at  TEXT
);
CREATE INDEX ix_tasks_queue    ON tasks(priority, created_at) WHERE status='queued';
CREATE INDEX ix_tasks_assignee ON tasks(assignee, status);

-- 交付：摘要拆列，格式由程序保证；content 留库，消息只传 id + 摘要
CREATE TABLE deliverables (
  id            INTEGER PRIMARY KEY,
  task_id       INTEGER NOT NULL REFERENCES tasks(id),
  worker        TEXT NOT NULL REFERENCES registry(role),
  round         INTEGER NOT NULL DEFAULT 1,   -- 打回重交是第 2 轮，历史全留
  s_changed     TEXT NOT NULL,
  s_impact      TEXT NOT NULL,
  s_risk        TEXT NOT NULL,
  s_confirm     TEXT NOT NULL,
  s_files       TEXT NOT NULL,
  content       TEXT NOT NULL,
  created_at    TEXT NOT NULL
);
CREATE INDEX ix_deliverables_task ON deliverables(task_id, round);

-- 文件声明，即锁
CREATE TABLE claims (
  id            INTEGER PRIMARY KEY,
  task_id       INTEGER NOT NULL REFERENCES tasks(id),
  worker        TEXT NOT NULL REFERENCES registry(role),
  file_path     TEXT NOT NULL,   -- 规范化绝对路径，锁的 key
  display_path  TEXT NOT NULL,   -- 声明时的原样，给人看
  scope         TEXT,            -- 选填：方法/region，只供协商，不参与锁判定
  claimed_at    TEXT NOT NULL,
  released_at   TEXT,
  released_by   TEXT
);
-- 仲裁交给数据库：同一文件同时只能有一个活跃声明，INSERT 冲突即代表已被占用。
-- 这比「读出全部声明→比时间戳→再写入」可靠，且天然没有竞态窗口。
CREATE UNIQUE INDEX ux_claims_active ON claims(file_path) WHERE released_at IS NULL;
CREATE INDEX ix_claims_task ON claims(task_id);

-- 审计。故意不建外键：任务清理之后，审计仍要活着。
CREATE TABLE events (
  id            INTEGER PRIMARY KEY,
  at            TEXT NOT NULL,
  actor         TEXT,
  session_id    TEXT,
  kind          TEXT NOT NULL,
  task_id       INTEGER,
  detail        TEXT
);
CREATE INDEX ix_events_at   ON events(at);
CREATE INDEX ix_events_task ON events(task_id);

-- 配置 KV
CREATE TABLE settings (
  key         TEXT PRIMARY KEY,
  value       TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);
"""


def _migrate_0_to_1(conn: sqlite3.Connection) -> None:
    # 事务写在脚本里而不是用 transaction()：executescript 执行前会隐式提交
    # 挂起的事务，外层包 BEGIN 反而会让随后的 COMMIT 落空。
    conn.executescript(f"BEGIN;\n{SCHEMA_V1}\nPRAGMA user_version=1;\nCOMMIT;")


#: from_version -> 迁移函数。加字段是必然会发生的事，路径第一版就留出来。
#:
#: 每个迁移函数自己负责两件事：原子性，以及把 user_version 推到下一个数。
MIGRATIONS: Final[dict[int, Callable[[sqlite3.Connection], None]]] = {
    0: _migrate_0_to_1,
}


# --------------------------------------------------------------------------
# 连接
# --------------------------------------------------------------------------


def connect(path: str | Path, *, create: bool = False) -> sqlite3.Connection:
    """打开连接并设好三个必须的 PRAGMA。

    - ``journal_mode=WAL``：多进程并发读写的前提，没有它读会阻塞写
    - ``busy_timeout``：否则并发下直接抛 database is locked
    - ``foreign_keys=ON``：**SQLite 默认是关的**，不显式打开外键约束形同虚设

    ``isolation_level=None`` 关掉 Python 那层隐式事务管理，事务边界由
    :func:`transaction` 显式控制 —— 隐式的那套在 DDL 与并发下行为难以预料。
    """
    db_file = Path(path)
    if not create and not db_file.exists():
        raise NotSetUp()
    db_file.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_file), isolation_level=None, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={config.BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """写事务。

    用 ``BEGIN IMMEDIATE`` 而不是裸 ``BEGIN``：后者先拿读锁、写时再升级，
    而**升级失败是立刻抛 SQLITE_BUSY 的，不会等 busy_timeout**。
    并发下这会变成随机失败，是 SQLite 多进程写最经典的坑。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def schema_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def migrate(conn: sqlite3.Connection) -> int:
    """把库升到最新 schema，返回最终版本。重复执行安全。"""
    version = schema_version(conn)
    while version in MIGRATIONS:
        MIGRATIONS[version](conn)
        advanced = schema_version(conn)
        if advanced <= version:
            raise RuntimeError(
                f"迁移 {version} 执行后 user_version 没有前进（仍是 {advanced}），已中止"
            )
        version = advanced
    return version


# --------------------------------------------------------------------------
# settings 表
# --------------------------------------------------------------------------


def get_setting(conn: sqlite3.Connection, key: str) -> str:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if row is None:
        return config.DEFAULT_SETTINGS[key]
    return row["value"]


def get_int_setting(conn: sqlite3.Connection, key: str) -> int:
    return int(get_setting(conn, key))


def set_setting(conn: sqlite3.Connection, key: str, value: str, now: str) -> None:
    conn.execute(
        "INSERT INTO settings(key, value, updated_at) VALUES (?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, value, now),
    )


def all_settings(conn: sqlite3.Connection) -> dict[str, str]:
    merged = dict(config.DEFAULT_SETTINGS)
    for row in conn.execute("SELECT key, value FROM settings"):
        merged[row["key"]] = row["value"]
    return merged


# --------------------------------------------------------------------------
# 审计
# --------------------------------------------------------------------------


def log_event(
    conn: sqlite3.Connection,
    *,
    kind: str,
    now: str,
    actor: str | None = None,
    session_id: str | None = None,
    task_id: int | None = None,
    detail: str | None = None,
) -> None:
    """写一条审计。调用方须已在事务中 —— 审计与业务变更必须同生共死。"""
    conn.execute(
        "INSERT INTO events(at, actor, session_id, kind, task_id, detail) VALUES (?,?,?,?,?,?)",
        (now, actor, session_id, kind, task_id, detail),
    )
