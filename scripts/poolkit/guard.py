"""hook 判定：check_edit / check_bash。

╔══════════════════════════════════════════════════════════════════════════╗
║ 硬约束：**本模块不准 import liveness**                                   ║
║                                                                          ║
║ 这里是热路径 —— 每一次 Edit / Write 都要跑一遍。liveness 要调             ║
║ `claude agents --json` 子进程，实测 1 秒以上，挂在这条路上等于给用户的    ║
║ 每次编辑都加一秒。存活探针只准在 `reap` 命令（慢路径）里调。              ║
║                                                                          ║
║ 这条约束由 tests/test_guard.py 的模块依赖检查强制，不靠这段注释。         ║
╚══════════════════════════════════════════════════════════════════════════╝

判定结果只依赖本地 SQLite 的两次索引查询，全程无子进程、无网络。
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from . import claims as claims_mod
from . import config, db, registry
from .models import Registration, TaskStatus, utcnow


@dataclass(slots=True)
class Decision:
    """一次 hook 判定的结果。"""

    allowed: bool
    reason: str = ""
    #: 附加提示，放行时也可能有（例如「已自动为你声明该文件」）
    note: str = ""

    def to_dict(self) -> dict[str, object]:
        return {"allowed": self.allowed, "reason": self.reason, "note": self.note}


ALLOW = Decision(allowed=True)


# --------------------------------------------------------------------------
# 入口辅助
# --------------------------------------------------------------------------


def try_open(
    cwd: str | None, *, db_file: str | Path | None = None
) -> sqlite3.Connection | None:
    """给 hook 用的宽容打开：当前项目没初始化过 am 就返回 None（=放行）。

    插件级 hook 对**所有项目**生效，绝大多数项目根本没跑过 setup。
    这个函数因此必须极快地判否 —— 纯文件系统查找，不碰 git 子进程。

    ``db_file`` 让调用方把已经找到的账本路径传进来，省掉重复查找；
    hook 入口就是这么用的（它在导入本模块之前已经找过一次）。
    """
    if db_file is not None:
        path = Path(db_file)
    else:
        root = config.find_project_root(cwd)
        if root is None:
            return None
        path = config.db_path(root)
    if not path.exists():
        return None
    try:
        return db.connect(path)
    except Exception:
        # 库损坏、权限不足一类的意外绝不能卡住用户的编辑
        return None


def whoami(conn: sqlite3.Connection, session_id: str | None) -> Registration | None:
    """本会话对应的角色。没注册就是 None —— 临时开的普通会话不受这套约束。"""
    if not session_id:
        return None
    return registry.by_session(conn, session_id)


# --------------------------------------------------------------------------
# Edit / Write：锁判定
# --------------------------------------------------------------------------


def check_edit(
    conn: sqlite3.Connection,
    *,
    session_id: str | None,
    file_path: str,
    auto_claim: bool = True,
) -> Decision:
    """改文件前的锁判定。

    - 未注册会话 → 放行（这套规则只约束在册的 main / worker）
    - 目标文件被**别人**活跃声明 → 否决，理由里给出持有者的**会话名**，
      对方拿到就能直接发消息过去协商（§6.4）
    - 自己已声明 → 放行
    - 无人声明 且 该角色有在办任务 → **自动补一条声明**再放行

    最后那条是刻意的：设计要求「改代码前先声明」，但靠自觉必然会漏，
    一漏锁就形同虚设。自动补声明让锁始终成立，同时在 note 里说明，行为可见。
    """
    me = whoami(conn, session_id)
    if me is None:
        return ALLOW

    key = config.normalize_path(file_path)

    # 库自己的文件不参与锁判定，否则 am 写库会被自己拦住
    if _is_am_internal(key):
        return ALLOW

    scratch = _scratch_check(conn, key, file_path)
    if scratch is not None:
        return scratch

    holder = claims_mod.active_for(conn, key)
    if holder is not None:
        if holder.worker == me.role:
            return ALLOW
        return _deny_locked(conn, holder, me)

    if not auto_claim:
        return ALLOW

    from . import ledger  # 局部 import：避免模块级环依赖，不是为了省时间

    task = ledger.open_task_of(conn, me.role)
    if task is None or task.status is TaskStatus.QUEUED:
        # 没有在办任务，无处挂声明。放行，但提醒一句。
        return Decision(
            allowed=True,
            note=f"{me.role} 当前没有在办任务，本次修改未登记声明（不受锁保护）",
        )

    return _auto_claim(conn, me=me, task_id=task.id, key=key, display=file_path)


def _deny_locked(
    conn: sqlite3.Connection, holder: claims_mod.Claim, me: Registration
) -> Decision:
    address = registry.address_of(conn, holder.worker) or holder.worker
    scope = f"（声明范围：{holder.scope}）" if holder.scope else ""
    return Decision(
        allowed=False,
        reason=(
            f"{Path(holder.display_path).name} 已被 {holder.worker} 声明"
            f"（任务 #{holder.task_id}）{scope}。\n"
            f"共用工作区里直接改会覆盖对方的改动、且 git 无从介入，所以这里必须拦。\n\n"
            f"下一步：直接给 `{address}` 发消息，说明你要改哪个方法、为什么。\n"
            f"  · 真冲突 → 等对方交付、审批通过后锁自动释放\n"
            f"  · 假冲突（改的是不同 region）→ 让对方交接锁：\n"
            f"      python pool.py handoff {holder.display_path} --to {me.role}\n"
            f"  · 谈不拢 → 报主 agent 仲裁"
        ),
    )


def _auto_claim(
    conn: sqlite3.Connection,
    *,
    me: Registration,
    task_id: int,
    key: str,
    display: str,
) -> Decision:
    """无人声明时补一条。

    唯一索引兜底：万一在这几毫秒里被别人抢先，INSERT 会失败，此时按撞锁处理。
    """
    now = utcnow()
    try:
        with db.transaction(conn):
            conn.execute(
                "INSERT INTO claims(task_id, worker, file_path, display_path, claimed_at) "
                "VALUES (?,?,?,?,?)",
                (task_id, me.role, key, display, now),
            )
            db.log_event(
                conn,
                kind="declare.auto",
                now=now,
                actor=me.role,
                session_id=me.session_id,
                task_id=task_id,
                detail=display,
            )
    except sqlite3.IntegrityError:
        holder = claims_mod.active_for(conn, key)
        if holder is not None and holder.worker != me.role:
            return _deny_locked(conn, holder, me)
        return ALLOW
    except sqlite3.Error:
        # 写不进去不代表要拦人，放行即可，最坏是这次修改没被锁保护
        return ALLOW

    return Decision(
        allowed=True,
        note=f"已自动为 {me.role} 声明 {Path(display).name}（任务 #{task_id}）",
    )


def _scratch_check(
    conn: sqlite3.Connection, key: str, display: str
) -> Decision | None:
    """临时文件不许往系统 Temp 写，返回 None 表示这条规则不适用。

    这是一条**规则**，不是一次决策 —— 所以由程序直接判掉，既不惊动用户
    （每次都弹一个权限框），也不惊动主 agent（它判断的依据比用户还少）。

    只在配置了 ``scratch_dir`` 时启用；对所有角色一视同仁，包括主 agent。
    """
    if not config.is_system_temp(key):
        return None
    scratch = db.get_setting(conn, "scratch_dir").strip()
    if not scratch:
        return None
    # 目标本身就在指定的暂存区里（有人把 scratch_dir 设进了 Temp）就别拦了
    if config.normalize_path(scratch) in key:
        return None
    return Decision(
        allowed=False,
        reason=(
            f"{Path(display).name} 要写进系统临时目录，本项目约定临时文件一律放：\n"
            f"    {scratch}\n\n"
            f"改用那个路径重试即可。系统 Temp 会被清理工具随时清掉，"
            f"路径里的 8.3 短名（ADMINI~1 这种）还会触发 Claude Code 的可疑路径检查，"
            f"每次都要人工点同意。\n"
            f"（这条约定可改： python pool.py config set scratch_dir <路径>，"
            f"留空即关闭本规则）"
        ),
    )


#: 规范化后的 ".claude\am"（Windows）/ ".claude/am"（POSIX）
_AM_MARKER = os.path.normcase(os.path.join(*config.AM_DIR_PARTS))


def _is_am_internal(normalized: str) -> bool:
    """账本自己的文件不参与锁判定，否则 am 写库会被自己拦下。"""
    return _AM_MARKER in normalized


# --------------------------------------------------------------------------
# Bash：权限收归主 agent
# --------------------------------------------------------------------------


def check_bash(
    conn: sqlite3.Connection, *, session_id: str | None, command: str
) -> Decision:
    """构建与 git 写操作只准主 agent 执行。

    不靠自觉，靠这里硬拦。匹配整条命令串而非只看开头 ——
    `cd src && dotnet build` 必须同样被拦住。
    """
    me = whoami(conn, session_id)
    if me is None or not me.is_worker:
        return ALLOW

    for pattern, why in config.DENIED_BASH:
        match = pattern.search(command)
        if match is None:
            continue
        main_address = registry.address_of(conn, config.MAIN_ROLE) or "主 agent"
        return Decision(
            allowed=False,
            reason=(
                f"`{match.group(0)}` 属于 worker 禁用操作：{why}。\n\n"
                f"下一步：把这件事报给主 agent（`{main_address}`）执行。\n"
                f"交付时在摘要里写明「是否已编译验证：否」，"
                f"由主 agent 在审批通过后统一构建。"
            ),
        )
    return ALLOW
