# 交接说明 — agentManager-Skill

> 给接手这个仓库的 Claude 会话看的。读完先照 §1 做，**不要直接动手写代码**。

## 1. 第一件事：先提问，别开工

**用户（wangjiahui）明确要求：你要先向他提问，把需求问清楚，他还有很多要补充的东西。**

不要读完这份文档就开始建目录、写代码。先把 §8 那些问题问他（想到别的也一并问），
等他把话说完、需求收敛了，再动手。

## 2. 要做的事

把 `docs/multi-agent-orchestration.md` 里那套**多 Agent 协作编排**方案，
做成一个**可通过 npm 安装的 Claude Code skill 插件**。

那份设计文档是用户和另一个会话讨论很久的产物，**已经相当收敛，不要推翻重来**。
里面每条决策都写了原因，§0 有 v1→v2 的变更对照，§11 列了未决点。**先完整读一遍。**

设计文档已同步到 **v3**（存储改 SQLite、交付传 ID、队列绑定审批、SKILL.md 从薄），
与本文一致，不存在矛盾描述。§0 有完整演进记录。

## 3. 参照物

`D:\workspace\sql-skill` —— 同一个用户写的另一个 skill 插件，结构直接照抄：

```
.claude-plugin/marketplace.json     插件清单（name / owner / metadata / plugins[].skills）
skills/<技能名>/SKILL.md            每个技能一个目录
skills/<技能名>/scripts/*.py        辅助脚本
README.md                           安装说明
```

安装方式 `npx skills add <owner>/<repo>`，owner 用 `Wjg-Ares`。
`/sql setup` 那个首次配置命令的做法值得参考（见 §9）。

## 4. 背景（一句话）

用户会同时开一个主 agent + 最多 3 个 worker agent（都是**手动开的交互式 Claude Code 会话**，
**共用同一个工作目录**）。他只跟主 agent 提需求，主 agent 派活、汇总、仲裁；
worker 干活、交付摘要；他逐个审批。

难点在共用工作目录下的并发写——两个 worker 改同一个文件会**直接覆盖丢代码**，
所以需要文件声明 + `PreToolUse` hook 拦截 + 撞车后 worker 之间自行协商。细节在设计文档里。

## 5. 已经定下来的技术决策（别再讨论了）

- **语言 Python**（用户机器 3.11.9，`D:\Python311`）
- **零依赖**：只用标准库。**存储用 SQLite，靠 Python 自带的 `sqlite3` 模块，
  用户无需安装任何数据库**（已实测：引擎版本 3.45.1）
- **必须模块化**，不接受单文件：

```
pool.py                 薄入口：sys.path 处理 + 调 cli.main()
poolkit/
  config.py             路径常量、超时阈值、禁用命令清单
  models.py             dataclass：Worker / Task / Claim / Deliverable（不传裸 dict）
  db.py                 连接管理、schema 初始化、user_version 迁移
  registry.py           角色注册：worker-1 ↔ 会话名 ↔ session_id
  ledger.py             任务与队列：CRUD、状态流转、优先级
  claims.py             文件声明与仲裁
  guard.py              hook 判定：check_edit / check_bash
  liveness.py           存活探针（慢路径）
  commands/             每个子命令一个模块，只做编排，不写业务
```

- **两条硬约束，实现时不能破**：

  1. **`guard.py` 不准 import `liveness.py`**。热路径（每次 Edit/Write 都跑的 `check-edit`）
     只准查本地 SQLite；`claude agents --json` 要 1 秒以上，只有 `reap` 命令能调。
     这条用模块依赖强制，不靠注释。
  2. **连接必须设三个 PRAGMA**：`journal_mode=WAL`（多进程并发读写的前提）、
     `busy_timeout`（否则并发直接抛 database is locked）、
     `foreign_keys=ON`（**SQLite 默认是关的**，不显式打开外键约束形同虚设）。

