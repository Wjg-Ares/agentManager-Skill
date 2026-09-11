"""首次初始化：建库、装规则、装 hook、注册主 agent。

对应 slash 命令 `/am-setup`。**重复执行安全** —— 已建的库只做迁移，
已存在的规则文件默认不覆盖，.gitignore 与 hook 配置都不写重复项。

关于 hook 装在哪，取决于这套东西是怎么被安装的（见 :func:`_layout`）：

- **插件安装**（`claude plugin install` / `--plugin-dir`）：仓库根带着
  `hooks/hooks.json`，Claude Code 会自动加载，这里**什么都不做**
- **skills 安装**（`npx skills add`）：那个安装器只复制 `skills/` 目录，
  不认识 `hooks.json`，所以这里把 hook 写进**项目的**
  `.claude/settings.local.json` —— 只碰当前项目，不碰全局配置
"""

from __future__ import annotations

import argparse
import json
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
        "--remove-hook",
        action="store_true",
        help="把本项目 settings.local.json 里的拦截 hook 撤掉（卸载时用）",
    )


def run(ctx, args) -> Result:
    root = ctx.project_root or Path(os.getcwd()).resolve()
    ctx.project_root = root

    if args.remove_hook:
        note = _install_hook(root, remove=True)
        return Result(
            text=f"✓ 拦截 hook：{note}\n  账本与规则文件未动，要彻底清掉就删 .claude/am/ 和 .claude/rules/am-orchestration.md",
            data={"hook": note, "removed": True},
        )

    conn = ctx.connect(create=True)  # 建库 + 迁移
    version = db.schema_version(conn)

    # 默认配置：只补缺失的 key，已改过的值不动
    now = utcnow()
    existing = {r["key"] for r in conn.execute("SELECT key FROM settings")}
    with db.transaction(conn):
        for key, value in config.DEFAULT_SETTINGS.items():
            if key not in existing:
                db.set_setting(conn, key, value, now)

    rules_path, rules_note = _install_rules(root, force=args.force_rules)
    gitignore_note = (
        "跳过" if args.no_gitignore else _ensure_gitignore(root)
    )
    hook_note = _install_hook(root)
    is_plugin, _ = _layout()
    reg_note = _register_self(conn, ctx, args.role)

    data = {
        "project_root": str(root),
        "db": str(config.db_path(root)),
        "schema_version": version,
        "rules": str(rules_path),
        "role": args.role,
        "install_mode": "plugin" if is_plugin else "skills",
        "hook": hook_note,
        "settings": db.all_settings(conn),
    }

    body = section(
        "✓ am 账本已就绪",
        [
            f"  库文件      {config.db_path(root)}",
            f"  schema      v{version}",
            f"  规则文件    {rules_path}（{rules_note}）",
            f"  .gitignore  {gitignore_note}",
            f"  hook        {hook_note}",
            f"  本会话      {reg_note}",
            f"  安装形态    {'插件' if is_plugin else 'skills（npx）'}",
        ],
    )
    lines = []
    if not is_plugin and "写入" in hook_note:
        # 新写进 settings 的 hook 要下个会话才加载，这一条必须排在最前面
        lines.append(
            "**先重开本窗口** —— 拦截 hook 刚写进配置，当前会话还没加载它，"
            "此时并发写没有任何保护"
        )
    lines += [
        "开 worker 窗口：在同一个工作目录再开 1~3 个 Claude Code 会话",
        "每个 worker 窗口里执行： /am-worker register worker-1（2 / 3 同理）",
        "回到本窗口执行 /am-status 确认它们都已上线",
        "之后直接跟我提需求即可，我来派活；每次交付你只需说满意或打回",
    ]
    steps = next_steps(lines)
    return Result(text=join(body, steps), data=data)


#: 本文件位于 <skill>/scripts/poolkit/commands/setup.py
_SKILL_ROOT = Path(__file__).resolve().parents[3]


