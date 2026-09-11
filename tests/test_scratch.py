"""临时文件的默认去处：`<项目根>/.claude/am/tmp`。

用户不该为「草稿纸放哪」配置任何东西。放项目里天然隔离、跟着项目走、
`.claude/am/` 本来就在 .gitignore 里，所以 setup 直接定好并建出来。
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import fresh_db, register_worker  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from poolkit import config, db, guard, ledger  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
POOL = REPO / "scripts" / "pool.py"


def run_setup(root: Path, *extra: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "CLAUDE_PLUGIN_ROOT": str(REPO)}
    return subprocess.run(
        [sys.executable, str(POOL), "--project-root", str(root), "setup", *extra],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=180,
    )


class DefaultScratchDirTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="am-scratch-")).resolve()

    def _scratch(self) -> str:
        conn = db.connect(config.db_path(self.root))
        try:
            return db.get_setting(conn, "scratch_dir")
        finally:
            conn.close()

    def test_setup_picks_a_default_without_being_asked(self) -> None:
        proc = run_setup(self.root)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            Path(self._scratch()), config.default_scratch_dir(self.root)
        )

    def test_default_lives_inside_the_project(self) -> None:
        run_setup(self.root)
        scratch = Path(self._scratch())
        self.assertEqual(scratch, self.root / ".claude" / "am" / "tmp")
        # 顺带保证它落在已被 .gitignore 排除的 .claude/am/ 下面
        self.assertIn(".claude", scratch.parts)
        self.assertIn("am", scratch.parts)

    def test_directory_is_actually_created(self) -> None:
        """光写配置不建目录，agent 往里放第一个文件就会失败。"""
        run_setup(self.root)
        self.assertTrue(Path(self._scratch()).is_dir())

    def test_explicit_flag_wins(self) -> None:
        custom = self.root / "my-scratch"
        run_setup(self.root, "--scratch-dir", str(custom))
        self.assertEqual(Path(self._scratch()), custom)
        self.assertTrue(custom.is_dir())

    def test_existing_choice_is_not_clobbered_by_rerun(self) -> None:
        """改过之后重复跑 setup，不能被默认值顶掉。"""
        custom = self.root / "my-scratch"
        run_setup(self.root, "--scratch-dir", str(custom))
        run_setup(self.root)
        self.assertEqual(Path(self._scratch()), custom)


class ProjectRootDetectionTest(unittest.TestCase):
    """从库的位置反推项目根 —— scratch 规则靠它区分「项目文件」和「Temp 垃圾」。"""

    def test_standard_layout_yields_the_project_root(self) -> None:
        conn, path = fresh_db()
        try:
            self.assertEqual(
                guard._project_root_of(conn),
                config.normalize_path(path.parent.parent.parent),
            )
        finally:
            conn.close()

    def test_non_standard_layout_yields_none(self) -> None:
        """库不在 <项目根>/.claude/am/pool.db 上时，宁可说「不知道」。

        盲目往上爬三级会推出个高得离谱的目录（实测过：AppData\\Local），
        它底下的一切都会被当成项目文件放行 —— 而且是静默失效，没人会发现。
        """
        stray = Path(tempfile.mkdtemp(prefix="am-stray-")) / "pool.db"
        conn = db.connect(stray, create=True)
        db.migrate(conn)
        try:
            self.assertIsNone(guard._project_root_of(conn))
        finally:
            conn.close()

    def test_scratch_rule_still_bites_when_root_is_unknown(self) -> None:
        """推不出项目根时，拦截规则必须照常生效，不能因此放行。"""
        stray = Path(tempfile.mkdtemp(prefix="am-stray2-")) / "pool.db"
        conn = db.connect(stray, create=True)
        db.migrate(conn)
        try:
            register_worker(conn, "worker-1")
            task = ledger.create(conn, title="活儿").id
            ledger.dispatch(conn, task_id=task, worker="worker-1")
            with db.transaction(conn):
                db.set_setting(
                    conn, "scratch_dir", r"D:\somewhere", "2026-01-01T00:00:00Z"
                )
            decision = guard.check_edit(
                conn,
                session_id="sid-worker-1",
                file_path=r"C:\Windows\Temp\junk.py",
            )
            self.assertFalse(decision.allowed)
        finally:
            conn.close()


class GuardUsesProjectScratchTest(unittest.TestCase):
    """拦下系统 Temp 时，提示的应当是项目内那个目录。"""

    def setUp(self) -> None:
        self.conn, self.db_file = fresh_db()
        register_worker(self.conn, "worker-1")
        task = ledger.create(self.conn, title="活儿").id
        ledger.dispatch(self.conn, task_id=task, worker="worker-1")
        self.project = self.db_file.parent.parent.parent
        with db.transaction(self.conn):
            db.set_setting(
                self.conn,
                "scratch_dir",
                str(config.default_scratch_dir(self.project)),
                "2026-01-01T00:00:00Z",
            )

    def tearDown(self) -> None:
        self.conn.close()

    def test_denial_points_at_the_project_directory(self) -> None:
        decision = guard.check_edit(
            self.conn,
            session_id="sid-worker-1",
            file_path=r"C:\Users\ADMINI~1\AppData\Local\Temp\claude\scan.py",
        )
        self.assertFalse(decision.allowed)
        self.assertIn("am", decision.reason)
        self.assertIn("tmp", decision.reason)

    def test_project_files_are_never_mistaken_for_temp_garbage(self) -> None:
        """项目本身就放在系统 Temp 下时（在临时目录里试东西），项目内的文件
        不该被这条规则拦住 —— 否则他整个项目都动不了，连锁判定都轮不到。

        这里的临时库正是 mkdtemp 建的，天然就是这个场景。
        """
        decision = guard.check_edit(
            self.conn,
            session_id="sid-worker-1",
            file_path=str(self.project / "src" / "A.cs"),
        )
        self.assertTrue(decision.allowed, decision.reason)

    def test_writing_into_the_scratch_dir_is_allowed(self) -> None:
        target = config.default_scratch_dir(self.project) / "scan.py"
        decision = guard.check_edit(
            self.conn, session_id="sid-worker-1", file_path=str(target)
        )
        self.assertTrue(decision.allowed)


if __name__ == "__main__":
    unittest.main()
