"""`/am-setup --reset` 清空账本。

最要紧的一条：**有 worker 活着时必须拒绝**。它们是独立进程，主窗口关了照样在
改代码；这时候清掉 claims，三个 worker 就在同一个目录里裸奔了，覆盖丢代码正是
这套东西唯一要防的事故。
"""

from __future__ import annotations

import argparse
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import fresh_db, register_worker  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from poolkit import claims, ledger  # noqa: E402
from poolkit.cli import Context  # noqa: E402
from poolkit.commands import setup as setup_cmd  # noqa: E402
from poolkit.errors import UsageError  # noqa: E402
from poolkit.liveness import LiveSession  # noqa: E402


def a_session(session_id: str) -> LiveSession:
    return LiveSession(
        session_id=session_id, name=f"win-{session_id}", pid=1, cwd=".", status="idle"
    )


class ResetTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn, self.path = fresh_db()
        register_worker(self.conn, "worker-1")
        register_worker(self.conn, "main")
        task = ledger.create(self.conn, title="在办的活").id
        ledger.dispatch(self.conn, task_id=task, worker="worker-1")
        claims.declare(
            self.conn, task_id=task, worker="worker-1", paths=["D:/p/A.cs"]
        )

    def tearDown(self) -> None:
        self.conn.close()

    def _reset(self, *, force: bool = False, live: list[str] | None = None):
        ctx = Context(project_root=self.path.parent, session_id="sid-main", as_json=False)
        ctx._conn = self.conn
        args = argparse.Namespace(reset=True, force=force, role="main")
        sessions = [a_session(s) for s in (live or [])]
        with patch.object(setup_cmd.liveness, "list_sessions", lambda: sessions), patch.object(
            setup_cmd.liveness, "self_session", lambda _sid: None
        ):
            return setup_cmd.run(ctx, args)

    def test_refuses_while_a_worker_is_alive(self) -> None:
        with self.assertRaises(UsageError) as caught:
            self._reset(live=["sid-worker-1"])
        self.assertIn("worker-1", str(caught.exception))
        # 什么都不许删
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) c FROM tasks").fetchone()["c"], 1
        )
        self.assertEqual(len(claims.active_all(self.conn)), 1)

    def test_refuses_when_probe_unavailable(self) -> None:
        """探不到不等于都死了 —— 宁可拦住让用户确认。"""
        ctx = Context(project_root=self.path.parent, session_id="sid-main", as_json=False)
        ctx._conn = self.conn
        args = argparse.Namespace(reset=True, force=False, role="main")
        with patch.object(setup_cmd.liveness, "list_sessions", lambda: None):
            with self.assertRaises(UsageError):
                setup_cmd.run(ctx, args)

    def test_clears_everything_when_workers_are_gone(self) -> None:
        result = self._reset(live=[])
        self.assertTrue(result.data["reset"])
        self.assertEqual(result.data["before"]["active_claims"], 1)
        for table in ("tasks", "deliverables", "claims", "events"):
            with self.subTest(table=table):
                remaining = self.conn.execute(
                    f"SELECT COUNT(*) c FROM {table}"
                ).fetchone()["c"]
                if table == "events":
                    continue  # reset 自己会记一条
                self.assertEqual(remaining, 0)

    def test_force_overrides_the_guard(self) -> None:
        result = self._reset(force=True, live=["sid-worker-1"])
        self.assertEqual(result.data["live_workers"], ["worker-1"])
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) c FROM tasks").fetchone()["c"], 0
        )

    def test_settings_survive(self) -> None:
        """配置得留着，不然每次 reset 都要重设 scratch_dir。"""
        from poolkit import db as db_mod

        with db_mod.transaction(self.conn):
            db_mod.set_setting(
                self.conn, "scratch_dir", r"D:\claude-tmp", "2026-01-01T00:00:00Z"
            )
        self._reset(live=[])
        self.assertEqual(
            db_mod.get_setting(self.conn, "scratch_dir"), r"D:\claude-tmp"
        )


if __name__ == "__main__":
    unittest.main()
