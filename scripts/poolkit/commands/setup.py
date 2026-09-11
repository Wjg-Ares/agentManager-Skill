"""首次初始化：建库、装规则、注册主 agent。

对应 slash 命令 `/am-setup`。**重复执行安全** —— 已建的库只做迁移，
已存在的规则文件默认不覆盖，.gitignore 不写重复行。

注意这里**不写 settings.json**：拦截用的 PreToolUse hook 随插件自带
（`hooks/hooks.json` + `${CLAUDE_PLUGIN_ROOT}`），装上插件即生效，
不需要改用户的配置文件，卸载插件也自动干净。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
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
    parser.add_argument(
        "--vendor",
        action="store_true",
        help="把脚本、hook、四个命令全部复制进项目的 .claude/，"
        "自检通过后自动卸掉插件安装（C 盘那份）",
    )
    parser.add_argument(
        "--keep-plugin",
        action="store_true",
        help="配合 --vendor：落地后保留插件安装，不自动卸载",
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

    # 落地模式：脚本与命令都搬进项目，规则里的路径也跟着指向项目内那份
    base = _plugin_root()
    vendor_note = ""
    retire_note = ""
    hook_note = "随插件自带，已生效（未改动你的 settings.json）"
    if args.vendor:
        vendor_note = _vendor_files(root)
        base = root / ".claude"
        hook_note = _install_hook(root)
        retire_note = _retire_plugin(root, keep=args.keep_plugin)

    # 落地会把规则里的脚本路径改写成项目内的新路径，旧副本里那条必然失效 ——
    # 这时候「保留用户的旧文件」是帮倒忙，worker 照着它跑只会找不到脚本。
    # 所以 --vendor 一律覆盖（手改过的内容会在 _install_rules 里自动备份）。
    rules_path, rules_note = _install_rules(
        conn,
        root,
        force=args.force_rules or args.vendor,
        base=base,
        force_reason="--force-rules" if args.force_rules else "落地重写了脚本路径",
    )
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
            f"  hook        {hook_note}",
            *([f"  落地        {vendor_note}"] if vendor_note else []),
            *([f"  插件安装    {retire_note}"] if retire_note else []),
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

    survivors, probed = _live_workers(conn)
    if not args.force:
        if not probed:
            raise UsageError(
                "探针不可用，无法确认那些 worker 窗口是不是真的关了",
                hint=(
                    "探不到不等于都死了。注册表里的 alive/dead 只是缓存，不能拿来当依据 —— "
                    "据此清掉锁，而对方其实还在改代码，就会覆盖丢代码。"
                    "确认窗口都关了的话加 --force"
                ),
            )
        if survivors:
            raise UsageError(
                f"探到还有 {len(survivors)} 个 worker 活着：{'、'.join(survivors)}",
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
    if survivors and probed:
        steps.insert(
            0,
            f"⚠ 你用了 --force，而探到 {'、'.join(survivors)} 还活着 —— "
            "立刻去那些窗口 /clear 或直接关掉，它们手上的锁已经没了",
        )
    elif survivors:
        steps.insert(
            0,
            f"⚠ 探针当时不可用，{'、'.join(survivors)} 是死是活没确认过 —— "
            "去看一眼那些窗口还在不在，在的话立刻关掉，它们手上的锁已经没了",
        )
    return Result(
        text=join(body, next_steps(steps)),
        data={"reset": True, "before": before, "live_workers": survivors},
    )


def _count(conn, table: str, where: str = "1=1") -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}").fetchone()[0])


def _live_workers(conn) -> tuple[list[str], bool]:
    """当场探一次，返回 (真正还活着的 worker 角色名, 探针是否可用)。

    **不看 registry.status** —— 那个字段是 reap 维护的缓存，两个方向都会过时：
    标着 alive 的可能早就关窗口了（那就该允许删），标着 dead 的也可能又开起来了
    （那就绝不能删）。后一种更危险：它活着在改代码，而我以为它死了，把锁清掉。

    要拿别人的锁开刀，判断依据只能是当场探到的实况，不能是表里的记录。
    """
    registered = registry.workers(conn)  # 全部，不按 status 预先过滤
    if not registered:
        return [], True
    sessions = liveness.list_sessions()
    if sessions is None:
        # 探不到 ≠ 都死了。这里返回全部并标记探针失效，由调用方拒绝执行
        return [r.role for r in registered], False
    alive_ids = {s.session_id for s in sessions}
    return [r.role for r in registered if r.session_id in alive_ids], True


def _plugin_root() -> Path:
    """插件根目录。

    优先用 Claude Code 注入的 CLAUDE_PLUGIN_ROOT；脱离插件直接跑脚本时
    退化成按文件位置推导（scripts/poolkit/commands/setup.py → 上四层）。
    """
    env = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if env and Path(env).exists():
        return Path(env)
    return Path(__file__).resolve().parents[3]


#: 内部记录：上次写进项目的规则文件长什么样。
#: 下划线开头表示「不是给用户调的旋钮」，config 命令不展示（见 db.all_settings）。
_RULES_HASH_KEY = "_rules_hash"


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _install_rules(
    conn,
    root: Path,
    *,
    force: bool,
    base: Path,
    force_reason: str = "--force-rules",
) -> tuple[Path, str]:
    """把规则模板装进项目的 .claude/rules/。

    规则必须落在**项目里**而不是插件里：共用同一个工作目录的所有会话都会加载它，
    这正是 worker 不需要任何 spawn 时注入就知道规矩的原因（§8）。

    但这也意味着项目里那份是**副本** —— 升级插件只换掉模板，副本不会跟着动，
    所以每次 setup 都要决定要不要更新它。光比「内容和模板一不一样」是不够的，
    那分不清下面两种情况，而它们的处理方式相反：

    - 和**上次写入时**一模一样 → 你没动过，只是模板升级了 → 直接更新，不该烦你
    - 和上次写入时也不一样 → 你手改过（加了项目专属规则）→ 不覆盖，保住你的修改

    所以写入时记一个哈希，下次拿它当基准比对。
    """
    source = _plugin_root() / "templates" / "rules" / config.RULES_FILENAME
    target = root / ".claude" / "rules" / config.RULES_FILENAME
    if not source.exists():
        raise UsageError(
            f"找不到规则模板：{source}",
            hint="插件文件不完整，重装一次：claude plugin install agentManager-Skill",
        )

    # 把 ${CLAUDE_PLUGIN_ROOT} 换成真实路径再写进项目。
    #
    # 这一步不能省：`.claude/rules/` 是常驻规则文本，**不是 skill**，
    # Claude Code 不会对它做 ${...} 替换（SKILL.md 才会）；丢给 shell 后
    # 那个变量也不存在，于是路径塌成 "/scripts/pool.py"，worker 一跑就找不到脚本。
    # setup 自己知道插件装在哪，在这里定死最省事，升级后重跑 setup 会自动跟上。
    source_text = source.read_text(encoding="utf-8").replace(
        "${CLAUDE_PLUGIN_ROOT}", base.as_posix()
    )
    # 哈希基于替换后的内容 —— 基准要和真正落盘的东西对齐
    source_hash = _digest(source_text)
    target.parent.mkdir(parents=True, exist_ok=True)

    def _remember() -> None:
        with db.transaction(conn):
            db.set_setting(conn, _RULES_HASH_KEY, source_hash, utcnow())

    def _write(note: str) -> tuple[Path, str]:
        # 覆盖之前先看一眼要盖掉的是不是用户自己写的东西。
        # 判据还是哈希基准：和上次写入时不一样，就说明是他改的，留个 .bak。
        if target.exists():
            current = _digest(target.read_text(encoding="utf-8"))
            if current != db.get_setting(conn, _RULES_HASH_KEY):
                backup = target.with_name(target.name + ".bak")
                shutil.copy2(target, backup)
                note += f"；你改过的那份已备份为 {backup.name}"
        target.write_text(source_text, encoding="utf-8")
        _remember()
        return target, note

    if not target.exists():
        return _write("已写入")

    current_hash = _digest(target.read_text(encoding="utf-8"))
    if current_hash == source_hash:
        if db.get_setting(conn, _RULES_HASH_KEY) != source_hash:
            _remember()  # 老版本装的，补上基准记录
        return target, "已是最新"

    if force:
        # 说清楚是谁触发的覆盖 —— 用户没敲 --force-rules 却看到它，只会困惑
        return _write(f"已覆盖（{force_reason}）")

    recorded = db.get_setting(conn, _RULES_HASH_KEY)
    if recorded == current_hash:
        return _write("已更新到新版规则")
    if not recorded:
        return (
            target,
            "内容与模板不同，且没有历史记录（判断不出是不是你改的），未覆盖；"
            "要用新版加 --force-rules",
        )
    return target, "你手改过，未覆盖；要用新版模板加 --force-rules"


#: hook 覆盖的工具。与 hooks/hooks.json 里的 matcher 保持一致。
HOOK_MATCHER = "Edit|Write|MultiEdit|NotebookEdit|Bash"

#: 识别「本插件写的 hook 条目」用的特征串
_HOOK_MARK = "pretooluse.py"


def _vendor_files(root: Path) -> str:
    """把运行时文件全部复制进项目的 `.claude/`。

    落完之后项目自包含 —— 脚本、hook、四个命令、规则都在项目里，
    C 盘那个插件可以卸载。代价是升级要重新跑一次 `--vendor`。
    """
    src = _plugin_root()
    claude = root / ".claude"
    dst_scripts = claude / "scripts"

    # 整棵树重来，避免上个版本留下的模块混在里面（删掉的命令会变成幽灵）
    shutil.rmtree(dst_scripts, ignore_errors=True)
    shutil.copytree(
        src / "scripts",
        dst_scripts,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )

    # hook 脚本放 scripts/hooks/ —— 它按自身位置找同级的 poolkit
    (dst_scripts / "hooks").mkdir(parents=True, exist_ok=True)
    shutil.copy2(
        src / "hooks" / _HOOK_MARK, dst_scripts / "hooks" / _HOOK_MARK
    )

    # 四个 skill：命令路径写死成项目内的绝对路径。
    # 项目级 skill 放在 .claude/skills/，Claude Code 会自动加载。
    pool_py = (dst_scripts / "pool.py").as_posix()
    installed = 0
    for skill_src in sorted((src / "skills").iterdir()):
        manifest = skill_src / "SKILL.md"
        if not manifest.is_file():
            continue
        dst = claude / "skills" / skill_src.name
        dst.mkdir(parents=True, exist_ok=True)
        (dst / "SKILL.md").write_text(
            manifest.read_text(encoding="utf-8").replace(
                "${CLAUDE_PLUGIN_ROOT}/scripts/pool.py", pool_py
            ),
            encoding="utf-8",
        )
        installed += 1

    _ignore_vendored(root)
    return f".claude/scripts + {installed} 个命令（来自 {src}）"


#: 落地产物：里面写死了本机绝对路径，进版本库对队友毫无意义还会天天冲突
_VENDORED_IGNORES = (
    ".claude/scripts/",
    ".claude/skills/",
    ".claude/settings.local.json",
)


def _ignore_vendored(root: Path) -> None:
    """把落地产物加进 .gitignore。已有的条目不重复写。"""
    path = root / ".gitignore"
    existing: set[str] = set()
    content = ""
    if path.exists():
        content = path.read_text(encoding="utf-8", errors="replace")
        existing = {line.strip().rstrip("/") for line in content.splitlines()}

    missing = [e for e in _VENDORED_IGNORES if e.rstrip("/") not in existing]
    if not missing:
        return

    sep = "" if content.endswith("\n") or not content else "\n"
    path.write_text(
        content + sep + "\n# 多 Agent 编排落地产物（写死了本机路径）\n"
        + "\n".join(missing)
        + "\n",
        encoding="utf-8",
    )


def _install_hook(root: Path, *, remove: bool = False) -> str:
    """把 PreToolUse hook 写进**项目的** settings.local.json。

    落地之后插件会被卸载，`hooks/hooks.json` 也就不再生效，强制层得靠项目
    自己的配置撑着。写 `.local.json` 是因为它不进版本库 —— 这是本机路径。
    """
    script = root / ".claude" / "scripts" / "hooks" / _HOOK_MARK
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

    # 先摘掉本插件的旧条目：重复执行才是幂等的，路径变了也能自动纠正
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
            "hooks": [
                {
                    "type": "command",
                    "command": f'python "{script.as_posix()}"',
                    "timeout": 10,
                }
            ],
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


def _claude_home() -> Path:
    """Claude Code 的配置目录。用户可能用 CLAUDE_CONFIG_DIR 搬到别的盘。"""
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(override) if override else Path.home() / ".claude"


def _is_installed_plugin(src: Path) -> bool:
    """脚本是不是来自「插件安装目录」。

    这道判断关系到会不会误删东西：用 `--plugin-dir` 从你自己的仓库跑落地时，
    `_plugin_root()` 指的是那个仓库 —— 那是源码，绝不能碰。
    只有落在 <配置目录>/plugins/ 下面的才是装出来的副本，才可以清掉。
    """
    try:
        src.resolve().relative_to((_claude_home() / "plugins").resolve())
    except (ValueError, OSError):
        return False
    return True


def _verify_vendored(root: Path) -> tuple[bool, str]:
    """跑一次落地的脚本，确认它能独立工作。

    卸插件之前必须过这一关 —— 万一落地少了文件又把插件删了，用户两头空。
    刻意清掉 CLAUDE_PLUGIN_ROOT 再跑，模拟插件已经不在的处境。
    """
    script = root / ".claude" / "scripts" / "pool.py"
    if not script.is_file():
        return False, "找不到落地后的 pool.py"

    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PLUGIN_ROOT"}
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        proc = subprocess.run(
            [sys.executable, str(script), "--project-root", str(root), "config"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=60,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        return False, str(exc)
    if proc.returncode != 0:
        return False, ((proc.stderr or proc.stdout).strip() or "退出码非 0")[:200]
    return True, ""


def _plugin_name(src: Path) -> str:
    manifest = src / ".claude-plugin" / "plugin.json"
    try:
        return json.loads(manifest.read_text(encoding="utf-8"))["name"]
    except Exception:
        return src.parent.parent.name if src.parent.name else src.name


def _retire_plugin(root: Path, *, keep: bool) -> str:
    """落地成功后把插件安装清掉 —— 这是用户要「C 盘不占空间」的最后一步。"""
    src = _plugin_root()
    if not _is_installed_plugin(src):
        return f"未动（脚本来自 {src}，不是插件安装目录）"

    ok, detail = _verify_vendored(root)
    if not ok:
        return f"**保留** —— 落地自检没通过（{detail}），不敢卸"
    if keep:
        return "按 --keep-plugin 保留"

    name = _plugin_name(src)
    exe = os.environ.get("CLAUDE_CODE_EXECPATH") or shutil.which("claude")
    if exe:
        for argv in (
            ["plugin", "uninstall", name],
            ["plugin", "marketplace", "remove", name],
        ):
            try:
                subprocess.run([exe, *argv], capture_output=True, timeout=120)
            except (subprocess.SubprocessError, OSError):
                pass  # 登记清不掉也没关系，下面直接删目录

    home = _claude_home() / "plugins"
    leftover = []
    for directory in (home / "cache" / name, home / "marketplaces" / name):
        if not directory.exists():
            continue
        shutil.rmtree(directory, ignore_errors=True)
        if directory.exists():
            leftover.append(directory)

    if leftover:
        # 正在跑的就是 cache 里这份脚本，Windows 下 .pyc 可能还被占着
        return (
            "已卸载，但有目录没删干净（本次运行就在里面）："
            + "、".join(str(p) for p in leftover)
            + " —— 重开窗口后删即可"
        )
    return f"已卸载并清空 C 盘目录（{name}）"


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
