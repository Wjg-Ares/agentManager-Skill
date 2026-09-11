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
| `--force-rules` | 强行覆盖项目里的规则文件。**平时不需要**，也**不要和 `--vendor` 叠加**（落地本来就会重写规则路径）—— 只要你没手改过那份文件，跑一次 `/am-setup` 就会自动更新；只有你在里面加过东西时它才会保留不覆盖，那时才用这个。覆盖前会把你改过的那份备份成 `.bak` |
| `--no-gitignore` | 不改 .gitignore |
| `--reset` | **清空账本从头来**，任务/交付/锁/注册/审计全删，配置保留 |
| `--vendor` | **把脚本、hook、四个命令全部复制进项目的 `.claude/`**，自检通过后**自动卸掉 C 盘那份插件安装**。用户说「不想占 C 盘」「装到项目里」就用这个 |
| `--keep-plugin` | 配合 `--vendor`：落地后保留插件安装，不自动卸载 |

**`--reset` 是破坏性的**，只在用户明确说「清空重来」时才用。主 agent 重开窗口
**不需要** reset —— 直接跑 `/am-setup` 就会把地址更新过来，账本留着。
它会先检查有没有 worker 还活着，有就拒绝并说明原因，别自作主张加 `--force`。

**照它的输出做，不要自己发挥。** 输出里已经写好了下一步（开几个 worker 窗口、
每个窗口里敲什么）。

拦截用的 PreToolUse hook 随插件自带，setup 不会改动用户的 `settings.json`。
