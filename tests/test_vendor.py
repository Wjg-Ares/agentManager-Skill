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

sys.path.insert(0, str(REPO / "scripts"))


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

    def test_templates_landed(self) -> None:
        """规则模板也得落地。

        漏了它 = 落地不完整：--vendor 会把插件卸掉，之后每次 setup 都找不到模板。
        """
        self.assertTrue(
            (self.claude / "templates" / "rules" / "am-orchestration.md").is_file()
        )

    def test_rules_written_even_though_plugin_gets_retired(self) -> None:
        """落地这一趟里插件会被卸掉，但规则文件必须照样装好。

        这正是事故现场：清理跑在读模板之前，把自己的源删了。
        """
        rules = self.claude / "rules" / "am-orchestration.md"
        self.assertTrue(rules.is_file(), "规则没写成 —— 八成又被提前卸插件坑了")
        self.assertIn(
            (self.claude / "scripts" / "pool.py").as_posix(),
            rules.read_text(encoding="utf-8"),
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


class VendorRewritesStaleRulesTest(unittest.TestCase):
    """用户的真实路径：先用插件模式跑过一阵，再落地。

    那时项目里的规则指向的是插件目录（C 盘）。落地必须把它改写成项目内路径 ——
    留着旧的等于让 worker 照着一条失效路径去找脚本，所以 --vendor 一律覆盖，
    不需要用户额外加 --force-rules。
    """

    def test_stale_plugin_path_is_rewritten(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="am-stale-")).resolve()
        rules = root / ".claude" / "rules" / "am-orchestration.md"

        # 1) 插件模式：规则里写的是插件目录
        proc = run_cli(str(POOL), "--project-root", str(root), "setup")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(REPO.as_posix(), rules.read_text(encoding="utf-8"))

        # 2) 落地：必须改写成项目内路径，不用加 --force-rules
        proc = run_cli(str(POOL), "--project-root", str(root), "setup", "--vendor")
        self.assertEqual(proc.returncode, 0, proc.stderr)

        text = rules.read_text(encoding="utf-8")
        self.assertIn((root / ".claude" / "scripts" / "pool.py").as_posix(), text)
        self.assertNotIn(f"{REPO.as_posix()}/scripts/pool.py", text)


class SelfVendorGuardTest(unittest.TestCase):
    """源即目标 —— 落地过的项目再落一次，绝不能把自己删掉。

    真事故：项目落地过之后 `.claude/skills/am-setup/` 就存在了，而 Claude Code
    **优先加载项目级 skill**，于是下一次 `/am-setup --vendor` 跑的就是落地副本
    自己。它不是插件加载的，CLAUDE_PLUGIN_ROOT 没设，`_plugin_root()` 退化成按
    文件位置推导 → `<项目>/.claude`。第一步 rmtree 把正在运行的脚本删了，复制时
    源已不存在，落地半途而废；而 hook 仍指向被删掉的 pretooluse.py，
    那个项目的 Edit / Bash 全被拦死，连改回配置都做不到。
    """

    def _vendored_project(self) -> Path:
        root = Path(tempfile.mkdtemp(prefix="am-self-")).resolve()
        proc = run_cli(str(POOL), "--project-root", str(root), "setup", "--vendor")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return root

    def test_self_vendor_does_not_delete_itself(self) -> None:
        """用落地副本自己跑 --vendor：要么成功，要么报错，但脚本必须还在。"""
        root = self._vendored_project()
        vendored_pool = root / ".claude" / "scripts" / "pool.py"

        # plugin_root=None 清掉 CLAUDE_PLUGIN_ROOT，复现「跑的是项目内那份」
        proc = run_cli(
            str(vendored_pool),
            "--project-root",
            str(root),
            "setup",
            "--vendor",
            plugin_root=None,
        )

        self.assertTrue(vendored_pool.is_file(), "把自己删了！")
        self.assertTrue(
            (root / ".claude" / "scripts" / "poolkit" / "config.py").is_file(),
            "poolkit 被删了！",
        )
        self.assertTrue(
            (root / ".claude" / "scripts" / "hooks" / "pretooluse.py").is_file(),
            "hook 脚本被删了 —— 项目的 Edit/Bash 会全被拦死",
        )
        if proc.returncode != 0:
            # 找不到可用的源时报错是允许的，但必须说清楚是同源问题
            self.assertIn("同一个目录", proc.stdout + proc.stderr)

    def test_still_runnable_after_self_vendor_attempt(self) -> None:
        """最要紧的是别把项目搞瘫 —— 试过之后脚本得还能跑。"""
        root = self._vendored_project()
        vendored_pool = root / ".claude" / "scripts" / "pool.py"
        run_cli(
            str(vendored_pool), "--project-root", str(root), "setup", "--vendor",
            plugin_root=None,
        )
        proc = run_cli(
            str(vendored_pool), "--project-root", str(root), "config", plugin_root=None
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


class SameTreeTest(unittest.TestCase):
    def test_detects_self_and_nesting(self) -> None:
        from poolkit.commands import setup as setup_cmd

        root = Path(tempfile.mkdtemp(prefix="am-tree-")).resolve()
        claude = root / ".claude"
        claude.mkdir()
        self.assertTrue(setup_cmd._same_tree(claude, claude))
        self.assertTrue(setup_cmd._same_tree(claude / "scripts", claude))
        self.assertFalse(setup_cmd._same_tree(REPO, claude))


if __name__ == "__main__":
    unittest.main()
