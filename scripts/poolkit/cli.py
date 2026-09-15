"""CLI 骨架：命令自动注册、上下文、统一异常出口。

**新增一个 `commands/xxx.py` 就自动可用**，核心里没有一处 if-else 分发。
命令模块只做编排，业务逻辑在 registry / ledger / claims / guard 里。

命令模块的契约：

    NAME = "status"                     # 可选，缺省用模块名（下划线转连字符）
    HELP = "一句话说明"
    def add_arguments(parser): ...      # 可选
    def run(ctx: Context, args) -> Result
"""

from __future__ import annotations

import argparse
import importlib
import json
import pkgutil
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Sequence

from . import commands as commands_pkg
from . import config, db
from .errors import AmError, NotSetUp, UsageError
from .render import Result


@dataclass(slots=True)
class Context:
    """命令的执行上下文。连接是懒开的 —— 不是每个命令都要碰库。"""

    project_root: Path | None
    session_id: str | None
    as_json: bool
    _conn: sqlite3.Connection | None = None

    def connect(self, *, create: bool = False) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        if self.project_root is None:
            raise NotSetUp("定位不到项目根目录")
        path = config.db_path(self.project_root)
        if not path.exists() and not create:
            raise NotSetUp()
        self._conn = db.connect(path, create=create)
        if create:
            db.migrate(self._conn)
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None


def discover() -> dict[str, ModuleType]:
    """扫描 commands 包，返回 {命令名: 模块}。"""
    found: dict[str, ModuleType] = {}
    for info in pkgutil.iter_modules(commands_pkg.__path__):
        if info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{commands_pkg.__name__}.{info.name}")
        if not hasattr(module, "run"):
            continue
        name = getattr(module, "NAME", info.name.replace("_", "-"))
        found[name] = module
    return dict(sorted(found.items()))


def build_parser(modules: dict[str, ModuleType]) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pool.py",
        description="多 Agent 协作编排账本。对外的 slash 命令见 /am-setup /am-status "
        "/am-worker /am-approve；这里是它们底下的 CLI。",
    )
    parser.add_argument("--json", action="store_true", help="输出 JSON（hook 与脚本用）")
    parser.add_argument(
        "--project-root", help="项目根目录，缺省自动向上查找 .claude/am 或 .git"
    )
    subparsers = parser.add_subparsers(dest="_command", metavar="<命令>")
    for name, module in modules.items():
        sub = subparsers.add_parser(
            name,
            help=getattr(module, "HELP", ""),
            description=getattr(module, "HELP", ""),
        )
        adder = getattr(module, "add_arguments", None)
        if adder is not None:
            adder(sub)
        sub.set_defaults(_module=module)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    modules = discover()
    parser = build_parser(modules)
    args = parser.parse_args(argv)

    if not getattr(args, "_command", None):
        parser.print_help()
        return 0

    root = Path(args.project_root).resolve() if args.project_root else config.find_project_root()
    ctx = Context(
        project_root=root,
        session_id=config.current_session_id(),
        as_json=bool(args.json),
    )

    try:
        result = args._module.run(ctx, args)
    except AmError as exc:
        _emit_error(exc, as_json=ctx.as_json)
        return exc.exit_code
    except KeyboardInterrupt:
        return 130
    except sqlite3.OperationalError as exc:
        # 并发下最常见的一类，单独给个能看懂的说法
        _emit_error(
            UsageError(f"数据库忙或不可用：{exc}", hint="稍后重试；持续出现请检查 .claude/am/pool.db"),
            as_json=ctx.as_json,
        )
        return 1
    else:
        _auto_reap(ctx, args._command)
    finally:
        ctx.close()

    _emit(result, as_json=ctx.as_json)
    return result.exit_code


#: 两次自动回收之间至少隔这么久。探针要起 `claude agents --json` 子进程（约 1 秒），
#: 挂在每条命令上谁都受不了；而 worker 死了晚几分钟被发现完全可以接受。
_AUTO_REAP_INTERVAL_SEC = 180

#: 上次自动回收的时间。下划线开头 = 不是给用户调的旋钮（config 命令不展示）
_AUTO_REAP_KEY = "_last_auto_reap"


def _auto_reap(ctx: Context, command: str) -> None:
    """顺手回收死掉的 worker。

    账本里写着「任务 #2 在 worker-1 手上」，而 worker-1 的窗口早就关了 ——
    账本不知道，这条任务从此卡死：没人做，也没人能拿走，因为它显示有主。
    `reap` 命令本来就能解决，问题是得有人想起来敲它。

    所以改成每条命令跑完顺手看一眼。**失败绝不能影响本次命令** ——
    用户敲的是 status，不该因为回收出错而拿不到结果。
    """
    if command == "reap":
        return  # 人家就是来干这个的，别重复跑
    try:
        conn = ctx.connect()
    except AmError:
        return  # 还没 setup，没什么可回收的

    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    try:
        last = db.get_setting(conn, _AUTO_REAP_KEY).strip()
        if last:
            previous = datetime.fromisoformat(last)
            if previous.tzinfo is None:
                previous = previous.replace(tzinfo=timezone.utc)
            if now - previous < timedelta(seconds=_AUTO_REAP_INTERVAL_SEC):
                return
    except (ValueError, sqlite3.Error):
        pass  # 记录坏了就当没记过，跑一次即可修正

    from . import liveness

    try:
        # 先记时间再干活：探针万一超时或抛异常，也不至于每条命令都重试一遍
        with db.transaction(conn):
            db.set_setting(conn, _AUTO_REAP_KEY, now.isoformat(), now.isoformat())
        liveness.reap(conn)
    except Exception:
        pass


def _emit(result: Result, *, as_json: bool) -> None:
    if as_json:
        payload = {"ok": result.exit_code == 0, **result.data}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(result.text)


def _emit_error(exc: AmError, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(exc.to_dict(), ensure_ascii=False, indent=2))
        return
    print(f"✗ {exc.message}", file=sys.stderr)
    if exc.hint:
        print(f"  → {exc.hint}", file=sys.stderr)
