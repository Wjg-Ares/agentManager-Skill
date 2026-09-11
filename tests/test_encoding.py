"""GBK 控制台下输出不能崩。

真实故障：Windows 控制台是 cp936，脚本输出里的 ✓ 直接抛 UnicodeEncodeError。
`setup` 其实已经执行成功了，只是结果打不出来 —— 看着像失败，用户会重跑。

hook 那边更糟：它输出的 JSON 带中文拒绝理由，崩掉就等于这次 Edit 失去保护，
而用户还以为锁在生效。

所以两个入口都在启动时把 stdout/stderr 强制成 UTF-8，不依赖用户去设
PYTHONIOENCODING。下面的用例就是把那个环境变量设成 gbk 来复现原始故障。
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

REPO = Path(__file__).resolve().parents[1]
POOL = REPO / "scripts" / "pool.py"
HOOK = REPO / "hooks" / "pretooluse.py"


def gbk_env() -> dict[str, str]:
    """模拟 GBK 控制台。去掉 CLAUDE_PLUGIN_ROOT，免得被外部环境干扰。"""
    env = {**os.environ, "PYTHONIOENCODING": "gbk"}
    env.pop("CLAUDE_PLUGIN_ROOT", None)
    return env


class CliEncodingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="am-enc-"))

    def test_setup_output_survives_gbk_console(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(POOL), "--project-root", str(self.root), "setup"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=gbk_env(),
            timeout=180,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # 成功执行还不够 —— 结果必须真的打出来
        self.assertIn("✓", proc.stdout)
        self.assertIn("账本已就绪", proc.stdout)

    def test_error_path_survives_gbk_console(self) -> None:
        """报错走 stderr，同样带 ✗ 和中文。"""
        proc = subprocess.run(
            [sys.executable, str(POOL), "--project-root", str(self.root), "status"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=gbk_env(),
            timeout=120,
        )
        self.assertNotEqual(proc.returncode, 0)  # 还没 setup
        self.assertIn("✗", proc.stderr)


class HookEncodingTest(unittest.TestCase):
    """hook 崩掉比拦错更危险：用户以为有锁，其实没有。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(tempfile.mkdtemp(prefix="am-enc-hook-"))
        conn = db.connect(config.db_path(cls.root), create=True)
        db.migrate(conn)
        register_worker(conn, "worker-1")
        register_worker(conn, "worker-2")
        task = ledger.create(conn, title="活儿").id
        ledger.dispatch(conn, task_id=task, worker="worker-1")
        cls.locked = str(cls.root / "src" / "Locked.cs")
        claims.declare(conn, task_id=task, worker="worker-1", paths=[cls.locked])
        conn.close()

    def _run_hook(self, payload: dict) -> dict:
        proc = subprocess.run(
            [sys.executable, str(HOOK)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=gbk_env(),
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout or "{}")

    def test_chinese_deny_reason_survives_gbk(self) -> None:
        result = self._run_hook(
            {
                "tool_name": "Edit",
                "session_id": "sid-worker-2",
                "cwd": str(self.root),
                "tool_input": {"file_path": self.locked},
            }
        )
        self.assertEqual(
            result["hookSpecificOutput"]["permissionDecision"], "deny"
        )
        # 中文理由必须完整传出去，否则 worker 不知道该找谁协商
        self.assertIn("已被 worker-1 声明", result["systemMessage"])
        self.assertIn("sess-worker-1", result["systemMessage"])

    def test_allow_path_survives_gbk(self) -> None:
        result = self._run_hook(
            {
                "tool_name": "Edit",
                "session_id": "sid-worker-1",
                "cwd": str(self.root),
                "tool_input": {"file_path": self.locked},
            }
        )
        self.assertEqual(result, {})


if __name__ == "__main__":
    unittest.main()
