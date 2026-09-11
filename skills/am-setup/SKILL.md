---
name: am-setup
description: 多 Agent 协作编排的首次初始化 —— 建 SQLite 账本、装常驻规则、装并发写拦截 hook、把本会话注册成主 agent。在某个项目里第一次使用这套编排前必须先跑一次；可重复执行。
disable-model-invocation: true
allowed-tools:
  - Bash(python "${CLAUDE_SKILL_DIR}/scripts/pool.py" *)
---

# /am-setup

执行：

```bash
python "${CLAUDE_SKILL_DIR}/scripts/pool.py" setup
```

带参数时（`$ARGUMENTS`）原样传过去。可用：`--role worker-1`（把本会话直接注册成
worker 而非 main）、`--force-rules`（覆盖已存在的规则文件）、`--no-gitignore`、
`--remove-hook`（卸载时撤掉拦截 hook）。

**照它的输出做，不要自己发挥。** 输出里已经写好了下一步 —— 包括是否需要先重开窗口、
开几个 worker 窗口、每个窗口里敲什么。
