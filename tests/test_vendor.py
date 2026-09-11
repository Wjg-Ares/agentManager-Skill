"""`--vendor`：把运行时文件全部落地进项目，之后不依赖插件安装。

用户的诉求是 C 盘不占空间。插件只要用 `claude plugin install` 装，本体就必然在
`~/.claude`，改不了；但落地之后项目自包含 —— 脚本、hook、四个命令、规则都在
项目的 `.claude/` 下，那份插件就可以卸了。

所以这里的验收标准不是「文件复制过去了」，而是**落地的那份真能独立跑起来**：
最后两个用例直接执行项目里的脚本和 hook，不依赖任何插件路径。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
POOL = REPO / "scripts" / "pool.py"


def run_cli(*args: str, cwd: Path | None = None, plugin_root: Path | None = REPO) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    if plugin_root is None:
        env.pop("CLAUDE_PLUGIN_ROOT", None)
    else:
        env["CLAUDE_PLUGIN_ROOT"] = str(plugin_root)
    return subprocess.run(
        [sys.executable, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=str(cwd) if cwd else None,
        timeout=180,
    )


class VendorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # resolve() 不能省：Windows 的 TEMP 是 8.3 短名（C:\Users\ADMINI~1\...），
        # 而 CLI 内部会把 --project-root 解析成长名，两边不归一断言就永远对不上。
        cls.root = Path(tempfile.mkdtemp(prefix="am-vendor-")).resolve()
        cls.proc = run_cli(
            str(POOL), "--project-root", str(cls.root), "setup", "--vendor"
        )
        assert cls.proc.returncode == 0, cls.proc.stderr
        # 落地刚完成时的快照。后面的用例会真的执行落地脚本，Python 会现场生成
        # __pycache__ —— 那不是复制带来的，所以必须在这一刻取样。
        cls.pycache_after_vendor = sorted(
            (cls.root / ".claude").rglob("__pycache__")
        )

    @property
    def claude(self) -> Path:
        return self.root / ".claude"

    # ---- 文件落地 ----

    def test_scripts_landed(self) -> None:
        self.assertTrue((self.claude / "scripts" / "pool.py").is_file())
        self.assertTrue((self.claude / "scripts" / "poolkit" / "guard.py").is_file())
        self.assertTrue(
            (self.claude / "scripts" / "hooks" / "pretooluse.py").is_file()
        )

    def test_no_pycache_copied(self) -> None:
        """复制时要排除源目录里的 __pycache__，别把编译产物搬进用户项目。"""
        self.assertEqual(self.pycache_after_vendor, [])

    def test_all_four_skills_landed(self) -> None:
        names = sorted(p.name for p in (self.claude / "skills").iterdir())
        self.assertEqual(
            names, ["am-approve", "am-setup", "am-status", "am-worker"]
        )

    def test_skill_paths_point_into_the_project(self) -> None:
        """SKILL.md 里的命令路径必须指向项目内的脚本，不能留占位符。"""
        expected = (self.claude / "scripts" / "pool.py").as_posix()
        for manifest in (self.claude / "skills").glob("*/SKILL.md"):
            with self.subTest(skill=manifest.parent.name):
                text = manifest.read_text(encoding="utf-8")
                self.assertNotIn("${CLAUDE_PLUGIN_ROOT}", text)
                self.assertIn(expected, text)

    def test_rules_point_into_the_project(self) -> None:
        text = (self.claude / "rules" / "am-orchestration.md").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("${CLAUDE_PLUGIN_ROOT}", text)
        self.assertIn((self.claude / "scripts" / "pool.py").as_posix(), text)

    def test_hook_registered_in_project_settings(self) -> None:
        data = json.loads(
            (self.claude / "settings.local.json").read_text(encoding="utf-8")
        )
        entries = data["hooks"]["PreToolUse"]
        commands = [h["command"] for e in entries for h in e["hooks"]]
        self.assertEqual(len(commands), 1)
        self.assertIn(
            (self.claude / "scripts" / "hooks" / "pretooluse.py").as_posix(),
            commands[0],
        )

    def test_rerun_is_idempotent(self) -> None:
        proc = run_cli(str(POOL), "--project-root", str(self.root), "setup", "--vendor")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(
            (self.claude / "settings.local.json").read_text(encoding="utf-8")
        )
        commands = [
            h["command"] for e in data["hooks"]["PreToolUse"] for h in e["hooks"]
        ]
        self.assertEqual(len(commands), 1, "重复落地不能堆出第二条 hook")

    def test_vendored_artifacts_are_gitignored(self) -> None:
        """落地产物里写死了本机绝对路径，进版本库对队友没意义还会天天冲突。"""
        text = (self.root / ".gitignore").read_text(encoding="utf-8")
        for entry in (
            ".claude/scripts/",
            ".claude/skills/",
            ".claude/settings.local.json",
            ".claude/am/",  # 账本，原本就该忽略
        ):
            with self.subTest(entry=entry):
                self.assertIn(entry, text)

    def test_gitignore_entries_not_duplicated(self) -> None:
        """无论落地几次，每条只该出现一次。"""
        run_cli(str(POOL), "--project-root", str(self.root), "setup", "--vendor")
        text = (self.root / ".gitignore").read_text(encoding="utf-8")
        self.assertEqual(text.count(".claude/scripts/"), 1)
        self.assertEqual(text.count(".claude/settings.local.json"), 1)

    # ---- 落地的那份能不能独立跑 ----

    def test_landed_cli_runs_without_plugin_env(self) -> None:
        """这才是验收标准：不给任何插件路径，项目里那份自己能跑。"""
        proc = run_cli(
            str(self.claude / "scripts" / "pool.py"),
            "--project-root",
            str(self.root),
            "status",
            plugin_root=None,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Slot", proc.stdout)

    def test_landed_hook_blocks_without_plugin_env(self) -> None:
        """落地的 hook 也要能独立判定 —— 它得自己找到同级的 poolkit。"""
        # 先造一个被别人占住的文件
        run_cli(
            str(self.claude / "scripts" / "pool.py"), "--project-root", str(self.root),
            "register", "worker-1", "--session-id", "sid-a", "--session-name", "win-a",
            plugin_root=None,
        )
        run_cli(
            str(self.claude / "scripts" / "pool.py"), "--project-root", str(self.root),
            "register", "worker-2", "--session-id", "sid-b", "--session-name", "win-b",
            plugin_root=None,
        )
        run_cli(
            str(self.claude / "scripts" / "pool.py"), "--project-root", str(self.root),
            "add", "活儿", plugin_root=None,
        )
        run_cli(
            str(self.claude / "scripts" / "pool.py"), "--project-root", str(self.root),
            "dispatch", "1", "--to", "worker-1", plugin_root=None,
        )
        target = str(self.root / "src" / "A.cs")
        env = {**os.environ, "CLAUDE_CODE_SESSION_ID": "sid-a", "PYTHONIOENCODING": "utf-8"}
        env.pop("CLAUDE_PLUGIN_ROOT", None)
        subprocess.run(
            [
                sys.executable, str(self.claude / "scripts" / "pool.py"),
                "--project-root", str(self.root), "declare", target,
            ],
            capture_output=True, text=True, encoding="utf-8", env=env, timeout=60,
        )

        payload = {
            "tool_name": "Edit",
            "session_id": "sid-b",
            "cwd": str(self.root),
            "tool_input": {"file_path": target},
        }
        hook_env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        hook_env.pop("CLAUDE_PLUGIN_ROOT", None)
        proc = subprocess.run(
            [sys.executable, str(self.claude / "scripts" / "hooks" / "pretooluse.py")],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=hook_env,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads(proc.stdout or "{}")
        self.assertEqual(
            result.get("hookSpecificOutput", {}).get("permissionDecision"),
            "deny",
            f"落地的 hook 没拦住：{proc.stdout}",
        )
        self.assertIn("worker-1", result["systemMessage"])


if __name__ == "__main__":
    unittest.main()
