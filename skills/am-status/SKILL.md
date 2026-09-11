---
name: am-status
description: 主 agent 的调度台 —— 查看各 worker 在干什么、谁待审批、队列还剩什么，以及建任务、派活、查交付、回收死掉的 worker。compact 之后调一次即可完全恢复调度状态。
allowed-tools:
  - Bash(python "${CLAUDE_SKILL_DIR}/../am-setup/scripts/pool.py" *)
---

# /am-status — 主 agent 调度台

`P` 代表 `python "${CLAUDE_SKILL_DIR}/../am-setup/scripts/pool.py"`。

先跑这个，**它的输出自带「下一步」，照做即可**：

```bash
P status
```

| 要做的事 | 命令 |
|---|---|
| 看全局（含下一步指引） | `P status` |
| 顺带检查 worker 死没死 | `P status --check-live` |
| 用户提了新需求 | `P add "标题" --detail "完整描述"` |
| 派活 | `P dispatch <任务ID> --to worker-N`（省略 `--to` 自动挑空闲的） |
| 看某次交付的**完整**产出 | `P show <交付ID>` |
| 产出很长，不想进上下文 | `P show <交付ID> --out D:\claude-tmp\d<ID>.md` |
| 用户不满意 | `P reject <任务ID> "用户的原话"` |
| 队列维护 | `P queue`、`P queue --priority <ID> <数字>`、`P queue --cancel <ID>` |
| worker 疑似卡死 | `P reap` |
| 排查「这事怎么发生的」 | `P log --task <ID>` |

用户说满意时**不要**用这里的命令 —— 走 `/am-approve`，那条会把队列待办和选项一起给出来。

## 三条不能破的

1. **只吃摘要**。worker 交付回来的是五行摘要 + 交付 ID，不要主动 `show` 全文；
   用户问细节了再查。这是防止你的上下文被 3 路产出撑爆的唯一措施。
2. **派活前看一眼 `status` 里各 worker 已声明的文件**，主动避开，冲突在派活阶段
   就能消掉一半。
3. **构建和 git 写操作是你的活**。worker 那边被 hook 拦着，它们报上来你来执行。
   审批通过 → 你构建 → 通过才算真完成。
