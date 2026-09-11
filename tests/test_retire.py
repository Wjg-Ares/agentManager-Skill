"""落地之后自动卸插件 —— 这一步的安全边界。

它会删目录，所以每条边界都得钉住：

- 从**自己的仓库**跑落地时（`--plugin-dir` 的用法），`_plugin_root()` 指的是源码，
  绝不能当成「插件安装」删掉
- 落地自检没过就不许卸 —— 否则落地缺文件又把插件删了，用户两头空
- `--keep-plugin` 要能拦住
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from poolkit.commands import setup as setup_cmd  # noqa: E402

REPO = Path(__file__).resolve().parents[1]


class InstalledPluginDetectionTest(unittest.TestCase):
    def test_source_repo_is_not_an_installed_plugin(self) -> None:
        """最要命的一条：别把用户的开发仓库当插件删了。"""
        self.assertFalse(setup_cmd._is_installed_plugin(REPO))

    def test_arbitrary_project_dir_is_not_an_installed_plugin(self) -> None:
        self.assertFalse(
            setup_cmd._is_installed_plugin(Path(tempfile.mkdtemp(prefix="am-notplugin-")))
        )

    def test_plugins_cache_is_an_installed_plugin(self) -> None:
        cache = (
            setup_cmd._claude_home()
            / "plugins"
            / "cache"
            / "agentManager-Skill"
            / "agentManager-Skill"
            / "1.0.0"
        )
        self.assertTrue(setup_cmd._is_installed_plugin(cache))

    def test_claude_home_follows_config_dir_override(self) -> None:
        """用户可能用 CLAUDE_CONFIG_DIR 把整个配置目录搬到别的盘。"""
        with patch.dict("os.environ", {"CLAUDE_CONFIG_DIR": r"D:\claude-data"}):
            self.assertEqual(setup_cmd._claude_home(), Path(r"D:\claude-data"))


class RetireGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="am-retire-")).resolve()

    def _retire(self, *, keep: bool = False) -> str:
        return setup_cmd._retire_plugin(self.root, keep=keep)

    def test_leaves_source_repo_alone(self) -> None:
        """从仓库跑落地 → 报告「未动」，而且绝不能碰到仓库里的文件。"""
        with patch.object(setup_cmd, "_plugin_root", lambda: REPO):
            note = self._retire()
        self.assertIn("未动", note)
        self.assertTrue((REPO / "scripts" / "pool.py").is_file(), "仓库被动过了！")

    def test_refuses_when_self_check_fails(self) -> None:
        """落地的脚本跑不起来就保留插件 —— 不能让用户两头空。"""
        fake_install = (
            setup_cmd._claude_home() / "plugins" / "cache" / "whatever" / "x" / "1.0.0"
        )
        with patch.object(setup_cmd, "_plugin_root", lambda: fake_install), patch.object(
            setup_cmd, "_verify_vendored", lambda _root: (False, "假装自检失败")
        ):
            note = self._retire()
        self.assertIn("保留", note)
        self.assertIn("假装自检失败", note)

    def test_keep_plugin_wins_over_successful_check(self) -> None:
        fake_install = (
            setup_cmd._claude_home() / "plugins" / "cache" / "whatever" / "x" / "1.0.0"
        )
        with patch.object(setup_cmd, "_plugin_root", lambda: fake_install), patch.object(
            setup_cmd, "_verify_vendored", lambda _root: (True, "")
        ):
            note = self._retire(keep=True)
        self.assertIn("--keep-plugin", note)


class VerifyVendoredTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="am-verify-")).resolve()

    def test_missing_script_fails_the_check(self) -> None:
        ok, detail = setup_cmd._verify_vendored(self.root)
        self.assertFalse(ok)
        self.assertIn("pool.py", detail)


if __name__ == "__main__":
    unittest.main()