- **SKILL.md 必须薄，逻辑一律下沉到 Python**（用户明确要求，理由是省 token）：

  SKILL.md 的内容**每次都进上下文烧 token**，程序执行不烧、只有输出烧。所以：

  - SKILL.md 只写"**什么场景调哪个命令**"，不写业务逻辑、不写分支判断、不抄设计文档
  - 能编码进程序的判断一律编码进去，**让命令输出直接给出下一步该做什么**
    （例：`am approve` 执行完直接把队列待办和「1 执行 / 2 删除」的选项一起输出）
  - 唯一例外：需要理解代码语义的判断（如"这俩 worker 的冲突是真是假"），程序做不了，留给 agent

- **工程质量要求**（用户原话"python 框架一定要强大"，具体落到这几条）：

  1. **命令自动注册**：新增一个 `commands/xxx.py` 就自动可用，不要在核心里写 if-else 分发
  2. **双输出格式**：人类可读 + `--json`，hook 和脚本消费走 json，agent 看走人类可读
  3. **统一异常体系**：自定义异常基类，统一转成退出码与错误消息，不要满地 try/except print
  4. **完整类型注解**，`models.py` 用 dataclass，不传裸 dict
  5. **幂等**：`setup` 可重复执行不产生重复配置；`claim` / `declare` 重复调用安全
  6. **所有写操作走事务**，别裸执行多条 SQL
  7. **可测试**：领域逻辑（registry / ledger / claims）不依赖 CLI 层，能直接单测。
     最值得测的是 claims 的并发仲裁——手工验证两个 worker 抢同一文件很痛苦，写成测试就是几行

## 6. 用户明确要求的功能点

这些是用户直接提的，不是推导出来的，务必覆盖：

0. **命令命名规范（硬性）**：本 skill 对外暴露的**所有 slash 命令一律用 `/am-***` 格式**，
   例如 `/am-setup`、`/am-status`、`/am-approve`。不许出现不带 `am-` 前缀的命令。

   > 注意区分两个层面：**slash 命令**（用户和 agent 在会话里敲的）必须是 `/am-xxx`；
   > **Python CLI 子命令**（脚本内部、hook 调用的）是另一回事，如
   > `python pool.py check-edit`，不受此规范约束。设计文档 §10.2 那张动词表说的是后者。

1. **`/am-setup` 首次初始化命令**。第一次使用必须先跑它：依托表结构建库、
   落地 hook 配置、注册主 agent。（此名由用户定，其余命令的后半截待确认）
2. **队列进数据库**。3 个 worker 全忙时，新任务入库排队，不靠内存也不靠 JSON。
3. **审批时联动待办（重点）**：
   **每次用户对任一 agent 说"满意"时，主 agent 都要顺带查一次队列。**
   有待办就给用户选项：**1 执行　2 删除这条待办**。

   > 这条设计的意义：把队列推进绑定到"用户说满意"这个他本来就在场的时刻，
   > 不需要额外的轮询、定时器或提醒机制。实现时不要另起一套通知。
4. **优先级可调整**：要有对应的 py 命令，能改队列中任务的优先级。
5. **交付只传摘要 + 数据 ID**：worker 交付给主 agent 的消息里只带
   **四行摘要 + deliverable 的 ID**，完整内容留在库里，**主 agent 需要时按 ID 去查**。
   这是为了防止主 agent 上下文被多路完整产出撑爆（设计文档 §5.1 的落地方式）。
6. **表结构必须合理且耐用**——用户原话，见 §7。

## 7. 表结构要求

用户特别强调"**一定要合理且耐用**"。下面是必须覆盖的语义和建议草案，
**字段可以增补、命名可以改，但这些语义不能少**；定稿前把你的 schema 给用户过目。

**必须有的表**

| 表 | 用途 | 关键点 |
|---|---|---|
| `registry` | 角色 ↔ 会话名 ↔ session_id | 会话名重启即变，靠这层把稳定角色名映射到当前地址 |
| `tasks` | 任务 + 队列（同一张表） | `status='queued'` 即在队列中，不要另建队列表 |
| `deliverables` | 交付记录 | `summary` 与 `content` 分列，消息只传 id + summary |
| `claims` | 文件声明（即锁） | 见下方"仲裁交给数据库" |
| `events` | 操作审计 | 谁在何时做了什么，排查问题和事后追溯全靠它 |

