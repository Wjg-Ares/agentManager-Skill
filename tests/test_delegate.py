"""`delegate` = add + dispatch 一步到位。

存在的理由不是省几个字符，是**降摩擦**：主 agent 自己动手是这套编排最主要的
失效方式，而它事后给的理由是「派出去还要建任务、写交付，我直接查一下更快」。
正确的路比错误的路贵，它就会走错的那条。

强制在 hook 那边（guard 拒绝主 agent 改业务文件），这条命令负责另一半。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import fresh_db, register_worker  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from poolkit import ledger  # noqa: E402
from poolkit.commands import delegate as delegate_cmd  # noqa: E402
from poolkit.errors import UsageError  # noqa: E402
from poolkit.models import TaskStatus  # noqa: E402


class Args:
    def __init__(self, **kw) -> None:
        self.title = kw.get("title", "把 X 梳理一遍")
        self.detail = kw.get("detail")
        self.priority = kw.get("priority")
        self.to = kw.get("to")


class Ctx:
    def __init__(self, conn) -> None:
        self._conn = conn
        self.as_json = False

    def connect(self, *, create: bool = False):
        return self._conn


class DelegateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn, _ = fresh_db()
        self.ctx = Ctx(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def test_creates_and_dispatches_in_one_call(self) -> None:
        register_worker(self.conn, "worker-1")
        result = delegate_cmd.run(self.ctx, Args())
        task_id = result.data["task"]["id"]
        task = ledger.require(self.conn, task_id)
        self.assertIs(task.status, TaskStatus.ASSIGNED)
        self.assertEqual(task.assignee, "worker-1")

    def test_output_carries_the_brief_to_send(self) -> None:
        """派活的原文要一并给出来，主 agent 不必自己组织措辞。"""
        register_worker(self.conn, "worker-1")
        result = delegate_cmd.run(self.ctx, Args(detail="只读盘点，别改文件"))
        self.assertIn("sess-worker-1", result.text)
        self.assertIn("只读盘点，别改文件", result.text)

    def test_honours_explicit_target(self) -> None:
        register_worker(self.conn, "worker-1")
        register_worker(self.conn, "worker-2")
        result = delegate_cmd.run(self.ctx, Args(to="worker-2"))
        self.assertEqual(
            ledger.require(self.conn, result.data["task"]["id"]).assignee, "worker-2"
        )

    def test_no_worker_online_refuses_without_creating(self) -> None:
        """派不出去就别建任务。

        留下一串「建了没派成」的孤儿，主 agent 看到命令失败多半就自己动手了 ——
        正是这条命令要防的事。
        """
        with self.assertRaises(UsageError) as caught:
            delegate_cmd.run(self.ctx, Args())
        self.assertIn("一个都没注册", caught.exception.hint)
        self.assertIn("不要自己动手", caught.exception.hint)
        self.assertEqual(ledger.queue(self.conn), [])

    def test_all_slots_busy_refuses_without_creating(self) -> None:
        """「都忙着」和「没人上线」是两回事，提示不能混。"""
        register_worker(self.conn, "worker-1")
        busy = ledger.create(self.conn, title="占着").id
        ledger.dispatch(self.conn, task_id=busy, worker="worker-1")
        with self.assertRaises(UsageError) as caught:
            delegate_cmd.run(self.ctx, Args())
        self.assertIn("slot 全占着", caught.exception.message)
        self.assertEqual([t.id for t in ledger.queue(self.conn)], [])


if __name__ == "__main__":
    unittest.main()
