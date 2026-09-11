"""「没有 worker 上线」不能说成「slot 全占着」。

真实事故：--reset 之后注册表是空的，`add` 却输出「slot 全占着，已排队」，
主 agent 据此判断无人可派，转头自己把活干了 —— 绕过了文件锁、交付记录和审批，
整套编排从这一步失效。

两种状态的解法完全相反：没人上线要让用户开窗口注册，都忙着才是入队等 slot。
"""

from __future__ import annotations

import argparse
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import fresh_db, register_worker  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from poolkit import ledger  # noqa: E402
from poolkit.cli import Context  # noqa: E402
from poolkit.commands import add as add_cmd  # noqa: E402
from poolkit.commands import status as status_cmd  # noqa: E402
from poolkit.models import Summary  # noqa: E402


class NoWorkerOnlineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn, self.path = fresh_db()

    def tearDown(self) -> None:
        self.conn.close()

    def _ctx(self) -> Context:
        ctx = Context(project_root=self.path.parent, session_id="sid-main", as_json=False)
        ctx._conn = self.conn
        return ctx

    def _add(self, title: str = "活儿"):
        return add_cmd.run(
            self._ctx(),
            argparse.Namespace(title=title, detail=None, priority=None),
        )

    def _status(self):
        return status_cmd.run(
            self._ctx(), argparse.Namespace(claims=False, check_live=False)
        )

    def _fill_all_slots(self) -> None:
        for i in range(1, 4):
            register_worker(self.conn, f"worker-{i}")
            task = ledger.create(self.conn, title=f"占位{i}").id
            ledger.dispatch(self.conn, task_id=task, worker=f"worker-{i}")

    # ---- add ----

    def test_add_says_nobody_online_not_all_busy(self) -> None:
        result = self._add()
        self.assertEqual(result.data["registered_workers"], 0)
        self.assertIn("还没有任何 worker 上线", result.text)
        self.assertNotIn("slot 全占着", result.text)
        self.assertIn("register worker-1", result.text)
        # 必须明确制止主 agent 自己上
        self.assertIn("不要自己做这个任务", result.text)

    def test_add_says_all_busy_when_really_busy(self) -> None:
        self._fill_all_slots()
        result = self._add()
        self.assertEqual(result.data["registered_workers"], 3)
        self.assertIn("slot 全占着", result.text)
        self.assertNotIn("还没有任何 worker 上线", result.text)

    def test_add_suggests_dispatch_when_free(self) -> None:
        register_worker(self.conn, "worker-1")
        result = self._add()
        self.assertIn("worker-1", result.data["free_slots"])
        self.assertIn("dispatch", result.text)

    # ---- status ----

    def test_status_says_nobody_online(self) -> None:
        ledger.create(self.conn, title="排队的")
        result = self._status()
        self.assertIn("还没有任何 worker 上线", result.text)
        self.assertNotIn("slot 全占着", result.text)

    def test_status_says_all_busy_when_really_busy(self) -> None:
        self._fill_all_slots()
        ledger.create(self.conn, title="排队的")
        result = self._status()
        self.assertIn("slot 全占着", result.text)
        self.assertNotIn("还没有任何 worker 上线", result.text)

    def test_idle_status_still_warns_against_diy(self) -> None:
        """没有任何待办时，也要提醒别自己动手。"""
        register_worker(self.conn, "worker-1")
        result = self._status()
        self.assertIn("别自己动手做", result.text)

    def test_status_after_reset_scenario(self) -> None:
        """复现那次事故：本来有 worker 在干活，reset 清空后再看 status。"""
        register_worker(self.conn, "worker-1")
        task = ledger.create(self.conn, title="旧活").id
        ledger.dispatch(self.conn, task_id=task, worker="worker-1")
        ledger.deliver(
            self.conn,
            task_id=task,
            worker="worker-1",
            summary=Summary(changed="x", impact="x", risk="无", confirm="无", files="x"),
            content="x",
        )
        ledger.approve(self.conn, task_id=task)

        for table in ("claims", "deliverables", "tasks", "registry", "events"):
            self.conn.execute(f"DELETE FROM {table}")

        result = self._add("reset 之后的新需求")
        self.assertIn("还没有任何 worker 上线", result.text)


if __name__ == "__main__":
    unittest.main()
