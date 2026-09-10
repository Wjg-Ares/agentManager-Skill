"""输出渲染：人类可读 + --json 双格式。

hook 和脚本消费走 json，agent 看走人类可读。

人类可读那一路有个额外职责：**把下一步该做什么直接写进输出**。
SKILL.md 每次都进上下文烧 token，命令输出只在执行那次烧 —— 能编进程序输出的
指引就不要写进 SKILL.md，这是整套设计省 token 的主要手段。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .models import Claim, Deliverable, Slot, Task, TaskStatus

#: 状态的中文标签，输出里不出现裸英文枚举
STATUS_LABEL = {
    TaskStatus.QUEUED: "排队中",
    TaskStatus.ASSIGNED: "进行中",
    TaskStatus.DELIVERED: "待审批",
    TaskStatus.REJECTED: "已打回",
    TaskStatus.APPROVED: "已通过",
    TaskStatus.FAILED: "已回收",
    TaskStatus.CANCELLED: "已取消",
}


@dataclass(slots=True)
class Result:
    """一条命令的执行结果。"""

    text: str
    data: dict[str, Any] = field(default_factory=dict)
    exit_code: int = 0


def task_line(task: Task, *, with_status: bool = True) -> str:
    bits = [f"#{task.id}", task.title]
    if with_status:
        bits.append(f"[{STATUS_LABEL[task.status]}]")
    if task.assignee:
        bits.append(f"→ {task.assignee}")
    if task.status is TaskStatus.QUEUED:
        bits.append(f"(优先级 {task.priority})")
    if task.dead_letter:
        bits.append("⚠死信")
    return " ".join(bits)


def task_dict(task: Task) -> dict[str, Any]:
    return {
        "id": task.id,
        "title": task.title,
        "status": str(task.status),
        "priority": task.priority,
        "assignee": task.assignee,
        "attempts": task.attempts,
        "dead_letter": task.dead_letter,
        "created_at": task.created_at,
        "assigned_at": task.assigned_at,
        "delivered_at": task.delivered_at,
        "approved_at": task.approved_at,
    }


def claim_dict(claim: Claim) -> dict[str, Any]:
    return {
        "id": claim.id,
        "task_id": claim.task_id,
        "worker": claim.worker,
        "path": claim.display_path,
        "scope": claim.scope,
        "claimed_at": claim.claimed_at,
    }


def slot_block(slot: Slot) -> list[str]:
    """一个 slot 的多行块。"""
    reg = slot.registration
    if reg is None:
        return [f"  {slot.role}：未注册（可开新窗口）"]

    head = f"  {slot.role}（{reg.session_name}）"
    if reg.status.value != "alive":
        return [f"{head}：已死亡，等待 reap 回收"]
    if slot.task is None:
        return [f"{head}：空闲"]

    lines = [f"{head}：{task_line(slot.task)}"]
    if slot.claims:
        names = ", ".join(c.display_path for c in slot.claims)
        lines.append(f"    已声明 {len(slot.claims)} 个文件：{names}")
    return lines


def slot_dict(slot: Slot) -> dict[str, Any]:
    reg = slot.registration
    return {
        "role": slot.role,
        "registered": reg is not None,
        "session_name": reg.session_name if reg else None,
        "session_id": reg.session_id if reg else None,
        "alive": reg.status.value == "alive" if reg else False,
        "free": slot.is_free,
        "task": task_dict(slot.task) if slot.task else None,
        "claims": [claim_dict(c) for c in slot.claims],
    }


def queue_block(tasks: list[Task], *, limit: int = 10) -> list[str]:
    if not tasks:
        return ["  （空）"]
    lines = [f"  {i}. {task_line(t, with_status=False)}" for i, t in enumerate(tasks[:limit], 1)]
    if len(tasks) > limit:
        lines.append(f"  … 另有 {len(tasks) - limit} 条")
    return lines


def deliverable_block(item: Deliverable) -> list[str]:
    return [
        f"交付 #{item.id}（任务 #{item.task_id} / {item.worker} / 第 {item.round} 轮）",
        *item.summary.render().splitlines(),
    ]


def deliverable_dict(item: Deliverable, *, with_content: bool = False) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": item.id,
        "task_id": item.task_id,
        "worker": item.worker,
        "round": item.round,
        "summary": {
            "changed": item.summary.changed,
            "impact": item.summary.impact,
            "risk": item.summary.risk,
            "confirm": item.summary.confirm,
            "files": item.summary.files,
        },
        "created_at": item.created_at,
    }
    if with_content:
        data["content"] = item.content
    return data


def next_steps(lines: list[str]) -> str:
    """统一的「下一步」段落。空列表返回空串。"""
    if not lines:
        return ""
    body = "\n".join(f"  {line}" for line in lines)
    return f"\n下一步：\n{body}"


def section(title: str, lines: list[str]) -> str:
    return "\n".join([title, *lines])


def join(*blocks: str) -> str:
    """拼接非空块，块间空一行。"""
    return "\n\n".join(b for b in blocks if b.strip())
