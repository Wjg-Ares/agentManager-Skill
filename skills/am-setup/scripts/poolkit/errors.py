"""统一异常体系。

每个异常自带退出码与可选的下一步提示，由 cli 层统一转成输出，
业务代码里不写 try/except print。
"""

from __future__ import annotations


class AmError(Exception):
    """所有 am 异常的基类。"""

    exit_code: int = 1
    #: 给人看的错误类别
    kind: str = "error"

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": False,
            "error": self.kind,
            "message": self.message,
            "hint": self.hint,
        }


class UsageError(AmError):
    """参数用错了。"""

    exit_code = 2
    kind = "usage"


class NotSetUp(AmError):
    """当前项目还没跑过 setup。"""

    exit_code = 3
    kind = "not_setup"

    def __init__(self, message: str = "当前项目还没初始化 am 账本") -> None:
        super().__init__(message, hint="先在主 agent 会话里执行 /am-setup")


class NotFound(AmError):
    """任务 / 角色 / 交付记录不存在。"""

    exit_code = 4
    kind = "not_found"


class Conflict(AmError):
    """声明撞车、slot 占满一类的并发冲突。"""

    exit_code = 5
    kind = "conflict"


class InvalidState(AmError):
    """状态流转非法，例如对未交付的任务执行 approve。"""

    exit_code = 6
    kind = "invalid_state"


class NotRegistered(AmError):
    """本会话没在注册表里，无法判定角色。"""

    exit_code = 7
    kind = "not_registered"

    def __init__(self, message: str = "本会话尚未注册角色") -> None:
        super().__init__(message, hint="worker 会话先执行 /am-worker register worker-N")
