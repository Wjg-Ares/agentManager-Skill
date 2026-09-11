"""任务状态流转、slot 占用、队列与审批联动。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import fresh_db, register_worker  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from poolkit import claims, db, ledger  # noqa: E402
from poolkit.errors import Conflict, InvalidState  # noqa: E402
from poolkit.models import Summary, TaskStatus  # noqa: E402


def a_summary() -> Summary:
    return Summary(changed="改了 A", impact="A.cs", risk="无", confirm="无", files="A.cs")


class FlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn, _ = fresh_db()
        register_worker(self.conn, "worker-1")
        register_worker(self.conn, "worker-2")

    def tearDown(self) -> None:
        self.conn.close()

    def test_new_task_goes_to_queue(self) -> None:
        task = ledger.create(self.conn, title="活儿")
        self.assertIs(task.status, TaskStatus.QUEUED)
        self.assertEqual([t.id for t in ledger.queue(self.conn)], [task.id])

    def test_full_happy_path(self) -> None:
        task = ledger.create(self.conn, title="活儿")
        ledger.dispatch(self.conn, task_id=task.id, worker="worker-1")
        self.assertIs(ledger.require(self.conn, task.id).status, TaskStatus.ASSIGNED)

        claims.declare(self.conn, task_id=task.id, worker="worker-1", paths=["D:/p/A.cs"])
        item = ledger.deliver(
            self.conn,
            task_id=task.id,
            worker="worker-1",
            summary=a_summary(),
            content="完整产出" * 100,
        )
        self.assertIs(ledger.require(self.conn, task.id).status, TaskStatus.DELIVERED)

        result = ledger.approve(self.conn, task_id=task.id)
        self.assertIs(result.task.status, TaskStatus.APPROVED)
        self.assertEqual(result.released, 1, "审批通过要释放该任务的文件声明")
        self.assertIn("worker-1", result.free_slots)
        # 完整产出留在库里，靠 ID 取
        self.assertEqual(len(ledger.require_deliverable(self.conn, item.id).content), 400)

    def test_slot_stays_occupied_until_user_is_happy(self) -> None:
        """用户没说满意，slot 就一直占着 —— 这是保护追问上下文的机制。"""
        first = ledger.create(self.conn, title="第一件")
        second = ledger.create(self.conn, title="第二件")
        ledger.dispatch(self.conn, task_id=first.id, worker="worker-1")
        ledger.deliver(
            self.conn, task_id=first.id, worker="worker-1",
            summary=a_summary(), content="x",
        )
        # 已交付但没审批，slot 不算空
        self.assertNotIn("worker-1", [s.role for s in ledger.free_slots(self.conn)])
        with self.assertRaises(Conflict):
            ledger.dispatch(self.conn, task_id=second.id, worker="worker-1")

        ledger.approve(self.conn, task_id=first.id)
        self.assertIn("worker-1", [s.role for s in ledger.free_slots(self.conn)])

    def test_reject_keeps_slot_and_claims(self) -> None:
        """打回不放 slot、不释放锁 —— 换人的话追问上下文就没了。"""
        task = ledger.create(self.conn, title="活儿")
        ledger.dispatch(self.conn, task_id=task.id, worker="worker-1")
        claims.declare(self.conn, task_id=task.id, worker="worker-1", paths=["D:/p/A.cs"])
        ledger.deliver(
            self.conn, task_id=task.id, worker="worker-1",
            summary=a_summary(), content="x",
        )
        ledger.reject(self.conn, task_id=task.id, reason="再改改")

        self.assertIs(ledger.require(self.conn, task.id).status, TaskStatus.REJECTED)
        self.assertNotIn("worker-1", [s.role for s in ledger.free_slots(self.conn)])
        self.assertEqual(len(claims.active_for_task(self.conn, task.id)), 1)

    def test_redeliver_keeps_history(self) -> None:
        task = ledger.create(self.conn, title="活儿")
        ledger.dispatch(self.conn, task_id=task.id, worker="worker-1")
        first = ledger.deliver(
            self.conn, task_id=task.id, worker="worker-1",
            summary=a_summary(), content="第一版",
        )
        ledger.reject(self.conn, task_id=task.id, reason="不行")
        second = ledger.deliver(
            self.conn, task_id=task.id, worker="worker-1",
            summary=a_summary(), content="第二版",
        )
        self.assertEqual((first.round, second.round), (1, 2))
        self.assertEqual(len(ledger.deliverables_of(self.conn, task.id)), 2)
        self.assertEqual(ledger.latest_deliverable(self.conn, task.id).content, "第二版")

    def test_cannot_approve_undelivered(self) -> None:
        task = ledger.create(self.conn, title="活儿")
        ledger.dispatch(self.conn, task_id=task.id, worker="worker-1")
        with self.assertRaises(InvalidState):
            ledger.approve(self.conn, task_id=task.id)

    def test_cannot_deliver_someone_elses_task(self) -> None:
        task = ledger.create(self.conn, title="活儿")
        ledger.dispatch(self.conn, task_id=task.id, worker="worker-1")
        with self.assertRaises(Conflict):
            ledger.deliver(
                self.conn, task_id=task.id, worker="worker-2",
                summary=a_summary(), content="x",
            )

    def test_approve_returns_the_queue(self) -> None:
        """审批把队列待办一起带回来 —— 队列推进就是靠这个绑到「用户说满意」上的。"""
        done = ledger.create(self.conn, title="做完的")
        ledger.create(self.conn, title="排队的甲")
        ledger.create(self.conn, title="排队的乙")
        ledger.dispatch(self.conn, task_id=done.id, worker="worker-1")
        ledger.deliver(
            self.conn, task_id=done.id, worker="worker-1",
            summary=a_summary(), content="x",
        )
        result = ledger.approve(self.conn, task_id=done.id)
        self.assertEqual([t.title for t in result.queue], ["排队的甲", "排队的乙"])

    def test_priority_reorders_the_queue(self) -> None:
        first = ledger.create(self.conn, title="先来的")
        later = ledger.create(self.conn, title="后来的")
        self.assertEqual([t.id for t in ledger.queue(self.conn)], [first.id, later.id])
        ledger.set_priority(self.conn, task_id=later.id, priority=1)
        self.assertEqual([t.id for t in ledger.queue(self.conn)], [later.id, first.id])

    def test_cancel_is_soft_and_frees_claims(self) -> None:
        task = ledger.create(self.conn, title="不做了")
        ledger.dispatch(self.conn, task_id=task.id, worker="worker-1")
        claims.declare(self.conn, task_id=task.id, worker="worker-1", paths=["D:/p/A.cs"])
        ledger.cancel(self.conn, task_id=task.id, reason="用户删了")
        self.assertIs(ledger.require(self.conn, task.id).status, TaskStatus.CANCELLED)
        self.assertIsNotNone(ledger.require(self.conn, task.id).cancelled_at)
        self.assertEqual(claims.active_for_task(self.conn, task.id), [])

    def test_dead_letter_after_max_attempts(self) -> None:
        """派发满 max_attempts 次仍失败 → 进死信，停止自动重派。"""
        db.set_setting(self.conn, "max_attempts", "2", "2026-01-01T00:00:00Z")
        task = ledger.create(self.conn, title="老失败")

        # 第 1 次派发失败：还能重排
        ledger.dispatch(self.conn, task_id=task.id, worker="worker-1")
        with db.transaction(self.conn):
            ledger.fail(self.conn, task_id=task.id, reason="崩了")
        self.assertFalse(ledger.require(self.conn, task.id).dead_letter)
        ledger.requeue(self.conn, task_id=task.id)

        # 第 2 次派发失败：到上限了
        ledger.dispatch(self.conn, task_id=task.id, worker="worker-1")
        with db.transaction(self.conn):
            ledger.fail(self.conn, task_id=task.id, reason="又崩了")

        self.assertTrue(ledger.require(self.conn, task.id).dead_letter)
        with self.assertRaises(InvalidState):
            ledger.requeue(self.conn, task_id=task.id)
        # 只有用户能决定要不要再试
        ledger.revive(self.conn, task_id=task.id)
        ledger.requeue(self.conn, task_id=task.id)
        self.assertIs(ledger.require(self.conn, task.id).status, TaskStatus.QUEUED)

    def test_dispatch_requires_registered_alive_worker(self) -> None:
        task = ledger.create(self.conn, title="活儿")
        from poolkit.errors import NotFound

        with self.assertRaises(NotFound):
            ledger.dispatch(self.conn, task_id=task.id, worker="worker-3")


if __name__ == "__main__":
    unittest.main()
