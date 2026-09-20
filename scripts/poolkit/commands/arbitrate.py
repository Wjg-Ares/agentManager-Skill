"""撞锁之后，先让程序判一次真假冲突。

撞锁本身已经由 hook 拦下了（`guard.check_edit`），这条命令跑在**那之后**，
不在热路径上 —— 实测一次 4–8 秒，换掉几轮跨会话对话，这笔账才算得过来。
（同样这几秒挂在每次 Edit 上就完全不成立，那正是它不能进 guard 的原因。）

原来的流程是：撞锁 → 拿到持有者会话名 → 发消息 → 两个 agent 用自然语言协商 →
谈不拢报主 agent → 还定不了问用户。一次撞锁要烧好几轮对话，而其中绝大多数
最后的结论只是「哦我们改的不是一块，你先」。

BRIEF §5 把这类判断列为「程序做不了、留给 agent」的唯一例外类型：需要理解
代码语义，但不需要生成任何东西。所以这里也**不该**用一个会写文章的模型 ——
要的就是一个带置信度的是非判断。

三条边界，实现时不要破：

1. **只出建议，不动锁。** 交接仍然必须由持有者执行 `handoff`。判错了顶多是
   给了个坏建议，不会有人凭这条命令把别人的锁拿走。
2. **判不了就退回人工协商，且退出码为 0。** 没配 key、断网、置信度不够，
   都走同一个出口。这条命令永远不该成为卡住人的那一环。
3. **两边的代价不对称，阈值也不对称。** 见 `_SURE_DIFFERENT`。
"""

from __future__ import annotations

import argparse

from .. import claims as claims_mod
from .. import config, db, jev, ledger, registry
from ..errors import UsageError
from ..models import utcnow
from ..render import Result, join, next_steps, section

NAME = "arbitrate"
HELP = "撞锁后判一次真假冲突（需要 TYPESAFE_API_KEY；判不了就退回人工协商）"

# 这两个数不是拍的，是拿 10 组真实调用量出来的（5 组同块 / 5 组不同块）：
#
#     同一块： 0.90  0.68  0.79  0.68  0.87
#     不同块： 0.08  0.08  0.09  0.12  0.17
#
# 两簇之间空着 0.17–0.68 一大段，阈值就放在这段里。重测的办法是照样造十来组
# 范围描述打一遍——这种数会随模型版本漂，写死在这儿而不是散在代码里就是为了好改。

#: 高于这个概率，认定两人改的是同一块 —— 真冲突，等着。
#: 贴着不同块那簇的上沿（0.17）留出大片余量；判不中也只是白等，代价小。
_SURE_SAME = 0.65

#: 低于这个概率，才敢认定是假冲突。
#:
#: 比另一侧严得多，因为**两种误判的代价差着一个量级**：
#: 误判成真冲突，最坏是白等一会儿，等审批通过锁自然释放，什么都没丢；
#: 误判成假冲突，会促成一次交接，两个人真的同时改同一个方法 —— 而这套东西
#: 存在的全部理由就是防这件事。宁可多等，不可乱交接。
#:
#: 所以这一侧刻意压在实测上沿（0.17）之下：宁可少判中一组，也不往同块那簇靠。
_SURE_DIFFERENT = 0.15

#: 问题本身。单一、具体、内行人扫一眼就能答 —— 这是 System One 能答好的形状。
#: 「谁应该先改」「这个交接安不安全」那种要权衡多个因素的，不在这里问。
_QUESTIONS = {
    "same_region": {
        "type": "noul",
        "instructions": (
            "两位开发者都要修改同一个文件，各自声明了打算改的范围。"
            "他们改的是不是同一块代码？"
        ),
        "criteria": {
            "true": "两人的范围指向同一个函数、方法或代码块，同时修改会互相覆盖。",
            "false": "两人的范围指向该文件中不同的、互不重叠的部分，可以各改各的。",
        },
    }
}


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("file", help="撞锁的那个文件")
    parser.add_argument(
        "--scope",
        help="你打算改哪个方法 / region —— 判断全靠它，不给就判不了",
    )


def run(ctx, args) -> Result:
    conn = ctx.connect()
    key = config.normalize_path(args.file)
    holder = claims_mod.active_for(conn, key)

    if holder is None:
        return Result(
            text=join(
                f"{args.file} 现在没有活跃声明，没什么可仲裁的。",
                next_steps([f"python pool.py declare {args.file}"]),
            ),
            data={"verdict": "no_holder", "path": args.file},
        )

    me = registry.by_session(conn, ctx.session_id) if ctx.session_id else None
    if me is None:
        raise UsageError(
            "本会话没注册角色，判不了「你」要改哪块",
            hint="先执行 /am-worker register worker-N",
        )
    if me.role == config.MAIN_ROLE:
        raise UsageError(
            "这是 worker 侧的命令，由被拦下的那个 worker 执行",
            hint=(
                "主 agent 要仲裁两个 worker 的争执，还是走原来的路：分别问清楚再拍板。"
                "这条命令只判「持有者 vs 我」这一种情形"
            ),
        )
    if holder.worker == me.role:
        return Result(
            text=f"{args.file} 的锁本来就在你（{me.role}）手上，直接改即可。",
            data={"verdict": "already_mine", "path": args.file},
        )

    address = registry.address_of(conn, holder.worker) or holder.worker

    # 缺范围就判不了 —— 这一步**程序自己就能判**，不必花一次调用去问模型。
    # 声明时 --scope 是选填的（claims.scope 允许为空），所以这条分支很常见。
    missing = _missing_scope(holder.scope, args.scope, me.role)
    if missing is not None:
        return _fallback(reason=missing, holder=holder, me=me, address=address)

    state = {
        "file": holder.display_path,
        "holder": {
            "developer": holder.worker,
            "task": _title(conn, holder.task_id),
            "wants_to_change": holder.scope,
        },
        "requester": {
            "developer": me.role,
            "task": _title(conn, _my_task_id(conn, me.role)),
            "wants_to_change": args.scope,
        },
    }

    try:
        answers = jev.ask(state, _QUESTIONS)
        same = jev.noul(answers, "same_region")
    except jev.JevUnavailable as exc:
        return _fallback(
            reason=(
                f"判不了：{exc}"
                if jev.available()
                else f"没有配置 {jev.ENV_KEY}，这条捷径没开"
            ),
            holder=holder,
            me=me,
            address=address,
        )

    _log(conn, me=me, holder=holder, same=same)

    if same >= _SURE_SAME:
        return _real_conflict(
            holder=holder, me=me, my_scope=args.scope, address=address, same=same
        )
    if same <= _SURE_DIFFERENT:
        return _false_conflict(
            holder=holder, me=me, my_scope=args.scope, address=address, same=same
        )
    return _fallback(
        reason=f"把握不够（同一块代码的概率 {same:.0%}，落在 {_SURE_DIFFERENT:.0%}–"
        f"{_SURE_SAME:.0%} 之间），不替你们拍板",
        holder=holder,
        me=me,
        address=address,
        same=same,
    )


