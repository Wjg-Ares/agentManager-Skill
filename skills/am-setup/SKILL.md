---
name: am-setup
description: 多 Agent 协作编排的首次初始化 —— 建 SQLite 账本、装常驻规则、把本会话注册成主 agent。在某个项目里第一次使用这套编排前必须先跑一次；可重复执行。
disable-model-invocation: true
allowed-tools:
  - Bash(python "${CLAUDE_PLUGIN_ROOT}/scripts/pool.py" *)
---

# /am-setup

执行：

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/pool.py" setup
```

带参数时（`$ARGUMENTS`）原样传过去。可用：

| 参数 | 用途 |
|---|---|
| `--scratch-dir <路径>` | 临时文件的去处，设了之后往系统 Temp 写会被拦下 |
| `--role worker-1` | 把本会话注册成 worker 而不是 main |
| `--force-rules` | 覆盖已存在的规则文件 |
| `--no-gitignore` | 不改 .gitignore |
| `--reset` | **清空账本从头来**，任务/交付/锁/注册/审计全删，配置保留 |

**`--reset` 是破坏性的**，只在用户明确说「清空重来」时才用。主 agent 重开窗口
**不需要** reset —— 直接跑 `/am-setup` 就会把地址更新过来，账本留着。
它会先检查有没有 worker 还活着，有就拒绝并说明原因，别自作主张加 `--force`。

**照它的输出做，不要自己发挥。** 输出里已经写好了下一步（开几个 worker 窗口、
每个窗口里敲什么）。

拦截用的 PreToolUse hook 随插件自带，setup 不会改动用户的 `settings.json`。
