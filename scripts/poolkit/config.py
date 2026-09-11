"""路径常量、超时阈值、禁用命令清单。

本模块处于依赖树最底层，**不 import 任何其他 poolkit 模块**。
guard 走热路径（每次 Edit/Write 都跑），这里的一切都必须是纯计算或文件系统操作，
不允许出现子进程调用。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Final

# --------------------------------------------------------------------------
# 目录与文件
# --------------------------------------------------------------------------

AM_DIR_PARTS: Final[tuple[str, ...]] = (".claude", "am")
DB_FILENAME: Final = "pool.db"
RULES_FILENAME: Final = "am-orchestration.md"

#: 向上查找项目根时的锚点，按优先级排列。命中即停。
ROOT_ANCHORS: Final[tuple[tuple[str, ...], ...]] = (
    (".claude", "am", DB_FILENAME),  # 已初始化过的项目，最准
    (".git",),
    (".claude",),
)

# --------------------------------------------------------------------------
# 角色
# --------------------------------------------------------------------------

MAIN_ROLE: Final = "main"
WORKER_PREFIX: Final = "worker-"

#: 角色名合法性：main 或 worker-<正整数>
ROLE_RE: Final = re.compile(r"^(main|worker-[1-9]\d*)$")

# --------------------------------------------------------------------------
# 默认配置。写入 settings 表，之后可用 `pool.py config set` 改。
# --------------------------------------------------------------------------

DEFAULT_SETTINGS: Final[dict[str, str]] = {
    "max_workers": "3",  # 并发 worker 上限
    "deliver_timeout_min": "30",  # 派活后多久未交付算卡死
    "max_attempts": "2",  # 重派几次后进死信
    "default_priority": "100",  # 越小越先出队
    # 临时文件的去处。设了它，往系统临时目录写文件会被拦下并改道到这里；
    # 留空则不管（默认不启用 —— 这是个人机器习惯，不该强加给所有人）。
    "scratch_dir": "",
}

#: 只接受整数的配置项，其余按字符串处理
INT_SETTINGS: Final[frozenset[str]] = frozenset(
    {"max_workers", "deliver_timeout_min", "max_attempts", "default_priority"}
)

# --------------------------------------------------------------------------
# 系统临时目录识别
# --------------------------------------------------------------------------

#: 规范化路径里出现这些片段就算系统临时目录。
#:
#: 用路径片段而不是 tempfile.gettempdir() 的完整值，是因为 Windows 会给出
#: 8.3 短名（`C:\Users\ADMINI~1\AppData\Local\Temp`），而实际写入用的多半是
#: 长名，两者字符串不等 —— 匹配中间这段才两种写法都能覆盖。
TEMP_PATH_MARKERS: Final[tuple[str, ...]] = (
    os.path.normcase(os.path.join("appdata", "local", "temp")),
    os.path.normcase(os.path.join("appdata", "roaming", "temp")),
    os.path.normcase(os.path.join("windows", "temp")),
    os.path.normcase(os.path.join("windows", "tmp")),
)


def is_system_temp(normalized_path: str) -> bool:
    """判断一个**已规范化**的路径是否落在系统临时目录里。"""
    return any(marker in normalized_path for marker in TEMP_PATH_MARKERS)

# --------------------------------------------------------------------------
# SQLite 连接
# --------------------------------------------------------------------------

BUSY_TIMEOUT_MS: Final = 5000
SCHEMA_VERSION: Final = 1

# --------------------------------------------------------------------------
# worker 禁用命令清单（写死，用户明确要求不做成可配）
#
# 匹配的是整条 Bash 命令串，不只是开头 —— `cd foo && dotnet build` 必须也被拦住。
# --------------------------------------------------------------------------

DENIED_BASH: Final[tuple[tuple[re.Pattern[str], str], ...]] = (
    (
        re.compile(r"\bdotnet\s+(build|run|publish|test|restore|watch|clean|pack)\b", re.I),
        "构建权在主 agent —— 并发构建会互相锁文件、写坏 bin/obj，且报错看不出根因",
    ),
    (
        re.compile(r"\bmsbuild(\.exe)?\b", re.I),
        "构建权在主 agent",
    ),
    (
        re.compile(
            r"\bgit\s+(commit|checkout|switch|stash|rebase|reset|merge|"
            r"cherry-pick|push|pull|clean|revert|apply|am|restore)\b",
            re.I,
        ),
        "git 写操作权在主 agent —— 共用工作区里这些是全局操作，一个 worker 动全员遭殃",
    ),
    (
        re.compile(r"\bnpm\s+(run\s+build|install|ci|publish)\b", re.I),
        "构建/安装权在主 agent",
    ),
    (
        re.compile(r"\b(yarn|pnpm)\s+(build|install|add)\b", re.I),
        "构建/安装权在主 agent",
    ),
)

#: 走锁判定的工具名
GUARDED_EDIT_TOOLS: Final[frozenset[str]] = frozenset(
    {"Edit", "Write", "NotebookEdit", "MultiEdit"}
)


# --------------------------------------------------------------------------
# 路径工具
# --------------------------------------------------------------------------


def normalize_path(path: str | os.PathLike[str]) -> str:
    """把文件路径规范成锁表的 key。

    Windows 下大小写不敏感，`D:\\a\\B.cs` 与 `d:/a/b.cs` 是同一个文件。
    锁靠 `ux_claims_active` 这个字符串唯一索引保证，两种写法不归一就会漏锁 ——
    这是整套并发保护里最容易被忽略的一处。
    """
    return os.path.normcase(os.path.abspath(str(path)))


def find_project_root(start: str | os.PathLike[str] | None = None) -> Path | None:
    """从 start 向上找项目根，找不到返回 None。

    纯文件系统操作，**不调 git 子进程** —— 这个函数在 hook 热路径上。
    """
    current = Path(start or os.getcwd()).resolve()
    for directory in (current, *current.parents):
        for anchor in ROOT_ANCHORS:
            if (directory.joinpath(*anchor)).exists():
                return directory
    return None


def am_dir(project_root: str | os.PathLike[str]) -> Path:
    """项目的 am 数据目录：<项目根>/.claude/am"""
    return Path(project_root).joinpath(*AM_DIR_PARTS)


def db_path(project_root: str | os.PathLike[str]) -> Path:
    """项目的账本路径：<项目根>/.claude/am/pool.db"""
    return am_dir(project_root) / DB_FILENAME


def current_session_id() -> str | None:
    """本会话的 session_id。

    Claude Code 通过环境变量注入，零成本可得 —— 实测 `CLAUDE_CODE_SESSION_ID`。
    hook 走 stdin 传入的 session_id，这个函数是给 CLI 侧用的。
    """
    return os.environ.get("CLAUDE_CODE_SESSION_ID") or None
