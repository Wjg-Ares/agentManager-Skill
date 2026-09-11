"""hook 入口的端到端行为，以及它为提速而复制的那点逻辑的一致性。

hook 里为了避开 `import poolkit` 的开销，内联了一份「向上找账本」的查找。
复制逻辑是有代价的 —— 这里用测试把两边钉在一起，改了一边另一边会红。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import register_worker  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "am-setup" / "scripts"))

from poolkit import claims, config, db, ledger  # noqa: E402

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = PLUGIN_ROOT / "skills" / "am-setup"
HOOK = SKILL_ROOT / "scripts" / "hooks" / "pretooluse.py"


def run_hook(payload: dict) -> dict:
    env = {**os.environ, "CLAUDE_PLUGIN_ROOT": str(PLUGIN_ROOT), "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=30,
    )
    assert proc.returncode == 0, f"hook 必须永远 exit 0，实际 {proc.returncode}"
    return json.loads(proc.stdout or "{}")


def is_denied(result: dict) -> bool:
    return (
        result.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"
    )


class LedgerLookupConsistencyTest(unittest.TestCase):
    def test_inline_path_matches_config(self) -> None:
        """hook 内联的 `_LEDGER_PARTS` 必须和 config 里的锚点一致。"""
        sys.path.insert(0, str(HOOK.parent))
        import importlib.util

        spec = importlib.util.spec_from_file_location("_hook_mod", HOOK)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        self.assertEqual(module._LEDGER_PARTS, config.ROOT_ANCHORS[0])
        self.assertEqual(
            os.path.join(*module._LEDGER_PARTS),
            os.path.join(*config.AM_DIR_PARTS, config.DB_FILENAME),
        )


class HookBehaviourTest(unittest.TestCase):
    """跑真的子进程 —— hook 就是这么被调用的，import 路径、编码、退出码都要真跑才算数。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(tempfile.mkdtemp(prefix="am-hook-"))
        conn = db.connect(config.db_path(cls.root), create=True)
        db.migrate(conn)
        register_worker(conn, "worker-1")
        register_worker(conn, "worker-2")
        register_worker(conn, "main")
        t1 = ledger.create(conn, title="任务一").id
        t2 = ledger.create(conn, title="任务二").id
        ledger.dispatch(conn, task_id=t1, worker="worker-1")
        ledger.dispatch(conn, task_id=t2, worker="worker-2")
        cls.locked = str(cls.root / "src" / "Locked.cs")
        claims.declare(conn, task_id=t1, worker="worker-1", paths=[cls.locked])
        conn.close()

    def _edit(self, session_id: str, file_path: str, cwd: str | None = None) -> dict:
        return run_hook(
            {
                "tool_name": "Edit",
                "session_id": session_id,
                "cwd": cwd or str(self.root),
                "tool_input": {"file_path": file_path},
            }
        )

    def _bash(self, session_id: str, command: str) -> dict:
        return run_hook(
            {
                "tool_name": "Bash",
                "session_id": session_id,
                "cwd": str(self.root),
                "tool_input": {"command": command},
            }
        )

    def test_denies_other_workers_file(self) -> None:
        result = self._edit("sid-worker-2", self.locked)
        self.assertTrue(is_denied(result))
        self.assertIn("sess-worker-1", result["systemMessage"])

    def test_allows_holder(self) -> None:
        self.assertEqual(self._edit("sid-worker-1", self.locked), {})

    def test_denies_worker_build(self) -> None:
        self.assertTrue(is_denied(self._bash("sid-worker-1", "cd src && dotnet build")))

    def test_allows_main_build(self) -> None:
        self.assertEqual(self._bash("sid-main", "dotnet build"), {})

    def test_allows_read_only_git(self) -> None:
        self.assertEqual(self._bash("sid-worker-1", "git status"), {})

    def test_unrelated_project_is_allowed(self) -> None:
        other = Path(tempfile.mkdtemp(prefix="am-unrelated-"))
        result = self._edit("sid-worker-2", str(other / "x.txt"), cwd=str(other))
        self.assertEqual(result, {})

    def test_unknown_tool_is_allowed(self) -> None:
        result = run_hook(
            {
                "tool_name": "Read",
                "session_id": "sid-worker-2",
                "cwd": str(self.root),
                "tool_input": {"file_path": self.locked},
            }
        )
        self.assertEqual(result, {})

    def test_garbage_input_never_blocks(self) -> None:
        """hook 自身出问题绝不能卡住用户的编辑。"""
        env = {**os.environ, "CLAUDE_PLUGIN_ROOT": str(PLUGIN_ROOT)}
        proc = subprocess.run(
            [sys.executable, str(HOOK)],
            input="这不是 JSON",
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(json.loads(proc.stdout or "{}"), {})


if __name__ == "__main__":
    unittest.main()
