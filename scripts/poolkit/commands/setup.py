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
    parser.add_argument(
        "--reset",
        action="store_true",
        help="清空账本从头来：任务、交付、文件声明、注册、审计全删（配置保留）",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="配合 --reset：即使还有 worker 活着也照删（危险，见输出里的说明）",
    )


def run(ctx, args) -> Result:
    root = ctx.project_root or Path(os.getcwd()).resolve()
    ctx.project_root = root

    if args.reset:
        return _reset(ctx, args)

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


#: 清空顺序：先子后父，外键才不会挡。events 也删 —— reset 的语义是从头来。
_RESET_TABLES = ("claims", "deliverables", "tasks", "registry", "events")


def _reset(ctx, args) -> Result:
    """清空账本重来。

    **不自动触发**。主 agent 重开窗口是日常操作（compact、手滑关窗、崩溃），
    那种情况下只要重新注册一下地址就行，账本必须留着 —— 因为 worker 是独立进程，
    主窗口没了它们照样在改代码，`claims` 一清锁就全没了，而它们还在同一个目录里
    写文件。那正是这套东西唯一要防的事故。

    所以清空只能由用户显式敲 `--reset`，而且还要先确认没有活着的 worker。
    """
    conn = ctx.connect()  # 库不存在会抛 NotSetUp，本来也没什么可清的

    before = {
        "tasks": _count(conn, "tasks"),
        "open_tasks": _count(conn, "tasks", "status IN ('assigned','delivered','rejected')"),
        "deliverables": _count(conn, "deliverables"),
        "active_claims": _count(conn, "claims", "released_at IS NULL"),
        "registry": _count(conn, "registry"),
        "events": _count(conn, "events"),
    }

    survivors = _live_workers(conn, ctx)
    if survivors and not args.force:
        raise UsageError(
            f"还有 {len(survivors)} 个 worker 活着：{'、'.join(survivors)}",
            hint=(
                "它们是独立进程，现在清空会把文件锁一起删掉，而它们还在同一个目录里改代码 —— "
                "覆盖丢代码正是这套东西要防的事。先关掉那些窗口再 --reset；"
                "确实要强来就加 --force"
            ),
        )

    now = utcnow()
    with db.transaction(conn):
        for table in _RESET_TABLES:
            conn.execute(f"DELETE FROM {table}")
        db.log_event(
            conn,
            kind="reset",
            now=now,
            detail=f"清空前：{before}" + ("（--force，无视存活 worker）" if survivors else ""),
        )

    reg_note = _register_self(conn, ctx, args.role)

    body = section(
        "✓ 账本已清空，从头来",
        [
            f"  任务        删了 {before['tasks']} 条（其中在办 {before['open_tasks']} 条）",
            f"  交付记录    删了 {before['deliverables']} 条",
            f"  文件声明    释放 {before['active_claims']} 把活跃锁",
            f"  角色注册    清了 {before['registry']} 条",
            f"  审计        删了 {before['events']} 条",
            f"  配置        保留（scratch_dir 等不受影响）",
            f"  本会话      {reg_note}",
        ],
    )
    steps = [
        "worker 窗口需要重新登记： /am-worker register worker-1（2 / 3 同理）",
        "规则文件和 hook 没动，不用重装",
    ]
    if survivors:
        steps.insert(
            0,
            f"⚠ 你用了 --force，而 {'、'.join(survivors)} 还活着 —— "
            "立刻去那些窗口 /clear 或直接关掉，它们手上的锁已经没了",
        )
    return Result(
        text=join(body, next_steps(steps)),
        data={"reset": True, "before": before, "live_workers": survivors},
    )


def _count(conn, table: str, where: str = "1=1") -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}").fetchone()[0])


def _live_workers(conn, ctx) -> list[str]:
    """还活着的 worker 角色名。探针不可用时保守返回注册表里所有 alive 的。"""
    registered = [r for r in registry.workers(conn) if r.status.value == "alive"]
    if not registered:
        return []
    sessions = liveness.list_sessions()
    if sessions is None:
        # 探不到不等于都死了 —— 宁可拦住让用户自己确认，也不能默认它们已经没了
        return [r.role for r in registered]
    alive_ids = {s.session_id for s in sessions}
    return [r.role for r in registered if r.session_id in alive_ids]


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