# --------------------------------------------------------------------------
# 三个出口
# --------------------------------------------------------------------------


def _real_conflict(*, holder, me, my_scope: str, address: str, same: float) -> Result:
    return Result(
        text=join(
            f"判定：**真冲突**（同一块代码的概率 {same:.0%}）",
            section(
                f"{holder.worker} 正在改的和你要改的是同一块：",
                [
                    f"  它：{holder.scope}",
                    f"  你：{my_scope}",
                ],
            ),
            next_steps(
                [
                    f"等 {holder.worker} 交付并通过审批，锁会自动释放",
                    f"急的话给 `{address}` 发消息问它还要多久",
                    "手上这个任务先做别的文件，别在这儿等着",
                ]
            ),
        ),
        data={
            "verdict": "real_conflict",
            "same_region_probability": round(same, 4),
            "path": holder.display_path,
            "holder": holder.worker,
            "holder_address": address,
        },
    )


def _false_conflict(*, holder, me, my_scope: str, address: str, same: float) -> Result:
    return Result(
        text=join(
            f"判定：**假冲突**（同一块代码的概率只有 {same:.0%}）",
            section(
                "两边声明的范围不重叠：",
                [
                    f"  {holder.worker}：{holder.scope}",
                    f"  {me.role}：{my_scope}",
                ],
            ),
            section(
                f"下一步：把这条发给 `{address}`，**由它执行**（交接只能由持有者做）：",
                [
                    f"  python pool.py handoff {holder.display_path} "
                    f"--to {me.role} --scope \"{my_scope}\"",
                ],
            ),
            "这只是程序给的建议，不是结论 —— 对方觉得不对就按它说的来，它比这里多看得见代码。",
        ),
        data={
            "verdict": "false_conflict",
            "same_region_probability": round(same, 4),
            "path": holder.display_path,
            "holder": holder.worker,
            "holder_address": address,
            "suggested_command": (
                f"python pool.py handoff {holder.display_path} --to {me.role}"
            ),
        },
    )


def _fallback(*, reason: str, holder, me, address: str, same: float | None = None) -> Result:
    """判不了。原样退回撞锁时那套人工协商，退出码 0。"""
    return Result(
        text=join(
            f"没给出判定：{reason}。",
            section(
                f"照原来的来 —— 直接给 `{address}` 发消息，说明你要改哪个方法、为什么：",
                [
                    "  · 真冲突 → 等对方交付、审批通过后锁自动释放",
                    "  · 假冲突（改的是不同 region）→ 让对方交接锁：",
                    f"      python pool.py handoff {holder.display_path} --to {me.role}",
                    "  · 谈不拢 → 报主 agent 仲裁",
                ],
            ),
        ),
        data={
            "verdict": "undecided",
            "reason": reason,
            "same_region_probability": round(same, 4) if same is not None else None,
            "path": holder.display_path,
            "holder": holder.worker,
            "holder_address": address,
        },
    )


# --------------------------------------------------------------------------
# 辅助
# --------------------------------------------------------------------------


def _missing_scope(holder_scope: str | None, my_scope: str | None, role: str) -> str | None:
    """两边的范围齐不齐。返回 None 表示齐了。"""
    if not (holder_scope or "").strip() and not (my_scope or "").strip():
        return "两边都没说要改哪一块，没有可比的东西"
    if not (holder_scope or "").strip():
        return "持有者声明时没写 --scope，不知道它在改哪一块"
    if not (my_scope or "").strip():
        return f"你没给 --scope，不知道 {role} 要改哪一块"
    return None


def _title(conn, task_id: int | None) -> str | None:
    if task_id is None:
        return None
    task = ledger.get(conn, task_id)
    return task.title if task else None


def _my_task_id(conn, role: str) -> int | None:
    task = ledger.open_task_of(conn, role)
    return task.id if task else None


def _log(conn, *, me, holder, same: float) -> None:
    """记一条审计。写不进去不影响判定结果 —— 这条命令本来就只是给建议。"""
    try:
        with db.transaction(conn):
            db.log_event(
                conn,
                kind="arbitrate",
                now=utcnow(),
                actor=me.role,
                session_id=me.session_id,
                detail=f"{holder.display_path} vs {holder.worker}：同块概率 {same:.2f}",
            )
    except Exception:
        pass
