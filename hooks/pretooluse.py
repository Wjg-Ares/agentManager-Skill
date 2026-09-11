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

    if tool != "Bash" and tool not in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        _allow()

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
