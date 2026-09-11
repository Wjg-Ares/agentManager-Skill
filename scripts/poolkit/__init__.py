"""多 Agent 协作编排账本。

分层（依赖只能自上而下）::

    commands/    编排，不写业务
    ─────────────────────────────
    guard        hook 判定（热路径，不准 import liveness）
    liveness     存活探针（慢路径，可以 import 一切）
    ledger       任务与队列
    claims       文件声明与仲裁
    registry     角色注册
    ─────────────────────────────
    db  models  render  errors  config
"""

__all__ = [
    "claims",
    "cli",
    "config",
    "db",
    "errors",
    "guard",
    "ledger",
    "liveness",
    "models",
    "registry",
    "render",
]
