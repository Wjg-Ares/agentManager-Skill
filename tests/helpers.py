"""测试公共脚手架。"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from poolkit import db, registry  # noqa: E402


def fresh_db() -> tuple[sqlite3.Connection, Path]:
    """建一个真实的临时库文件。

    不用 `:memory:` —— 这套东西的要害就是多进程共享一个文件，
    内存库测不出 WAL、busy_timeout 和部分唯一索引在文件上的真实行为。
    """
    tmp = Path(tempfile.mkdtemp(prefix="am-test-")) / "pool.db"
    conn = db.connect(tmp, create=True)
    db.migrate(conn)
    return conn, tmp


def register_worker(conn: sqlite3.Connection, role: str) -> None:
    registry.register(
        conn,
        role=role,
        session_id=f"sid-{role}",
        session_name=f"sess-{role}",
        pid=1000,
        cwd="D:/workspace/demo",
    )
