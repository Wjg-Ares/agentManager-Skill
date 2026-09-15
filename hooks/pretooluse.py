#!/usr/bin/env python3
"""PreToolUse hook：并发写拦截 + worker 禁用命令拦截。

随插件自带（见 hooks/hooks.json），装上插件即生效，不需要改用户的 settings.json。

因此它对**所有项目**都会跑一遍，第一件事必须是极快地判否：当前项目没有
`.claude/am/pool.db` 就立刻放行。纯文件系统查找，不碰 git、不碰 claude CLI。

这里直接 import guard 而不是去调 pool.py，省掉一次 Python 进程启动 ——
这是每次 Edit / Write 都要走的路。

**永远 exit 0**：hook 自身出任何问题都不该卡住用户的编辑。要拦是靠 stdout 里的
permissionDecision，不是靠退出码。
"""

from __future__ import annotations

import json
import os
import re
import sys

# Windows 下 stdout 默认 GBK，而这里吐出的 JSON 带中文拒绝理由，直接 print 会
# UnicodeEncodeError —— hook 一崩，这次 Edit 就失去了保护（比没装还糟：
# 用户以为有锁）。强制 UTF-8，Claude Code 本来就按 UTF-8 读 hook 输出。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

#: 账本相对项目根的位置。与 poolkit.config.ROOT_ANCHORS[0] 必须一致，
#: tests/test_hook.py 会断言这一点。
_LEDGER_PARTS = (".claude", "am", "pool.db")


def _allow() -> None:
    print("{}")
    sys.exit(0)


def _find_ledger(cwd: str) -> str | None:
    """从 cwd 向上找账本文件。

    **刻意不 import poolkit** —— 导入那个包要 40 多毫秒，而绝大多数 Edit 发生在
    跟本插件无关的项目里，那笔开销会白白加在用户每一次编辑上。
    这里只用 os.path，找不到就直接放行退出。
    """
    directory = os.path.abspath(cwd)
    while True:
        candidate = os.path.join(directory, *_LEDGER_PARTS)
        if os.path.exists(candidate):
            return candidate
        parent = os.path.dirname(directory)
        if parent == directory:
            return None
        directory = parent


def _grant(reason: str) -> None:
    """**主动**放行，跳过权限弹窗。

    和 `_allow()` 是两回事，这个区别是整个快速通道的立足点：
      - `_allow()` 打印 `{}` —— 「我不表态」，Claude Code 接着走它原有的权限规则，
        该弹窗还是弹窗。这是所有兜底路径该走的。
      - `_grant()` 打印 permissionDecision=allow —— 「我批了」，不弹窗。
    所以「不是子 agent 就维持原先的权限行为」不需要写任何代码，
    只要不进这个函数就是了。
    """
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                    "permissionDecisionReason": reason,
                }
            },
            ensure_ascii=False,
        )
    )
    sys.exit(0)


#: 命令里出现这些就不走快速通道 —— 放行是白名单，一旦能拼接就等于放行任意命令
_CHAINING = ("&&", "||", ";", "|", "`", "$(", ">", "<", "\n", "\r")

#: Claude Code 习惯在命令前面加 `cd "<项目>" &&`，这一段要允许，但仅此一段
_CD_PREFIX_RE = re.compile(
    r"""^\s*cd\s+(?:"[^"]*"|'[^']*'|[^\s&|;<>]+)\s*&&\s*""")

#: 形如 `python <任意路径>/pool.py ...`。解释器名要匹配上，
#: 否则 `echo pool.py` 这种也会被当成自己人。
_POOL_CMD_RE = re.compile(
    r"""^\s*"?[^"\s]*python[0-9.]*(?:\.exe)?"?\s+"?[^"\s]*pool\.py"?(?:\s|$)""",
    re.IGNORECASE,
)


def _is_pool_command(command: str) -> bool:
    """这条 Bash 是不是「单纯在调本工具的 pool.py」。"""
    rest = _CD_PREFIX_RE.sub("", command, count=1)
    if any(token in rest for token in _CHAINING):
        return False
    return bool(_POOL_CMD_RE.match(rest))


#: 只读 transcript 末尾这么多字节 —— 会话文件能到几十 MB，全读进来太贵
_TAIL_BYTES = 65536


