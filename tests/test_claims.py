"""文件声明与仲裁 —— 整套里最要命的一块。

手工验证「两个 worker 抢同一个文件」很痛苦：要开两个窗口、卡时机。
写成测试就是几行，而且能覆盖手工根本试不出来的路径（跨进程真并发）。
"""

from __future__ import annotations

import multiprocessing
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import fresh_db, register_worker  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from poolkit import claims, config, db, ledger  # noqa: E402


class DeclareTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn, self.path = fresh_db()
        register_worker(self.conn, "worker-1")
        register_worker(self.conn, "worker-2")
        self.t1 = ledger.create(self.conn, title="任务一").id
        self.t2 = ledger.create(self.conn, title="任务二").id

    def tearDown(self) -> None:
        self.conn.close()

    def test_first_declare_wins(self) -> None:
        result = claims.declare(
            self.conn, task_id=self.t1, worker="worker-1", paths=["D:/proj/A.cs"]
        )
        self.assertTrue(result.ok)
        self.assertEqual(len(result.granted), 1)

    def test_second_worker_is_blocked(self) -> None:
        claims.declare(self.conn, task_id=self.t1, worker="worker-1", paths=["D:/proj/A.cs"])
        result = claims.declare(
            self.conn, task_id=self.t2, worker="worker-2", paths=["D:/proj/A.cs"]
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.conflicts[0].holder, "worker-1")
        # 拒绝理由里必须给出会话名 —— 那是对方的消息地址，协商流程的起点
        self.assertEqual(result.conflicts[0].holder_address, "sess-worker-1")

    def test_case_and_separator_differences_are_the_same_file(self) -> None:
        """`D:\\proj\\A.cs` 和 `d:/proj/a.cs` 在 Windows 上是同一个文件。

        不归一就会漏锁 —— 两个 worker 各拿一把「不同」的锁改同一个文件。
        """
        if sys.platform != "win32":
            self.skipTest("路径大小写归一是 Windows 特有的问题")
        claims.declare(self.conn, task_id=self.t1, worker="worker-1", paths=[r"D:\proj\A.cs"])
        result = claims.declare(
            self.conn, task_id=self.t2, worker="worker-2", paths=["d:/proj/a.cs"]
        )
        self.assertFalse(result.ok)

    def test_partial_conflict_rolls_back_everything(self) -> None:
        """一批里只要有一个冲突，整批都不给 —— 不留「以为拿到了其实没有」的状态。"""
        claims.declare(self.conn, task_id=self.t1, worker="worker-1", paths=["D:/proj/B.cs"])
        result = claims.declare(
            self.conn,
            task_id=self.t2,
            worker="worker-2",
            paths=["D:/proj/C.cs", "D:/proj/B.cs", "D:/proj/D.cs"],
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.granted, [])
        # C 和 D 也必须没被锁住
        self.assertIsNone(claims.active_for(self.conn, config.normalize_path("D:/proj/C.cs")))
        self.assertIsNone(claims.active_for(self.conn, config.normalize_path("D:/proj/D.cs")))

    def test_redeclare_is_idempotent(self) -> None:
        claims.declare(self.conn, task_id=self.t1, worker="worker-1", paths=["D:/proj/A.cs"])
        result = claims.declare(
            self.conn, task_id=self.t1, worker="worker-1", paths=["D:/proj/A.cs"]
        )
        self.assertTrue(result.ok)
        self.assertEqual(len(result.already_mine), 1)
        self.assertEqual(len(result.granted), 0)

    def test_release_lets_the_next_worker_in(self) -> None:
        claims.declare(self.conn, task_id=self.t1, worker="worker-1", paths=["D:/proj/A.cs"])
        with db.transaction(self.conn):
            claims.release_task(self.conn, self.t1, by="approve")
        result = claims.declare(
            self.conn, task_id=self.t2, worker="worker-2", paths=["D:/proj/A.cs"]
        )
        self.assertTrue(result.ok)

    def test_release_is_soft(self) -> None:
        """软删除：释放之后记录还在，事后能追溯当时谁占着。"""
        claims.declare(self.conn, task_id=self.t1, worker="worker-1", paths=["D:/proj/A.cs"])
        with db.transaction(self.conn):
            claims.release_task(self.conn, self.t1, by="approve")
        rows = self.conn.execute("SELECT * FROM claims").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertIsNotNone(rows[0]["released_at"])
        self.assertEqual(rows[0]["released_by"], "approve")

    def test_handoff_transfers_the_lock(self) -> None:
        claims.declare(self.conn, task_id=self.t1, worker="worker-1", paths=["D:/proj/A.cs"])
        claims.handoff(
            self.conn, file_path="D:/proj/A.cs", to_worker="worker-2", to_task=self.t2
        )
        holder = claims.active_for(self.conn, config.normalize_path("D:/proj/A.cs"))
        self.assertIsNotNone(holder)
        self.assertEqual(holder.worker, "worker-2")
        # 同一时刻只能有一个活跃声明
        active = self.conn.execute(
            "SELECT COUNT(*) c FROM claims WHERE file_path=? AND released_at IS NULL",
            (config.normalize_path("D:/proj/A.cs"),),
        ).fetchone()["c"]
        self.assertEqual(active, 1)


def _race_declare(args: tuple[str, str, int, str]) -> bool:
    """子进程里抢同一个文件，返回是否抢到。"""
    db_path, worker, task_id, target = args
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    from poolkit import claims as c
    from poolkit import db as d

    conn = d.connect(db_path)
    try:
        return c.declare(conn, task_id=task_id, worker=worker, paths=[target]).ok
    finally:
        conn.close()


class RealConcurrencyTest(unittest.TestCase):
    """真·多进程并发：这才是实际场景（每个 worker 是独立的 Claude Code 进程）。

    单进程里怎么测都测不出 WAL 与部分唯一索引在跨进程时的真实行为。
    """

    def test_only_one_of_four_processes_gets_the_file(self) -> None:
        conn, path = fresh_db()
        tasks = []
        for i in range(1, 5):
            register_worker(conn, f"worker-{i}")
            tasks.append(ledger.create(conn, title=f"任务{i}").id)
        conn.close()

        target = "D:/proj/Hot.cs"
        payload = [(str(path), f"worker-{i}", tasks[i - 1], target) for i in range(1, 5)]
        with multiprocessing.Pool(4) as pool:
            results = pool.map(_race_declare, payload)

        self.assertEqual(
            sum(1 for r in results if r),
            1,
            "同一个文件同时只能有一个 worker 拿到锁",
        )

        conn = db.connect(path)
        active = conn.execute(
            "SELECT COUNT(*) c FROM claims WHERE released_at IS NULL"
        ).fetchone()["c"]
        conn.close()
        self.assertEqual(active, 1)


if __name__ == "__main__":
    unittest.main()
