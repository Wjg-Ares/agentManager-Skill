"""`/am-worker` 空参数时的提示。

要点：**只提示，不替用户决定**。程序负责把「哪些编号空着、该敲什么」算清楚，
挑哪个是用户的事。
"""

from __future__ import annotations

import argparse
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import fresh_db, register_worker  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from poolkit.cli import Context  # noqa: E402
from poolkit.commands import whoami  # noqa: E402


class WhoamiHintTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn, self.path = fresh_db()

    def tearDown(self) -> None:
        self.conn.close()

    def _run(self, session_id: str):
        ctx = Context(project_root=self.path.parent, session_id=session_id, as_json=False)
        ctx._conn = self.conn
        return whoami.run(ctx, argparse.Namespace(session_id=None))

    def test_suggests_first_free_slot(self) -> None:
        result = self._run("fresh")
        self.assertFalse(result.data["registered"])
        self.assertEqual(result.data["suggested"], "worker-1")
        self.assertEqual(result.data["free_roles"], ["worker-1", "worker-2", "worker-3"])
        self.assertIn("register worker-1", result.text)

    def test_skips_taken_slots(self) -> None:
        register_worker(self.conn, "worker-1")
        result = self._run("fresh")
        self.assertEqual(result.data["suggested"], "worker-2")
        self.assertIn("cdc", result.text.lower().replace("sess-worker-1", "cdc"))
        self.assertIn("register worker-2", result.text)

    def test_does_not_register_anything(self) -> None:
        """提示就是提示 —— 跑完之后注册表必须还是空的。"""
        self._run("fresh")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) c FROM registry").fetchone()["c"], 0
        )

    def test_full_slots_point_at_reap(self) -> None:
        for i in range(1, 4):
            register_worker(self.conn, f"worker-{i}")
        result = self._run("fresh")
        self.assertIsNone(result.data["suggested"])
        self.assertIn("reap", result.text)

    def test_registered_session_reports_its_own_role(self) -> None:
        register_worker(self.conn, "worker-2")
        result = self._run("sid-worker-2")
        self.assertTrue(result.data["registered"])
        self.assertEqual(result.data["role"], "worker-2")


if __name__ == "__main__":
    unittest.main()