def _is_subagent(payload: dict) -> bool:
    """判断触发本次工具调用的是不是子 agent（Task 工具起的那种）。

    依据是 transcript 里的 `isSidechain`：子 agent 的记录会标 true。
    **这条判据尚未在真实子 agent 上验证过** —— 这台开发机的 transcript 里
    该字段全是 false（从没跑过子 agent），没有正样本可比对。
    所以整条路径的失败方向被刻意设计成「返回 False」：认不出来就是不放行，
    退回原有的权限弹窗，也就是加这段之前的行为。宁可少放行，不可乱放行。

    要确认字段对不对，设环境变量 AM_HOOK_DEBUG=<日志路径> 跑一次，
    对比子 agent 和主 agent 两边的 payload。
    """
    path = payload.get("transcript_path")
    if not path or not os.path.isfile(path):
        return False
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - _TAIL_BYTES))
            chunk = handle.read()
    except OSError:
        return False

    # 从后往前找最近一条带该字段的记录：当前上下文是主是子，最新那条说了算
    for raw in reversed(chunk.splitlines()):
        if b'"isSidechain"' not in raw:
            continue
        try:
            entry = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            continue  # 尾部截断的半行，跳过
        return bool(entry.get("isSidechain"))
    return False


def _debug_dump(payload: dict) -> None:
    """设了 AM_HOOK_DEBUG 就把原始 payload 追加到该文件。

    留这个口子是因为子 agent 的判据还没验；在真实环境里跑一次就能定下来。
    """
    target = os.environ.get("AM_HOOK_DEBUG")
    if not target:
        return
    try:
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _deny(reason: str) -> None:
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                },
                "systemMessage": reason,
            },
            ensure_ascii=False,
        )
    )
    sys.exit(0)


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        _allow()

    tool = payload.get("tool_name", "")
    tool_input = payload.get("tool_input") or {}
    session_id = payload.get("session_id") or os.environ.get("CLAUDE_CODE_SESSION_ID")
    cwd = payload.get("cwd") or os.getcwd()

    _debug_dump(payload)

    if tool != "Bash" and tool not in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        _allow()

    # 快速通道：子 agent 调本工具自己的 pool.py，不必弹窗问用户。
    #
    # 放在找账本之前 —— 这两个判断都是纯字符串匹配，比向上遍历目录便宜；
    # 而且 pool.py 是账本工具本身，它的读写走的是自己的事务，不需要过并发锁那一关。
    #
    # 三个条件缺一不可，任一不满足就往下走原有流程（该弹窗弹窗、该拦截拦截）。
    if tool == "Bash" and _is_pool_command(tool_input.get("command") or ""):
        if _is_subagent(payload):
            _grant("子 agent 调用本工具的 pool.py，无需人工确认")

    # 先判否，再谈其他：没有账本就跟本插件无关，此时连 poolkit 都不导入
    ledger_file = _find_ledger(cwd)
    if ledger_file is None:
        _allow()

    # 按**自己的位置**找 poolkit，支持两种布局：
    #   插件：  <插件根>/hooks/pretooluse.py        → <插件根>/scripts/poolkit
    #   落地：  <项目>/.claude/scripts/hooks/...    → <项目>/.claude/scripts/poolkit
    # 刻意不读 CLAUDE_PLUGIN_ROOT —— 落地之后那个变量仍指向 C 盘的插件副本，
    # 跟着它走就会加载到另一份代码（甚至是已卸载的残留）。
    _here = os.path.dirname(os.path.abspath(__file__))
    for _candidate in (
        _here,                                                    # 同级就有 poolkit
        os.path.dirname(_here),                                   # scripts/hooks → scripts
        os.path.join(os.path.dirname(_here), "scripts"),          # <根>/hooks → <根>/scripts
    ):
        if os.path.isdir(os.path.join(_candidate, "poolkit")):
            sys.path.insert(0, _candidate)
            break
    else:
        _allow()

    try:
        from poolkit import guard
    except Exception:
        _allow()

    conn = guard.try_open(cwd, db_file=ledger_file)
    if conn is None:
        _allow()

    try:
        if tool == "Bash":
            command = tool_input.get("command") or ""
            if not command:
                _allow()
            decision = guard.check_bash(
                conn, session_id=session_id, command=command
            )
        else:
            file_path = (
                tool_input.get("file_path")
                or tool_input.get("notebook_path")
                or ""
            )
            if not file_path:
                _allow()
            decision = guard.check_edit(
                conn, session_id=session_id, file_path=file_path
            )
    except Exception:
        # 判定本身出错 → 放行。锁的作用是防丢代码，不是给用户添堵；
        # 出错时拦住一切会让整个工作区瘫掉，那比漏拦一次更糟。
        _allow()
    finally:
        try:
            conn.close()
        except Exception:
            pass

    if decision.allowed:
        if decision.note:
            print(json.dumps({"systemMessage": decision.note}, ensure_ascii=False))
            sys.exit(0)
        _allow()
    _deny(decision.reason)


if __name__ == "__main__":
    main()