**仲裁交给数据库，不要在应用层扫描比时间戳**

`claims` 上建**部分唯一索引**：

```sql
CREATE UNIQUE INDEX ux_claims_active ON claims(file_path) WHERE released_at IS NULL;
```

这样"同一文件同时只能有一个活跃声明"由数据库保证，INSERT 冲突即代表已被占用。
比应用层"扫描所有声明再比时间戳"可靠得多，也天然没有竞态。
（SQLite 支持部分索引，可放心用。）

**"耐用"的具体标准**

1. 状态一律用**文本枚举**（`queued` / `assigned` / `delivered` / `approved` / `rejected` / `failed` / `cancelled`），
   不用魔法数字——三个月后没人记得 `status=3` 是什么
2. 时间统一 **ISO8601 UTC 文本**（SQLite 无原生时间类型），全表口径一致
3. 每张表都有 `created_at`
4. **软删除**（`released_at` / `cancelled_at`）而非物理删除，保留可追溯性
5. **schema 版本用 `PRAGMA user_version`**，配套迁移函数——加字段是必然会发生的事，
   第一版就要把迁移路径留出来
6. 外键该建就建，且记得 `PRAGMA foreign_keys=ON`
7. 别把结构化数据塞进 JSON 字段图省事；确实无结构的（如自由文本产出）才用 TEXT

## 8. 建议问用户的问题

**关于 skill 形态**

1. 拆成几个技能？主 agent 一个（派活/仲裁/审批）、worker 一个（声明/交付/协商）合理吗？
2. 前缀已定死为 `/am-`（见 §6 第 0 条），但后半截叫什么要问：
   `/am-status`？`/am-dispatch`？`/am-approve`？把你打算暴露的命令清单列给用户确认
3. 只他自己用，还是要发布给别人用？影响 README 和默认配置写法

**关于安装与数据位置**

4. 数据库文件放哪？必须在**用户的项目目录**下（不同项目的账本要隔离），
   不能放插件安装目录。项目根怎么定位？
5. `/am-setup` 除了建库，要不要一并把 hook 写进 `settings.json`？（见 §9）

**关于设计里还没定的**

6. 声明的粒度：只记文件路径，还是带上"要改哪个方法/region"？
   （上一轮倾向**文件必填、方法选填**，未最终拍板）
7. schema 定稿（§7 是草案，必须让用户过目）
8. 交付摘要的四行格式够不够用（设计文档 §5.2）
9. 完整产出存进 `deliverables.content`，还是存文件、库里只记路径？
10. 队列里"1 执行 / 2 删除"之外要不要第三个选项（比如"改优先级后重排"）？

## 9. 已知的技术难点

**hook 怎么随 skill 一起装上。** skill 本身是一堆 md 和脚本，
但这套方案的强制力全靠 `PreToolUse` hook（拦并发写、拦 worker 执行构建和 git 写操作）。
hook 必须写进 `settings.json`，skill 装进去不会自动生效。

`/am-setup` 是解决它的天然位置——建库的同时把 hook 配置写进项目的
`.claude/settings.json`。但要考虑：重复执行不能写重复项、用户已有 hook 配置不能覆盖、
卸载时怎么清理。**方案跟用户确认，别自己拍。**

## 10. 上下文补充

- 用户的实际项目在 `D:\workspace\xinghe\CDC\cdc`（.NET 6 + SqlSugar 的 LIS 系统），
  这套编排工具就是给那个项目用的，所以设计里对 `dotnet build` 并发写坏 `bin/obj` 有专门处理
- 用户是后端开发，**中文沟通**
- **临时文件写 `D:\claude-tmp\`，不要往 C 盘写**
- 用户不喜欢长篇分析和自作主张。**发现问题先说，不要先写**
