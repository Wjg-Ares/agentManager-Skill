# agentManager-Skill

多 Agent 协作编排 Claude Code 技能集。一个主 agent 带最多 3 个 worker，
**在同一个工作目录里**并行干活。

零依赖：只用 Python 标准库，账本落 SQLite（`sqlite3` 是 Python 自带的，
不需要安装任何数据库）。

## 安装

```bash
claude plugin marketplace add Wjg-Ares/agentManager-Skill
claude plugin install agentManager-Skill
```

**装完要重开一个 Claude Code 窗口**，当前会话不会热加载。

> **不要用 `npx skills add` 装这个插件。** 那个安装器只复制 `skills/` 目录下的
> SKILL.md，而本插件四个 skill 共用的 Python 代码在仓库根的 `scripts/`、
> 强制层在 `hooks/` —— 两者都不会被下载，装出来的四个命令是死的。
> 它也不认识 Claude Code 的 `hooks.json`（那是个跨 agent 的通用安装器），
> 所以并发写拦截这套**命脉功能无论如何都装不上**。

装完后文件在 `~/.claude/plugins/cache/agentManager-Skill/agentManager-Skill/<版本>/`。

## 首次使用

在主 agent 会话里：

```
/am-setup
```

它会建库、把规则装进 `.claude/rules/`、把本会话注册成主 agent，
并告诉你接下来该做什么。**拦截用的 hook 随插件自带，不会改你的 `settings.json`。**

然后在同一个工作目录另开 1~3 个 Claude Code 窗口，每个里面执行：

```
/am-worker register worker-1     # 2、3 同理
```

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

四个技能底下是同一个 CLI，也可以直接用：

```bash
python scripts/pool.py status
python scripts/pool.py --json check-edit src/Foo.cs
```

| 命令 | 用途 |
|---|---|
| `setup` | 建库、装规则、注册主 agent（`--reset` 清空账本重来） |
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

加命令不用改核心：往 `scripts/poolkit/commands/` 放一个模块就自动注册。

## 配置

配置存在库里，跟着项目走，不动你的 `settings.json`：

```bash
python scripts/pool.py config                          # 看全部
python scripts/pool.py config set deliver_timeout_min 45
```

| 键 | 默认 | 含义 |
|---|---|---|
| `max_workers` | 3 | 并发 worker 上限 |
| `deliver_timeout_min` | 30 | 派活后多久未交付算卡死 |
| `max_attempts` | 2 | 重派几次后进死信 |
| `default_priority` | 100 | 新任务默认优先级（越小越先） |
| `scratch_dir` | 空 | 临时文件的去处。设了它，**任何角色**往系统 Temp 写文件都会被 hook 拦下并提示改道；留空则不启用 |

`scratch_dir` 也可以在初始化时一并设好：

```bash
/am-setup --scratch-dir D:\claude-tmp
```

这条拦的是**规则**不是决策：系统 Temp 会被清理工具随时清掉，而且 Windows 的
8.3 短名路径（`C:\Users\ADMINI~1\...`）会触发 Claude Code 的可疑路径检查，
每写一次就要人工点一次同意。程序一毫秒判掉的事，不该惊动人。

## 开发

```bash
python -m unittest discover -s tests
```

80 个用例，含**真·多进程并发抢同一个文件**的仲裁测试 —— 那是这套东西的要害，
手工验证要开两个窗口卡时机，写成测试就是几行。

两条硬约束由测试强制，不靠注释：

1. `guard.py` 不准 import `liveness.py`（也不准 import `subprocess`）。
   热路径每次 Edit 都跑，而 `claude agents --json` 要 1 秒以上。
2. hook 入口内联的账本查找必须与 `config.ROOT_ANCHORS[0]` 一致。

## 数据

账本在 `<项目根>/.claude/am/pool.db`，不同项目天然隔离，`/am-setup` 会自动
写进 `.gitignore`。表结构见 `scripts/poolkit/db.py`，schema 版本走
`PRAGMA user_version`，迁移路径已留好。
