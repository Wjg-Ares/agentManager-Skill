"""hook 判定，以及那条「guard 不准 import liveness」的硬约束。

约束用 AST 检查强制，不靠注释 —— 注释拦不住三个月后手滑的一次 import。
"""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import fresh_db, register_worker  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "am-setup" / "scripts"))

from poolkit import claims, guard, ledger  # noqa: E402

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "am-setup" / "scripts"


def imported_names(path: Path) -> set[str]:
    """一个模块 import 了哪些名字（含函数体里的局部 import）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[-1] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module.split(".")[-1])
            names.update(alias.name for alias in node.names)
    return names


class HotPathConstraintTest(unittest.TestCase):
    def test_guard_never_imports_liveness(self) -> None:
        """热路径不准碰存活探针。

        `claude agents --json` 要 1 秒以上；挂在 check_edit 上等于给用户的
        每一次编辑都加一秒。这条一旦破了，整套东西会慢到没人用。
        """
        names = imported_names(SCRIPTS / "poolkit" / "guard.py")
        self.assertNotIn("liveness", names)

    def test_guard_never_spawns_a_subprocess(self) -> None:
        names = imported_names(SCRIPTS / "poolkit" / "guard.py")
        for banned in ("subprocess", "shutil"):
            self.assertNotIn(banned, names, f"热路径不该出现 {banned}")

    def test_config_stays_at_the_bottom(self) -> None:
        """config 是依赖树底层，不准反向依赖任何 poolkit 模块。"""
        tree = ast.parse((SCRIPTS / "poolkit" / "config.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                self.assertNotEqual(node.level, 1, "config 不该有相对 import")


class CheckEditTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn, _ = fresh_db()
        register_worker(self.conn, "worker-1")
        register_worker(self.conn, "worker-2")
        self.t1 = ledger.create(self.conn, title="任务一").id
        self.t2 = ledger.create(self.conn, title="任务二").id
        ledger.dispatch(self.conn, task_id=self.t1, worker="worker-1")
        ledger.dispatch(self.conn, task_id=self.t2, worker="worker-2")

    def tearDown(self) -> None:
        self.conn.close()

    def test_unregistered_session_is_never_blocked(self) -> None:
        """临时开的普通会话不受这套约束。"""
        decision = guard.check_edit(
            self.conn, session_id="不认识的会话", file_path="D:/p/A.cs"
        )
        self.assertTrue(decision.allowed)

    def test_holder_can_edit_own_file(self) -> None:
        claims.declare(self.conn, task_id=self.t1, worker="worker-1", paths=["D:/p/A.cs"])
        decision = guard.check_edit(
            self.conn, session_id="sid-worker-1", file_path="D:/p/A.cs"
        )
        self.assertTrue(decision.allowed)

    def test_other_worker_is_blocked_and_told_where_to_go(self) -> None:
        claims.declare(self.conn, task_id=self.t1, worker="worker-1", paths=["D:/p/A.cs"])
        decision = guard.check_edit(
            self.conn, session_id="sid-worker-2", file_path="D:/p/A.cs"
        )
        self.assertFalse(decision.allowed)
        self.assertIn("worker-1", decision.reason)
        # 必须给出会话名，那是协商用的地址
        self.assertIn("sess-worker-1", decision.reason)

    def test_undeclared_file_gets_auto_claimed(self) -> None:
        """没声明就改 → 自动补一条声明。靠自觉必然会漏，一漏锁就形同虚设。"""
        decision = guard.check_edit(
            self.conn, session_id="sid-worker-1", file_path="D:/p/New.cs"
        )
        self.assertTrue(decision.allowed)
        self.assertIn("已自动", decision.note)

        holder = claims.active_for(self.conn, guard.config.normalize_path("D:/p/New.cs"))
        self.assertIsNotNone(holder)
        self.assertEqual(holder.worker, "worker-1")

        # 补出来的声明一样挡得住别人
        other = guard.check_edit(
            self.conn, session_id="sid-worker-2", file_path="D:/p/New.cs"
        )
        self.assertFalse(other.allowed)

    def test_am_internal_files_are_exempt(self) -> None:
        """账本自己的文件不参与锁判定，否则 am 写库会被自己拦住。"""
        decision = guard.check_edit(
            self.conn, session_id="sid-worker-2", file_path="D:/p/.claude/am/pool.db"
        )
        self.assertTrue(decision.allowed)

    def test_worker_without_task_is_allowed_but_warned(self) -> None:
        ledger.deliver(
            self.conn,
            task_id=self.t1,
            worker="worker-1",
            summary=__import__("poolkit.models", fromlist=["Summary"]).Summary(
                changed="x", impact="x", risk="无", confirm="无", files="x"
            ),
            content="x",
        )
        ledger.approve(self.conn, task_id=self.t1)
        decision = guard.check_edit(
            self.conn, session_id="sid-worker-1", file_path="D:/p/Free.cs"
        )
        self.assertTrue(decision.allowed)
        self.assertIn("没有在办任务", decision.note)


class CheckBashTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn, _ = fresh_db()
        register_worker(self.conn, "worker-1")
        register_worker(self.conn, "main")

    def tearDown(self) -> None:
        self.conn.close()

    def test_worker_cannot_build(self) -> None:
        for command in ("dotnet build", "dotnet test --filter X", "msbuild App.sln"):
            with self.subTest(command=command):
                decision = guard.check_bash(
                    self.conn, session_id="sid-worker-1", command=command
                )
                self.assertFalse(decision.allowed)

    def test_worker_cannot_write_git(self) -> None:
        for command in ("git commit -m x", "git checkout main", "git stash"):
            with self.subTest(command=command):
                decision = guard.check_bash(
                    self.conn, session_id="sid-worker-1", command=command
                )
                self.assertFalse(decision.allowed)

    def test_worker_can_read_git(self) -> None:
        for command in ("git diff", "git log --oneline", "git status"):
            with self.subTest(command=command):
                decision = guard.check_bash(
                    self.conn, session_id="sid-worker-1", command=command
                )
                self.assertTrue(decision.allowed)

    def test_chained_command_is_still_caught(self) -> None:
        """`cd src && dotnet build` 必须同样被拦 —— 只看开头是拦不住的。"""
        decision = guard.check_bash(
            self.conn, session_id="sid-worker-1", command="cd src && dotnet build -c Release"
        )
        self.assertFalse(decision.allowed)

    def test_main_agent_is_exempt(self) -> None:
        decision = guard.check_bash(
            self.conn, session_id="sid-main", command="dotnet build"
        )
        self.assertTrue(decision.allowed)

    def test_denial_points_at_the_main_agent(self) -> None:
        decision = guard.check_bash(
            self.conn, session_id="sid-worker-1", command="dotnet build"
        )
        self.assertIn("sess-main", decision.reason)


if __name__ == "__main__":
    unittest.main()
