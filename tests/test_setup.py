"""安装形态判定，以及往项目 settings.local.json 里装 hook 的那段。

这段代码会**写用户的配置文件**，所以每条路径都要有测试盯着：
不能覆盖别人的配置、不能写重复项、要能干净撤掉。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "am-setup" / "scripts"))

from poolkit.commands import setup as setup_cmd  # noqa: E402
from poolkit.errors import UsageError  # noqa: E402

REPO = Path(__file__).resolve().parents[1]


def settings_of(root: Path) -> dict:
    path = root / ".claude" / "settings.local.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def pre_tool_use(root: Path) -> list:
    return (settings_of(root).get("hooks") or {}).get("PreToolUse") or []


class LayoutTest(unittest.TestCase):
    def test_repo_itself_is_plugin_layout(self) -> None:
        """开发仓库自身就是插件布局：根上有 hooks/hooks.json。"""
        is_plugin, plugin_root = setup_cmd._layout()
        self.assertTrue(is_plugin)
        self.assertEqual(plugin_root, REPO)

    def test_hook_command_points_at_a_real_file(self) -> None:
        """写进配置的命令必须指向真实存在的脚本，否则每次 Edit 都会静默失败。"""
        command = setup_cmd._hook_command()
        self.assertIn("pretooluse.py", command)
        path = Path(command.split('"')[1])
        self.assertTrue(path.exists(), f"hook 脚本不存在：{path}")


class InstallHookTest(unittest.TestCase):
    """以下全部模拟 npx（skills）形态 —— 插件形态本就什么都不写。"""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="am-setup-"))
        self.patcher = patch.object(setup_cmd, "_layout", lambda: (False, None))
        self.patcher.start()

    def tearDown(self) -> None:
        self.patcher.stop()

    def test_writes_hook_into_project_settings(self) -> None:
        note = setup_cmd._install_hook(self.root)
        self.assertIn("写入", note)
        entries = pre_tool_use(self.root)
        self.assertEqual(len(entries), 1)
        self.assertIn("pretooluse.py", entries[0]["hooks"][0]["command"])
        self.assertEqual(entries[0]["matcher"], setup_cmd.HOOK_MATCHER)

    def test_is_idempotent(self) -> None:
        """重复跑 setup 不能堆出两条一样的 hook。"""
        setup_cmd._install_hook(self.root)
        note = setup_cmd._install_hook(self.root)
        self.assertIn("更新", note)
        self.assertEqual(len(pre_tool_use(self.root)), 1)

    def test_keeps_other_peoples_hooks(self) -> None:
        """用户已有的 hook 一条都不能动。"""
        path = self.root / ".claude" / "settings.local.json"
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(
                {
                    "permissions": {"allow": ["Bash(ls *)"]},
                    "hooks": {
                        "PreToolUse": [
                            {
                                "matcher": "Bash",
                                "hooks": [{"type": "command", "command": "echo 别人的"}],
                            }
                        ],
                        "Stop": [
                            {"hooks": [{"type": "command", "command": "echo 停了"}]}
                        ],
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        setup_cmd._install_hook(self.root)

        data = settings_of(self.root)
        self.assertEqual(data["permissions"], {"allow": ["Bash(ls *)"]})
        self.assertIn("Stop", data["hooks"])
        commands = [
            h["command"] for e in data["hooks"]["PreToolUse"] for h in e["hooks"]
        ]
        self.assertIn("echo 别人的", commands)
        self.assertEqual(sum("pretooluse.py" in c for c in commands), 1)

    def test_remove_takes_only_ours(self) -> None:
        path = self.root / ".claude" / "settings.local.json"
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(
                {
                    "hooks": {
                        "PreToolUse": [
                            {
                                "matcher": "Bash",
                                "hooks": [{"type": "command", "command": "echo 别人的"}],
                            }
                        ]
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        setup_cmd._install_hook(self.root)
        note = setup_cmd._install_hook(self.root, remove=True)

        self.assertIn("移除", note)
        commands = [h["command"] for e in pre_tool_use(self.root) for h in e["hooks"]]
        self.assertEqual(commands, ["echo 别人的"])

    def test_remove_on_clean_project_is_harmless(self) -> None:
        self.assertIn("本来就没装", setup_cmd._install_hook(self.root, remove=True))

    def test_broken_json_is_not_overwritten(self) -> None:
        """配置文件坏了就报错退出 —— 绝不能拿覆盖当修复。"""
        path = self.root / ".claude" / "settings.local.json"
        path.parent.mkdir(parents=True)
        path.write_text("{ 这不是 JSON", encoding="utf-8")
        with self.assertRaises(UsageError):
            setup_cmd._install_hook(self.root)
        self.assertEqual(path.read_text(encoding="utf-8"), "{ 这不是 JSON")

    def test_empty_file_is_treated_as_empty_config(self) -> None:
        path = self.root / ".claude" / "settings.local.json"
        path.parent.mkdir(parents=True)
        path.write_text("", encoding="utf-8")
        setup_cmd._install_hook(self.root)
        self.assertEqual(len(pre_tool_use(self.root)), 1)


if __name__ == "__main__":
    unittest.main()
