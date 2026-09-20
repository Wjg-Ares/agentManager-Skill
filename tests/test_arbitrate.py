"""撞锁仲裁。

重点不在「模型答得准不准」——那不是这里能测的。重点在**答不出来的时候会怎样**，
以及**答得模棱两可的时候会怎样**：这条命令的全部价值前提是它永远不会把人卡住，
也永远不会凭一个半信半疑的概率去促成一次交接。
"""

from __future__ import annotations

import argparse
import sys
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import fresh_db, register_worker  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from poolkit import claims, guard, jev, ledger  # noqa: E402
from poolkit.commands import arbitrate  # noqa: E402
from poolkit.errors import UsageError  # noqa: E402

FILE = "D:/p/CollectTask.cs"


@dataclass
class FakeCtx:
    """命令只用到这两样。"""

    conn: object
    session_id: str | None
    as_json: bool = False

    def connect(self, *, create: bool = False):
        return self.conn


def args_for(scope: str | None = None, file: str = FILE) -> argparse.Namespace:
    return argparse.Namespace(file=file, scope=scope)


def answer(probability: float) -> dict:
    return {"same_region": {"type": "noul", "noul": probability}}


class ArbitrateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn, _ = fresh_db()
        register_worker(self.conn, "worker-1")
        register_worker(self.conn, "worker-2")
        self.t1 = ledger.create(self.conn, title="改采集保存").id
        self.t2 = ledger.create(self.conn, title="改采集读取").id
        ledger.dispatch(self.conn, task_id=self.t1, worker="worker-1")
        ledger.dispatch(self.conn, task_id=self.t2, worker="worker-2")
        # worker-1 先占住文件，worker-2 是被拦下来的那个
        self.ctx = FakeCtx(self.conn, session_id="sid-worker-2")

    def tearDown(self) -> None:
        self.conn.close()

    def hold(self, scope: str | None) -> None:
        claims.declare(
            self.conn, task_id=self.t1, worker="worker-1", paths=[FILE], scope=scope
        )

    # ---------------------------------------------------------------- 不必问

    def test_no_holder_says_go_ahead(self) -> None:
        with mock.patch.object(jev, "ask", side_effect=AssertionError("不该问")):
            result = arbitrate.run(self.ctx, args_for("Save 方法"))
        self.assertEqual(result.data["verdict"], "no_holder")

    def test_lock_already_mine(self) -> None:
        claims.declare(self.conn, task_id=self.t2, worker="worker-2", paths=[FILE])
        with mock.patch.object(jev, "ask", side_effect=AssertionError("不该问")):
            result = arbitrate.run(self.ctx, args_for("Save 方法"))
        self.assertEqual(result.data["verdict"], "already_mine")

    def test_missing_scope_never_calls_the_model(self) -> None:
        """缺范围是程序自己就能判的事，不该花一次调用去问。"""
        self.hold(scope=None)
        with mock.patch.object(jev, "ask", side_effect=AssertionError("不该问")):
            result = arbitrate.run(self.ctx, args_for("Save 方法"))
        self.assertEqual(result.data["verdict"], "undecided")

        self.conn.execute("UPDATE claims SET scope='Load 方法' WHERE worker='worker-1'")
        with mock.patch.object(jev, "ask", side_effect=AssertionError("不该问")):
            result = arbitrate.run(self.ctx, args_for(scope=None))
        self.assertEqual(result.data["verdict"], "undecided")

    def test_main_agent_is_turned_away(self) -> None:
        self.hold(scope="Save 方法")
        register_worker(self.conn, "main")
        ctx = FakeCtx(self.conn, session_id="sid-main")
        with self.assertRaises(UsageError):
            arbitrate.run(ctx, args_for("Load 方法"))

    # ---------------------------------------------------------------- 判定

    def test_high_probability_is_a_real_conflict(self) -> None:
        self.hold(scope="CollectTask.Save 方法")
        with mock.patch.object(jev, "ask", return_value=answer(0.96)):
            result = arbitrate.run(self.ctx, args_for("CollectTask.Save 里的事务处理"))
        self.assertEqual(result.data["verdict"], "real_conflict")
        self.assertEqual(result.exit_code, 0)

    def test_low_probability_suggests_handoff_by_the_holder(self) -> None:
        self.hold(scope="CollectTask.Save 方法")
        with mock.patch.object(jev, "ask", return_value=answer(0.03)):
            result = arbitrate.run(self.ctx, args_for("CollectTask.Load 方法"))
        self.assertEqual(result.data["verdict"], "false_conflict")
        # 建议里必须是「由持有者执行 handoff」，不能是这条命令自己把锁挪走
        self.assertIn("handoff", result.data["suggested_command"])
        self.assertIn("--to worker-2", result.data["suggested_command"])
        self.assertIn("由它执行", result.text)
        # 锁一动不动 —— 这条命令只出建议
        holder = claims.active_for(self.conn, guard.config.normalize_path(FILE))
        self.assertEqual(holder.worker, "worker-1")

    def test_middle_band_refuses_to_decide(self) -> None:
        self.hold(scope="CollectTask.Save 方法")
        with mock.patch.object(jev, "ask", return_value=answer(0.5)):
            result = arbitrate.run(self.ctx, args_for("CollectTask 的日志"))
        self.assertEqual(result.data["verdict"], "undecided")

    def test_thresholds_are_asymmetric(self) -> None:
        """误判成假冲突会真的丢代码，误判成真冲突只是白等 —— 两侧不能一样松。

        30% 这个把握，换算到「真冲突」那侧（70%）早就判了；在「假冲突」这侧必须还不够。
        """
        self.hold(scope="CollectTask.Save 方法")
        with mock.patch.object(jev, "ask", return_value=answer(0.30)):
            result = arbitrate.run(self.ctx, args_for("CollectTask.Load 方法"))
        self.assertEqual(result.data["verdict"], "undecided")
        self.assertLess(arbitrate._SURE_DIFFERENT, 1 - arbitrate._SURE_SAME)

    # ---------------------------------------------------------------- 降级

    def test_unavailable_falls_back_and_still_exits_zero(self) -> None:
        """没 key、断网、返回结构不对 —— 一律退回人工协商，绝不报错。"""
        self.hold(scope="CollectTask.Save 方法")
        with mock.patch.object(
            jev, "ask", side_effect=jev.JevUnavailable("连不上")
        ):
            result = arbitrate.run(self.ctx, args_for("CollectTask.Load 方法"))
        self.assertEqual(result.data["verdict"], "undecided")
        self.assertEqual(result.exit_code, 0)
        # 退回去的必须是原来那套：会话名 + handoff 路子
        self.assertIn("sess-worker-1", result.text)
        self.assertIn("handoff", result.text)

    def test_garbled_answer_is_treated_as_unavailable(self) -> None:
        self.hold(scope="CollectTask.Save 方法")
        with mock.patch.object(jev, "ask", return_value={"same_region": {"type": "noul"}}):
            result = arbitrate.run(self.ctx, args_for("CollectTask.Load 方法"))
        self.assertEqual(result.data["verdict"], "undecided")


