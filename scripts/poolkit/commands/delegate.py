"""建任务 + 派活，一步到位。

为什么要有这条「只是把 add 和 dispatch 拼起来」的命令：

主 agent 自己动手做业务活，是这套编排最主要的失效方式 —— 一旦它自己上，
就没有文件锁、没有交付记录、没有审批，账本上什么都查不到。规则第 0 条点名
禁止这件事，但那是纯约定，靠自觉。

实际发生过一次，事后主 agent 自己给的理由是：

    「派出去还要建任务、写交付，我直接查一下更快」

**这是摩擦力问题，不是态度问题。** 正确的路比错误的路贵，它就会走错的那条。
所以把两条命令压成一条：派活的成本降到和自己动手差不多，那句「更快」就不成立了。

堵后路的部分在 hook 里（guard.check_edit 拒绝主 agent 改业务文件），
那才是强制。这条命令负责的是另一半：让正确的做法顺手。
"""

from __future__ import annotations

import argparse

from .. import ledger
from ..errors import UsageError
from ..render import Result

NAME = "delegate"
HELP = "建任务并立刻派出去（= add + dispatch，主 agent 的默认动作）"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("title", help="一句话任务标题")
    parser.add_argument("--detail", help="完整需求描述，派活时原样转给 worker")
    parser.add_argument("--priority", type=int, help="越小越先出队，缺省 100")
    parser.add_argument(
        "--to", help="指定 worker，如 worker-1；缺省自动挑一个空闲的"
    )


def run(ctx, args) -> Result:
    from . import add as add_cmd
    from . import dispatch as dispatch_cmd

    conn = ctx.connect()

    # 先确认真派得出去，再建任务 —— 否则队列里会堆一串「建了但没派成」的孤儿，
    # 而主 agent 看到命令失败，多半就自己动手了，正是要防的事。
    if args.to is None and not [s for s in ledger.slots(conn) if s.is_free]:
        registered = [s for s in ledger.slots(conn) if s.registration is not None]
        if not registered:
            raise UsageError(
                "还没有任何 worker 上线，派不出去",
                hint=(
                    "**不是 slot 占满了，是一个都没注册。**\n"
                    "  让用户新开一个窗口，在里面执行： /am-worker register worker-1\n"
                    "  在那之前不要自己动手做这个任务 —— 你自己干就没有文件锁、"
                    "没有交付记录、没有审批，账本上查不到"
                ),
            )
        raise UsageError(
            "slot 全占着，现在派不出去",
            hint=(
                "用 python pool.py add \"标题\" 先入队，用户对某个交付说满意后"
                "slot 会腾出来，那时再 dispatch"
            ),
        )

    created = add_cmd.run(ctx, args)
    task_id = created.data["task"]["id"]

    dispatch_args = argparse.Namespace(task_id=task_id, to=args.to)
    result = dispatch_cmd.run(ctx, dispatch_args)
    result.data["created_by"] = NAME
    return result
