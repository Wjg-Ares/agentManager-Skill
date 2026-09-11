# agentManager-Skill

多 Agent 协作编排 Claude Code 技能集。一个主 agent 带最多 3 个 worker，
**在同一个工作目录里**并行干活。

零依赖：只用 Python 标准库，账本落 SQLite（`sqlite3` 是 Python 自带的，
不需要安装任何数据库）。

## 安装

```bash
npx skills add Wjg-Ares/agentManager-Skill
```

装进**当前项目**的 `.claude/skills/`，不碰 C 盘。然后在项目里：

```
/am-setup
```

它会建账本、装规则、把拦截 hook 写进本项目的 `.claude/settings.local.json`，
并告诉你下一步做什么。

> **`/am-setup` 跑完要重开一次窗口** —— hook 刚写进配置，当前会话还没加载它，
> 那之前并发写没有保护。

然后在同一个工作目录另开 1~3 个窗口，每个里面执行：

```
/am-worker register worker-1     # 2、3 同理
```

### 或者装成全局插件

```bash
claude plugin marketplace add Wjg-Ares/agentManager-Skill
claude plugin install agentManager-Skill
```

这条写 `~/.claude`，装一次所有项目都能用，hook 随插件自带（`hooks/hooks.json`），
`/am-setup` 不会去改任何配置文件。代价是 hook 对**所有项目**生效
（无关项目约 69ms/次编辑，直接放行）。

两种形态 `/am-setup` 会自动分辨，输出里的「安装形态」一行会告诉你它认成了哪种。

## 四个技能

| 技能 | 谁用 | 干什么 |
|---|---|---|
| `/am-setup` | 主 agent | 首次初始化，可重复执行 |
| `/am-status` | 主 agent | 调度台：看全局、建任务、派活、查交付、回收死 slot |
| `/am-worker` | worker | 注册、声明文件、交付、交接锁 |
| `/am-approve` | 主 agent | 用户说满意时走这条，顺带把队列待办和选项一起给出来 |

每条命令的输出都自带「下一步」，照做即可 —— SKILL.md 里不写分支判断，
逻辑全在程序里，这样每轮对话烧的 token 最少。

## 它解决的问题

共用一个工作目录时，A 刚写完文件、B 拿旧内容一覆盖，**改动直接消失，
无提示，git 无从介入**。所以：

- **文件声明即锁**。worker 改代码前登记要动的文件，
  `claims` 表上的部分唯一索引保证同一文件同时只有一个活跃声明 ——
  仲裁交给数据库，不在应用层比时间戳，天然没有竞态。
- **`PreToolUse` hook 硬拦**。撞锁时拒绝理由里直接给出持有者的**会话名**，
  两个 worker 拿着地址自己去协商；谈不拢才升级到主 agent。
- **构建和 git 写操作收归主 agent**。并发 `dotnet build` 会写坏 `bin/obj`，
  `git checkout` 一动全员遭殃。worker 那边由 hook 拦着。
- **交付只传摘要 + ID**。完整产出落库，主 agent 只吃五行摘要，
  防止它的上下文被 3 路产出撑爆。
- **队列推进绑定到审批**。用户每次说满意，`/am-approve` 就把队列待办连同
  「1 执行 / 2 删除 / 3 改优先级 / 0 不处理」一起摆出来 ——
  不需要轮询、定时器或任何额外提醒机制。

设计全文见 [`docs/multi-agent-orchestration.md`](docs/multi-agent-orchestration.md)。

## 底层 CLI

四个技能底下是同一个 CLI（在 `skills/am-setup/scripts/pool.py`，另外三个 skill
按相对路径引用它），也可以直接用：

```bash
python skills/am-setup/scripts/pool.py status
python skills/am-setup/scripts/pool.py --json check-edit src/Foo.cs
```

| 命令 | 用途 |
|---|---|
| `setup` | 建库、装规则、装 hook、注册主 agent（`--remove-hook` 撤 hook） |
| `register <角色>` | 登记本会话（worker-1 / main …） |
| `whoami` | 我是谁、在干什么、占着哪些文件 |
| `status` | 全景 + 下一步（`--check-live` 顺带探活） |
| `add <标题>` | 建任务并入队 |
| `dispatch <任务ID> [--to]` | 派活，打开始标记 |
| `declare <文件...>` | 声明将要改的文件（即上锁） |
| `deliver <任务ID> ...` | 交付，返回交付 ID |
| `show <交付ID>` | 查完整产出（`--out` 写文件不进上下文） |
| `approve <任务ID>` | 审批通过 + 列出队列待办与选项 |
| `reject <任务ID> <原因>` | 打回给原 worker |
| `queue` | 队列维护：`--priority` / `--cancel` / `--requeue` / `--revive` |
| `handoff <文件> --to` | 交接锁（协商判定为假冲突时） |
| `reap` | 存活探针，回收死掉的 worker |
| `config` | 改 `max_workers` / `deliver_timeout_min` / `max_attempts` |
| `log` | 审计：谁在何时做了什么 |
| `check-edit` / `check-bash` | hook 用的同一套判定，可手工排查 |

加命令不用改核心：往 `skills/am-setup/scripts/poolkit/commands/` 放一个模块就自动注册。

## 配置

配置存在库里，跟着项目走，不动你的 `settings.json`：

```bash
python skills/am-setup/scripts/pool.py config
python skills/am-setup/scripts/pool.py config set deliver_timeout_min 45
```

| 键 | 默认 | 含义 |
|---|---|---|
| `max_workers` | 3 | 并发 worker 上限 |
| `deliver_timeout_min` | 30 | 派活后多久未交付算卡死 |
| `max_attempts` | 2 | 重派几次后进死信 |
| `default_priority` | 100 | 新任务默认优先级（越小越先） |

## 卸载

```bash
python skills/am-setup/scripts/pool.py setup --remove-hook   # 撤掉 hook
rm -rf .claude/skills/am-*                                   # npx 装的
rm -rf .claude/am .claude/rules/am-orchestration.md          # 账本与规则
```

插件形态则是 `claude plugin uninstall agentManager-Skill` 加
`claude plugin marketplace remove agentManager-Skill`。

## 开发

```bash
python -m unittest discover -s tests
```

66 个用例，含**真·多进程并发抢同一个文件**的仲裁测试 —— 那是这套东西的要害，
手工验证要开两个窗口卡时机，写成测试就是几行。

三条硬约束由测试强制，不靠注释：

1. `guard.py` 不准 import `liveness.py`（也不准 import `subprocess`）。
   热路径每次 Edit 都跑，而 `claude agents --json` 要 1 秒以上。
2. hook 入口内联的账本查找必须与 `config.ROOT_ANCHORS[0]` 一致。
3. 写进用户 `settings.local.json` 的 hook 必须幂等、不吞掉别人的配置、能干净撤掉。

### 为什么 Python 放在 `skills/am-setup/scripts/` 而不是仓库根

`npx skills add` 只复制 `skills/` 目录下的内容，仓库根的东西一概不下载。
代码放在 skill 里，`npx` 和 `claude plugin` 两条安装路径才都能拿到完整的一份。
四个 skill 共用这一份，另外三个用 `${CLAUDE_SKILL_DIR}/../am-setup/scripts/pool.py`
引用 —— 两种安装形态下它们都是兄弟目录，这个相对路径都成立。

## 数据

账本在 `<项目根>/.claude/am/pool.db`，不同项目天然隔离，`/am-setup` 会自动
写进 `.gitignore`。表结构见 `skills/am-setup/scripts/poolkit/db.py`，schema 版本走
`PRAGMA user_version`，迁移路径已留好。
