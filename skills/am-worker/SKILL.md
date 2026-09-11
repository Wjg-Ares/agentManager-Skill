---
name: am-worker
description: worker 会话用的一组动作 —— 注册角色、查自己在干什么、声明将要改的文件（即上锁）、交付产出、撞锁后交接。共用工作目录下改代码前必须先声明文件，否则会覆盖别人的改动。
allowed-tools:
  - Bash(python "${CLAUDE_SKILL_DIR}/../am-setup/scripts/pool.py" *)
---

# /am-worker — worker 侧动作

`P` 代表 `python "${CLAUDE_SKILL_DIR}/../am-setup/scripts/pool.py"`。

| 场景 | 命令 |
|---|---|
| 刚开窗口 / 重启后 | `P register worker-1`（2、3 同理） |
| 忘了自己是谁、在干什么 | `P whoami` |
| **动代码之前** | `P declare <文件路径...>`（可反复追加） |
| 声明时想说清改哪块 | `P declare <文件> --scope "CollectTask.Save 方法"` |
| 交付 | `P deliver <任务ID> --changed "..." --impact "..." --risk "无" --confirm "无" --content-file <产出文件>` |
| 撞锁、协商后判定是假冲突 | 由**持有者**执行 `P handoff <文件> --to worker-N` |

`$ARGUMENTS` 是子命令时（如 `/am-worker register worker-2`）直接把它拼到 `P` 后面执行。

**每条命令的输出都写好了下一步，照做即可。**

## 撞锁了怎么办

`declare` 失败或 hook 拦下你时，输出里会给出**持有者的会话名** —— 那就是地址，
直接发跨会话消息过去：说明你要改哪个方法、为什么。

- 真冲突 → 等对方交付并通过审批，锁自动释放
- 假冲突（改的是同一文件的不同 region）→ 请对方 `handoff` 给你
- 谈不拢 → 报主 agent 仲裁；还定不了 → 主 agent 问用户

## 三条硬规矩

1. **不许构建、不许 git 写**（`dotnet build/run/test`、`git commit/checkout/stash`…）。
   hook 会拦。这些报给主 agent 执行。
2. **交付只发五行摘要 + 交付 ID 给主 agent，不要把完整产出贴过去**。
   `deliver` 的输出里已经把该发的那段整理好了，照抄。
3. **用户说满意之前不要 `/clear`**。追问的上下文只在你这里，清了就没了。
   审批通过后主 agent 会通知你，那时才清。
