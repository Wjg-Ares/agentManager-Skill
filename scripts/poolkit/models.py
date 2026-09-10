"""领域模型。

跨层传递一律用这里的 dataclass，不传裸 dict —— 字段拼错在这里是 AttributeError，
在 dict 里是 None，后者要到三层之外才炸。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from sqlite3 import Row


def utcnow() -> str:
    """全库统一的时间口径：ISO8601 UTC 文本，秒级精度。

    SQLite 没有原生时间类型，口径不统一的话排序和比较都会出错。
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


class TaskStatus(StrEnum):
    """任务状态。文本枚举，不用魔法数字。"""

    QUEUED = "queued"  # 在队列里等 slot
    ASSIGNED = "assigned"  # 已派给 worker，开始标记已打
    DELIVERED = "delivered"  # worker 已交付，等用户审批
    REJECTED = "rejected"  # 用户不满意，打回给同一个 worker（slot 不放）
    APPROVED = "approved"  # 用户满意，结束标记已打，slot 已放
    FAILED = "failed"  # 超时/崩溃回收，可重派
    CANCELLED = "cancelled"  # 用户取消

    @property
    def is_open(self) -> bool:
        """是否仍占着 worker 的 slot。"""
        return self in (TaskStatus.ASSIGNED, TaskStatus.DELIVERED, TaskStatus.REJECTED)

    @property
    def is_terminal(self) -> bool:
        return self in (TaskStatus.APPROVED, TaskStatus.CANCELLED)


class WorkerStatus(StrEnum):
    ALIVE = "alive"
    DEAD = "dead"


@dataclass(slots=True)
class Registration:
    """一个角色在注册表里的当前地址。

    会话名重启即变，稳定的是 role；这张表就是 role → 当前地址 的翻译层。
    """

    role: str
    session_id: str
    session_name: str
    pid: int | None
    cwd: str | None
    status: WorkerStatus
    registered_at: str
    last_seen_at: str

    @classmethod
    def from_row(cls, row: Row) -> "Registration":
        return cls(
            role=row["role"],
            session_id=row["session_id"],
            session_name=row["session_name"],
            pid=row["pid"],
            cwd=row["cwd"],
            status=WorkerStatus(row["status"]),
            registered_at=row["registered_at"],
            last_seen_at=row["last_seen_at"],
        )

    @property
    def is_worker(self) -> bool:
        return self.role.startswith("worker-")


@dataclass(slots=True)
class Task:
    id: int
    title: str
    detail: str | None
    status: TaskStatus
    priority: int
    assignee: str | None
    attempts: int
    dead_letter: bool
    created_at: str
    assigned_at: str | None  # 开始标记
    delivered_at: str | None
    approved_at: str | None  # 结束标记
    cancelled_at: str | None

    @classmethod
    def from_row(cls, row: Row) -> "Task":
        return cls(
            id=row["id"],
            title=row["title"],
            detail=row["detail"],
            status=TaskStatus(row["status"]),
            priority=row["priority"],
            assignee=row["assignee"],
            attempts=row["attempts"],
            dead_letter=bool(row["dead_letter"]),
            created_at=row["created_at"],
            assigned_at=row["assigned_at"],
            delivered_at=row["delivered_at"],
            approved_at=row["approved_at"],
            cancelled_at=row["cancelled_at"],
        )


@dataclass(slots=True)
class Claim:
    """一条文件声明，同时就是一把锁。"""

    id: int
    task_id: int
    worker: str
    file_path: str  # 规范化后的绝对路径，锁的 key
    display_path: str  # worker 声明时的原样，给人看
    scope: str | None  # 选填：方法/region，只给协商看，不参与锁判定
    claimed_at: str
    released_at: str | None
    released_by: str | None

    @classmethod
    def from_row(cls, row: Row) -> "Claim":
        return cls(
            id=row["id"],
            task_id=row["task_id"],
            worker=row["worker"],
            file_path=row["file_path"],
            display_path=row["display_path"],
            scope=row["scope"],
            claimed_at=row["claimed_at"],
            released_at=row["released_at"],
            released_by=row["released_by"],
        )

    @property
    def is_active(self) -> bool:
        return self.released_at is None


@dataclass(slots=True)
class Summary:
    """交付摘要。五行，格式由程序保证，缺项直接打回。"""

    changed: str  # 改了什么
    impact: str  # 影响面
    risk: str  # 风险点
    confirm: str  # 待你确认
    files: str  # 本次声明的文件

    #: 顺序即输出顺序
    LABELS = (
        ("changed", "改了什么"),
        ("impact", "影响面"),
        ("risk", "风险点"),
        ("confirm", "待你确认"),
        ("files", "涉及文件"),
    )

    def render(self) -> str:
        lines = [f"{label}：{getattr(self, attr)}" for attr, label in self.LABELS]
        lines.append("是否已编译验证：否（构建权在主 agent）")
        return "\n".join(lines)


@dataclass(slots=True)
class Deliverable:
    id: int
    task_id: int
    worker: str
    round: int
    summary: Summary
    content: str
    created_at: str

    @classmethod
    def from_row(cls, row: Row) -> "Deliverable":
        return cls(
            id=row["id"],
            task_id=row["task_id"],
            worker=row["worker"],
            round=row["round"],
            summary=Summary(
                changed=row["s_changed"],
                impact=row["s_impact"],
                risk=row["s_risk"],
                confirm=row["s_confirm"],
                files=row["s_files"],
            ),
            content=row["content"],
            created_at=row["created_at"],
        )


@dataclass(slots=True)
class Slot:
    """一个 worker 槽位的聚合视图，`status` 命令的主要输出单元。"""

    role: str
    registration: Registration | None
    task: Task | None
    claims: list[Claim] = field(default_factory=list)

    @property
    def is_free(self) -> bool:
        """空闲判定：注册在册、活着、且没有仍占 slot 的任务。

        依据是 §4.1 的双标记 —— 最后一条记录是已审批的结束标记才算空闲，
        而不是看某个瞬时的 busy/idle 快照。
        """
        if self.registration is None or self.registration.status is not WorkerStatus.ALIVE:
            return False
        return self.task is None
