"""项目里的规则副本该不该被新模板覆盖。

项目里的 `.claude/rules/am-orchestration.md` 是从插件模板复制过去的副本，
升级插件只换模板、不动副本。所以每次 setup 都要决定要不要更新它。

关键是分清两种「内容和模板不一样」，它们的处理方式相反：
用户没动过（只是模板升级了）就该直接更新；用户手改过就得保住他的修改。
光比内容分不出来，得靠写入时记下的哈希当基准。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import fresh_db  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from poolkit import config, db  # noqa: E402
from poolkit.commands import setup as setup_cmd  # noqa: E402

V1 = "# 规则 v1\n\n第一版内容。\n"
V2 = "# 规则 v2\n\n第二版内容，多了一条。\n"
MINE = "# 规则 v1\n\n第一版内容。\n\n## 我自己加的\n本项目禁止改 Legacy 目录。\n"


class RulesUpdateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn, self.db_path = fresh_db()
        self.tmp = Path(tempfile.mkdtemp(prefix="am-rules-"))
        self.project = self.tmp / "proj"
        self.project.mkdir()
        self.plugin = self.tmp / "plugin"
        (self.plugin / "templates" / "rules").mkdir(parents=True)

    def tearDown(self) -> None:
        self.conn.close()

    @property
    def target(self) -> Path:
        return self.project / ".claude" / "rules" / config.RULES_FILENAME

    def _set_template(self, text: str) -> None:
        (self.plugin / "templates" / "rules" / config.RULES_FILENAME).write_text(
            text, encoding="utf-8"
        )

    def _install(self, *, force: bool = False) -> str:
        with patch.object(setup_cmd, "_plugin_root", lambda: self.plugin):
            _, note = setup_cmd._install_rules(
                self.conn, self.project, force=force, base=self.plugin
            )
        return note

    # ------------------------------------------------------------------

    def test_first_install_writes_and_records(self) -> None:
        self._set_template(V1)
        self.assertEqual(self._install(), "已写入")
        self.assertEqual(self.target.read_text(encoding="utf-8"), V1)
        self.assertTrue(db.get_setting(self.conn, setup_cmd._RULES_HASH_KEY))

    def test_same_template_twice_is_noop(self) -> None:
        self._set_template(V1)
        self._install()
        self.assertEqual(self._install(), "已是最新")

    def test_untouched_copy_auto_updates(self) -> None:
        """用户没动过，模板升级了 —— 直接更新，不该让他记 --force-rules。"""
        self._set_template(V1)
        self._install()
        self._set_template(V2)
        self.assertEqual(self._install(), "已更新到新版规则")
        self.assertEqual(self.target.read_text(encoding="utf-8"), V2)

    def test_hand_edited_copy_is_protected(self) -> None:
        """用户手改过就不能覆盖，否则他加的项目专属规则就没了。"""
        self._set_template(V1)
        self._install()
        self.target.write_text(MINE, encoding="utf-8")
        self._set_template(V2)

        note = self._install()
        self.assertIn("手改过", note)
        self.assertIn("--force-rules", note)
        self.assertEqual(self.target.read_text(encoding="utf-8"), MINE)

    def test_force_overwrites_hand_edits(self) -> None:
        self._set_template(V1)
        self._install()
        self.target.write_text(MINE, encoding="utf-8")
        self._set_template(V2)

        self.assertIn("已覆盖", self._install(force=True))
        self.assertEqual(self.target.read_text(encoding="utf-8"), V2)

    def test_force_updates_the_baseline(self) -> None:
        """--force 之后基准要跟上，否则下次又被当成「手改过」。"""
        self._set_template(V1)
        self._install()
        self.target.write_text(MINE, encoding="utf-8")
        self._set_template(V2)
        self._install(force=True)

        self._set_template(V1)  # 再升一版（这里退回 v1，内容不同即可）
        self.assertEqual(self._install(), "已更新到新版规则")

    def test_legacy_copy_without_baseline_is_not_overwritten(self) -> None:
        """老版本装的没有哈希记录，判断不出是不是用户改的 —— 保守不覆盖。"""
        self._set_template(V1)
        self.target.parent.mkdir(parents=True)
        self.target.write_text(MINE, encoding="utf-8")

        note = self._install()
        self.assertIn("--force-rules", note)
        self.assertEqual(self.target.read_text(encoding="utf-8"), MINE)

    def test_legacy_identical_copy_backfills_baseline(self) -> None:
        """内容本来就和模板一致，只是缺记录 —— 补上基准，下次才能自动更新。"""
        self._set_template(V1)
        self.target.parent.mkdir(parents=True)
        self.target.write_text(V1, encoding="utf-8")

        self.assertEqual(self._install(), "已是最新")
        self._set_template(V2)
        self.assertEqual(self._install(), "已更新到新版规则")

    def test_plugin_root_placeholder_is_expanded(self) -> None:
        """规则文件里的 ${CLAUDE_PLUGIN_ROOT} 必须在写入时换成真实路径。

        `.claude/rules/` 是常驻规则文本，不是 skill —— Claude Code 不对它做
        ${...} 替换，shell 里也没有这个变量。留着字面量的话路径会塌成
        "/scripts/pool.py"，worker 一跑就找不到脚本。
        """
        self._set_template('执行 python "${CLAUDE_PLUGIN_ROOT}/scripts/pool.py" whoami\n')
        self._install()

        written = self.target.read_text(encoding="utf-8")
        self.assertNotIn("${CLAUDE_PLUGIN_ROOT}", written)
        self.assertIn(self.plugin.as_posix(), written)
        self.assertIn("/scripts/pool.py", written)

    def test_baseline_uses_the_expanded_text(self) -> None:
        """哈希得基于替换后的内容，否则每次比对都不一致，会被误判成手改过。"""
        self._set_template('${CLAUDE_PLUGIN_ROOT}/scripts/pool.py\n')
        self._install()
        self.assertEqual(self._install(), "已是最新")

    def test_internal_key_hidden_from_config(self) -> None:
        """哈希是内部记录，不该出现在 config 的配置列表里。"""
        self._set_template(V1)
        self._install()
        self.assertNotIn(setup_cmd._RULES_HASH_KEY, db.all_settings(self.conn))
        self.assertIn(
            setup_cmd._RULES_HASH_KEY, db.all_settings(self.conn, include_internal=True)
        )


if __name__ == "__main__":
    unittest.main()
