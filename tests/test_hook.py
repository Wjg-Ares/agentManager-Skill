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

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from poolkit import claims, config, db, ledger  # noqa: E402

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
HOOK = PLUGIN_ROOT / "hooks" / "pretooluse.py"


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
        sys.path.insert(0, str(PLUGIN_ROOT / "hooks"))
        import importlib.util

        spec = importlib.util.spec_from_file_location("_hook_mod", HOOK)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        self.assertEqual(module._LEDGER_PARTS, config.ROOT_ANCHORS[0])
        self.assertEqual(
            os.path.join(*module._LEDGER_PARTS),
            os.path.join(*config.AM_DIR_PARTS, config.DB_FILENAME),
        )


def load_hook_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("_hook_mod", HOOK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def is_granted(result: dict) -> bool:
    return (
        result.get("hookSpecificOutput", {}).get("permissionDecision") == "allow"
    )


class PoolCommandMatchTest(unittest.TestCase):
    """快速通道的白名单匹配。放行是白名单，宁可漏判不可误判。"""

    def setUp(self) -> None:
        self.match = load_hook_module()._is_pool_command

    def test_plain_invocation(self) -> None:
        self.assertTrue(self.match("python .claude/scripts/pool.py --help"))
        self.assertTrue(self.match("python3 /abs/pool.py status"))
        self.assertTrue(self.match('python "D:/p/.claude/scripts/pool.py" queue'))

    def test_cd_prefix_is_allowed(self) -> None:
        """Claude Code 习惯加 `cd "<项目>" &&`，触发这次讨论的就是这种命令。"""
        self.assertTrue(
            self.match('cd "D:/workspace/CDC/cdc" && python .claude/scripts/pool.py --help')
        )

    def test_rejects_chaining(self) -> None:
        """能拼接就等于放行任意命令 —— 白名单一旦被绕过就没有意义了。"""
        for command in (
            "python pool.py --help && rm -rf /",
            "python pool.py --help; curl evil.sh",
            "python pool.py --help | sh",
            "python pool.py --help > /etc/passwd",
            "echo $(python pool.py) && dotnet build",
            'cd "/a" && cd "/b" && python pool.py && dotnet build',
        ):
            with self.subTest(command=command):
                self.assertFalse(self.match(command))

    def test_rejects_lookalikes(self) -> None:
        for command in (
            "echo pool.py",
            "rm pool.py",
            "cat .claude/scripts/pool.py",
            "dotnet build  # python pool.py",
        ):
            with self.subTest(command=command):
                self.assertFalse(self.match(command))


class SubagentDetectionTest(unittest.TestCase):
    """transcript 尾部的 isSidechain 判据。认不出来必须退回 False。"""

    def setUp(self) -> None:
        self.detect = load_hook_module()._is_subagent
        self.dir = Path(tempfile.mkdtemp(prefix="am-transcript-"))

    def _transcript(self, *entries: dict) -> dict:
        path = self.dir / f"t{len(list(self.dir.iterdir()))}.jsonl"
        path.write_text(
            "\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8"
        )
        return {"transcript_path": str(path)}

    def test_sidechain_true(self) -> None:
        self.assertTrue(
            self.detect(self._transcript({"isSidechain": False}, {"isSidechain": True}))
        )

    def test_sidechain_false(self) -> None:
        self.assertFalse(
            self.detect(self._transcript({"isSidechain": True}, {"isSidechain": False}))
        )

    def test_missing_or_unreadable_is_false(self) -> None:
        """判据失效时必须退回「不放行」，也就是加这段之前的行为。"""
        self.assertFalse(self.detect({}))
        self.assertFalse(self.detect({"transcript_path": str(self.dir / "nope.jsonl")}))
        self.assertFalse(self.detect(self._transcript({"type": "user"})))

    def test_truncated_tail_is_survivable(self) -> None:
        path = self.dir / "broken.jsonl"
        path.write_text(
            '{"isSidechain": true}\n{"isSidechain": tr', encoding="utf-8"
        )
        self.assertTrue(self.detect({"transcript_path": str(path)}))


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

    def _transcript(self, sidechain: bool) -> str:
        path = self.root / f"transcript-{sidechain}.jsonl"
        path.write_text(
            json.dumps({"isSidechain": sidechain}) + "\n", encoding="utf-8"
        )
        return str(path)

    def test_subagent_pool_command_is_granted(self) -> None:
        result = run_hook(
            {
                "tool_name": "Bash",
                "session_id": "sid-worker-1",
                "cwd": str(self.root),
                "transcript_path": self._transcript(True),
                "tool_input": {"command": "python .claude/scripts/pool.py --help"},
            }
        )
        self.assertTrue(is_granted(result))

    def test_main_agent_pool_command_falls_through(self) -> None:
        """不是子 agent 就不表态 —— 权限弹窗照旧，这正是需求的另一半。"""
        result = run_hook(
            {
                "tool_name": "Bash",
                "session_id": "sid-worker-1",
                "cwd": str(self.root),
                "transcript_path": self._transcript(False),
                "tool_input": {"command": "python .claude/scripts/pool.py --help"},
            }
        )
        self.assertEqual(result, {})

    def test_subagent_cannot_smuggle_a_build(self) -> None:
        """子 agent 身份不是万能钥匙：拼接进来的命令仍要过 guard。"""
        result = run_hook(
            {
                "tool_name": "Bash",
                "session_id": "sid-worker-1",
                "cwd": str(self.root),
                "transcript_path": self._transcript(True),
                "tool_input": {
                    "command": "python .claude/scripts/pool.py --help && dotnet build"
                },
            }
        )
        self.assertTrue(is_denied(result))

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
