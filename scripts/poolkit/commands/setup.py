"""首次初始化：建库、装规则、注册主 agent。

对应 slash 命令 `/am-setup`。**重复执行安全** —— 已建的库只做迁移，
已存在的规则文件默认不覆盖，.gitignore 不写重复行。

注意这里**不写 settings.json**：拦截用的 PreToolUse hook 随插件自带
（`hooks/hooks.json` + `${CLAUDE_PLUGIN_ROOT}`），装上插件即生效，
不需要改用户的配置文件，卸载插件也自动干净。
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from .. import config, db, liveness, registry
from ..errors import UsageError
from ..models import utcnow
from ..render import Result, join, next_steps, section

NAME = "setup"
HELP = "首次初始化：建库、装规则文件、注册主 agent（可重复执行）"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--role",
        default=config.MAIN_ROLE,
        help="把本会话注册成哪个角色，缺省 main",
    )
    parser.add_argument(
        "--force-rules", action="store_true", help="覆盖已存在的规则文件"
    )
    parser.add_argument(
        "--no-gitignore", action="store_true", help="不改 .gitignore"
    )
    parser.add_argument(
        "--scratch-dir",
        help="临时文件的去处，如 D:\\claude-tmp。设了之后往系统 Temp 写文件会被拦下",
    )


def run(ctx, args) -> Result:
    root = ctx.project_root or Path(os.getcwd()).resolve()
    ctx.project_root = root

    conn = ctx.connect(create=True)  # 建库 + 迁移
    version = db.schema_version(conn)

    # 默认配置：只补缺失的 key，已改过的值不动
    now = utcnow()
    existing = {r["key"] for r in conn.execute("SELECT key FROM settings")}
    with db.transaction(conn):
        for key, value in config.DEFAULT_SETTINGS.items():
            if key not in existing:
                db.set_setting(conn, key, value, now)
        if args.scratch_dir:
            db.set_setting(conn, "scratch_dir", args.scratch_dir.strip(), now)

    rules_path, rules_note = _install_rules(root, force=args.force_rules)
    gitignore_note = (
        "跳过" if args.no_gitignore else _ensure_gitignore(root)
    )
    reg_note = _register_self(conn, ctx, args.role)

    data = {
        "project_root": str(root),
        "db": str(config.db_path(root)),
        "schema_version": version,
        "rules": str(rules_path),
        "role": args.role,
        "settings": db.all_settings(conn),
    }

    body = section(
        "✓ am 账本已就绪",
        [
            f"  库文件      {config.db_path(root)}",
            f"  schema      v{version}",
            f"  规则文件    {rules_path}（{rules_note}）",
            f"  .gitignore  {gitignore_note}",
            f"  本会话      {reg_note}",
            "  hook        随插件自带，已生效（未改动你的 settings.json）",
            f"  临时文件    {db.get_setting(conn, 'scratch_dir') or '未限制（--scratch-dir 可设）'}",
        ],
    )
    steps = next_steps(
        [
            "开 worker 窗口：在同一个工作目录再开 1~3 个 Claude Code 会话",
            "每个 worker 窗口里执行： /am-worker register worker-1（2 / 3 同理）",
            "回到本窗口执行 /am-status 确认它们都已上线",
            "之后直接跟我提需求即可，我来派活；每次交付你只需说满意或打回",
        ]
    )
    return Result(text=join(body, steps), data=data)


def _plugin_root() -> Path:
    """插件根目录。

    优先用 Claude Code 注入的 CLAUDE_PLUGIN_ROOT；脱离插件直接跑脚本时
    退化成按文件位置推导（scripts/poolkit/commands/setup.py → 上四层）。
    """
    env = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if env and Path(env).exists():
        return Path(env)
    return Path(__file__).resolve().parents[3]


def _install_rules(root: Path, *, force: bool) -> tuple[Path, str]:
    """把规则模板装进项目的 .claude/rules/。

    规则必须落在**项目里**而不是插件里：共用同一个工作目录的所有会话都会加载
    它，这正是 worker 不需要任何 spawn 时注入就知道规矩的原因（§8）。
    """
    source = _plugin_root() / "templates" / "rules" / config.RULES_FILENAME
    target = root / ".claude" / "rules" / config.RULES_FILENAME
    if not source.exists():
        raise UsageError(
            f"找不到规则模板：{source}",
            hint="插件文件不完整，重装一次 npx skills add Wjg-Ares/agentManager-Skill",
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and not force:
        if target.read_text(encoding="utf-8") == source.read_text(encoding="utf-8"):
            return target, "已是最新"
        return target, "已存在且内容不同，未覆盖；要更新加 --force-rules"
    target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    return target, "已写入"


def _ensure_gitignore(root: Path) -> str:
    """账本是本机运行状态，不该进版本库。已有则不重复写。"""
    entry = ".claude/am/"
    path = root / ".gitignore"
    if path.exists():
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        if any(line.strip().rstrip("/") == entry.rstrip("/") for line in lines):
            return "已包含 .claude/am/"
        content = path.read_text(encoding="utf-8", errors="replace")
        sep = "" if content.endswith("\n") or not content else "\n"
        path.write_text(
            f"{content}{sep}\n# 多 Agent 编排账本（本机运行状态）\n{entry}\n",
            encoding="utf-8",
        )
        return "已追加 .claude/am/"
    path.write_text(f"# 多 Agent 编排账本（本机运行状态）\n{entry}\n", encoding="utf-8")
    return "已新建并写入 .claude/am/"


def _register_self(conn, ctx, role: str) -> str:
    session_id = ctx.session_id
    if not session_id:
        return "未注册（拿不到 CLAUDE_CODE_SESSION_ID，可能不在 Claude Code 会话里）"
    live = liveness.self_session(session_id)
    reg = registry.register(
        conn,
        role=role,
        session_id=session_id,
        session_name=live.name if live else session_id[:8],
        pid=live.pid if live else None,
        cwd=str(ctx.project_root),
    )
    return f"{reg.role}（{reg.session_name}）"