def _layout() -> tuple[bool, Path | None]:
    """判断安装形态，返回 (是否插件安装, 插件根)。

    只看文件系统，不读环境变量 —— ``CLAUDE_PLUGIN_ROOT`` 只在插件安装下存在，
    而这个函数恰恰要在它不存在时也能给出正确答案。

    判据：skills 的父目录里有没有 ``hooks/hooks.json``。
    插件布局是 ``<插件根>/skills/am-setup``，有；
    npx 布局是 ``<项目>/.claude/skills/am-setup``，没有。
    """
    container = _SKILL_ROOT.parents[1]  # <skills 的父>
    if (container / "hooks" / "hooks.json").exists():
        return True, container
    return False, None


def _install_rules(root: Path, *, force: bool) -> tuple[Path, str]:
    """把规则模板装进项目的 .claude/rules/。

    规则必须落在**项目里**而不是插件里：共用同一个工作目录的所有会话都会加载
    它，这正是 worker 不需要任何 spawn 时注入就知道规矩的原因（§8）。
    """
    source = _SKILL_ROOT / "templates" / "rules" / config.RULES_FILENAME
    target = root / ".claude" / "rules" / config.RULES_FILENAME
    if not source.exists():
        raise UsageError(
            f"找不到规则模板：{source}",
            hint="安装不完整，重装一次：npx skills add Wjg-Ares/agentManager-Skill",
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and not force:
        if target.read_text(encoding="utf-8") == source.read_text(encoding="utf-8"):
            return target, "已是最新"
        return target, "已存在且内容不同，未覆盖；要更新加 --force-rules"
    target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    return target, "已写入"


#: hook 覆盖的工具。与 hooks/hooks.json 里的 matcher 保持一致。
HOOK_MATCHER = "Edit|Write|MultiEdit|NotebookEdit|Bash"

#: 识别「本插件写的 hook 条目」用的特征串
_HOOK_MARK = "pretooluse.py"


def _hook_command() -> str:
    script = _SKILL_ROOT / "scripts" / "hooks" / _HOOK_MARK
    # 用正斜杠：JSON 里不必转义，Windows 的 python 也照样认
    return f'python "{script.as_posix()}"'


def _install_hook(root: Path, *, remove: bool = False) -> str:
    """把 PreToolUse hook 写进**项目的** settings.local.json。

    只在 skills 安装形态下需要 —— 插件形态有 `hooks/hooks.json` 自动加载。
    写的是项目级的 `.local.json`，不碰用户全局配置，也不进版本库。
    """
    is_plugin, _ = _layout()
    if is_plugin and not remove:
        return "随插件自带，已生效（未改动任何配置文件）"

    settings_path = root / ".claude" / "settings.local.json"
    data: dict = {}
    if settings_path.exists():
        raw = settings_path.read_text(encoding="utf-8").strip()
        if raw:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise UsageError(
                    f"{settings_path} 不是合法 JSON：{exc}",
                    hint="先把它修好再跑 —— 我不会覆盖你已有的配置",
                ) from exc

    entries = (data.get("hooks") or {}).get("PreToolUse") or []

    # 先摘掉本插件的旧条目：这样重复执行是幂等的，路径变了也能自动纠正
    cleaned: list = []
    had_ours = False
    for entry in entries:
        inner = entry.get("hooks") or []
        kept = [h for h in inner if _HOOK_MARK not in str(h.get("command", ""))]
        if len(kept) != len(inner):
            had_ours = True
        if kept:
            cleaned.append({**entry, "hooks": kept})

    if remove:
        if not had_ours:
            return "本来就没装"
        _write_settings(settings_path, data, cleaned)
        return f"已从 {settings_path.name} 移除"

    cleaned.append(
        {
            "matcher": HOOK_MATCHER,
            "hooks": [{"type": "command", "command": _hook_command(), "timeout": 10}],
        }
    )
    _write_settings(settings_path, data, cleaned)
    return f"已{'更新' if had_ours else '写入'} .claude/settings.local.json"


def _write_settings(path: Path, data: dict, pre_tool_use: list) -> None:
    hooks = data.setdefault("hooks", {})
    if pre_tool_use:
        hooks["PreToolUse"] = pre_tool_use
    else:
        hooks.pop("PreToolUse", None)
        if not hooks:
            data.pop("hooks", None)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


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