class HotPathTest(unittest.TestCase):
    """热路径一次都不准联网。"""

    def setUp(self) -> None:
        self.conn, _ = fresh_db()
        register_worker(self.conn, "worker-1")
        register_worker(self.conn, "worker-2")
        t1 = ledger.create(self.conn, title="任务一").id
        t2 = ledger.create(self.conn, title="任务二").id
        ledger.dispatch(self.conn, task_id=t1, worker="worker-1")
        ledger.dispatch(self.conn, task_id=t2, worker="worker-2")
        claims.declare(self.conn, task_id=t1, worker="worker-1", paths=[FILE])

    def tearDown(self) -> None:
        self.conn.close()

    def test_check_edit_never_calls_the_model(self) -> None:
        """撞锁的提示里会提到 arbitrate，但 hook 自己一次都不准去问。"""
        with mock.patch.object(jev, "ask", side_effect=AssertionError("热路径联网了")):
            decision = guard.check_edit(
                self.conn, session_id="sid-worker-2", file_path=FILE
            )
        self.assertFalse(decision.allowed)

    def test_check_bash_never_calls_the_model(self) -> None:
        with mock.patch.object(jev, "ask", side_effect=AssertionError("热路径联网了")):
            guard.check_bash(
                self.conn, session_id="sid-worker-2", command="git commit -m x"
            )


if __name__ == "__main__":
    unittest.main()
