"""测试公共脚手架。"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from poolkit import config, db, registry  # noqa: E402


def fresh_db() -> tuple[sqlite3.Connection, Path]:
    """建一个真实的临时库文件。

    不用 `:memory:` —— 这套东西的要害就是多进程共享一个文件，
    内存库测不出 WAL、busy_timeout 和部分唯一索引在文件上的真实行为。

    库放在**真实结构**上（`<项目根>/.claude/am/pool.db`）而不是随便一个临时文件 ——
    有些判定要从库的位置反推项目根，结构不对就测不出真实行为。
    """
    root = Path(tempfile.mkdtemp(prefix="am-test-")).resolve()
    path = config.db_path(root)
    conn = db.connect(path, create=True)
    db.migrate(conn)
    return conn, path


def register_worker(conn: sqlite3.Connection, role: str) -> None:
    registry.register(
        conn,
        role=role,
        session_id=f"sid-{role}",
        session_name=f"sess-{role}",
        pid=1000,
        cwd="D:/workspace/demo",
    )
