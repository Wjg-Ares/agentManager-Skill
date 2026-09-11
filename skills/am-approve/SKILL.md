---
name: am-approve
description: 用户对某次交付说「满意」时用 —— 打结束标记、释放该任务占的文件锁、腾出 worker slot，并把队列里的待办连同「1 执行 / 2 删除 / 3 改优先级 / 0 不处理」一起摆给用户。用户每次表示满意都要走这条。
allowed-tools:
  - Bash(python "${CLAUDE_PLUGIN_ROOT}/scripts/pool.py" *)
---

# /am-approve — 审批并推进队列

用户对某次交付表示满意（「可以」「行」「过」「没问题」都算）时，执行：

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/pool.py" approve <任务ID>
```

**把输出原样转达给用户，包括那几个选项。** 队列待办、可选项、每个选项对应的命令
都已经在输出里了，不需要你另外组织。

然后：

1. **等用户选**，别抢跑。用户选了几就执行输出里对应的那条命令。
2. 审批通过意味着这次改动可以构建了 —— 你（主 agent）执行构建验证。
3. 通知那个 worker 可以 `/clear` 了。

用户**不满意**时不要用这条，用 `P reject <任务ID> "用户的原话"`，
任务会回到同一个 worker（换人的话追问上下文就没了）。
