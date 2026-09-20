"""Jev 客户端 —— 全仓库唯一一处调外部模型的地方。

**这个模块是可选增强，不是依赖。** 整套编排在没有它的情况下必须照常工作：
没配 key、断网、对方挂了，一律抛 :class:`JevUnavailable`，调用方降级回原来的
人工流程。任何一条正确性路径都不准建立在「Jev 答得出来」之上。

为什么只有 `arbitrate` 一个调用方：

- **guard 热路径绝对不准碰这里。** `guard.py` 开头那个方框写着「无子进程、无网络」，
  理由是 `claude agents --json` 要 1 秒以上，不能挂在每次 Edit/Write 上。
  Jev 是同一个数量级的等待，从网络那边引回来和从子进程引回来没有区别。
  `check_bash` 的白名单同理 —— 那是安全边界，断网时既不能失败开放也不能失败关闭。
- 剩下能用的，只有「程序算不出、又不需要生成文本」的判断，而这类判断在本仓库
  只有一处：两个 worker 撞在同一个文件上时，这冲突是真是假（见 BRIEF §5 末尾）。

只用标准库 —— `urllib` 而不是 requests，零依赖这条不破。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Final

ENDPOINT: Final = "https://api.typesafe.ai/v1/systemone"
MODEL: Final = "jev-latest"

#: 装了插件之后 key 就放在环境变量里，本仓库不落盘、不进账本、不进日志
ENV_KEY: Final = "TYPESAFE_API_KEY"

#: 超时按**握手**最坏情况给，不是按模型速度给。
#: 实测：模型本身 0.4–1.0 秒，而这台机器到 api.typesafe.ai 的 TLS 握手要 5–10 秒。
#: 给小了会变成「网络稍微抖一下就永远判不出来」，那还不如不装。
TIMEOUT_SEC: Final = 20


class JevUnavailable(Exception):
    """判不了。

    **故意不继承 AmError** —— 它不是用户的错误，不该走 cli 的错误出口变成非零退出码。
    调用方接住它、退回原来的人工流程，这是唯一正确的处理方式。
    """


def available() -> bool:
    """有没有配 key。用来在输出里区分「判不了」和「没装」两种情况。"""
    return bool(os.environ.get(ENV_KEY))


def ask(
    state: Any, questions: dict[str, Any], *, timeout: int = TIMEOUT_SEC
) -> dict[str, Any]:
    """问一组 typed 问题，返回 ``answers``。

    问题之间是并行的，多问几个几乎不加时间 —— 但每个问题都必须是单一、具体、
    「内行人扫一眼就能答」的那种。需要权衡多个独立因素的，拆开问再在代码里合成。
    """
    key = os.environ.get(ENV_KEY)
    if not key:
        raise JevUnavailable(f"没有配置 {ENV_KEY}")

    payload = json.dumps(
        {"model": MODEL, "state": state, "questions": questions},
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        ENDPOINT,
        data=payload,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.load(response)
    except urllib.error.HTTPError as exc:
        # 只带状态码，不带响应体 —— 响应体可能回显请求内容，那里面是用户的代码
        raise JevUnavailable(f"HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise JevUnavailable(f"连不上：{exc}") from exc
    except ValueError as exc:  # json.JSONDecodeError
        raise JevUnavailable("返回的不是 JSON") from exc

    answers = body.get("answers") if isinstance(body, dict) else None
    if not isinstance(answers, dict):
        raise JevUnavailable("返回里没有 answers")
    return answers


def noul(answers: dict[str, Any], name: str) -> float:
    """取一个是非题的概率（0–1）。结构对不上就当判不了，不猜。"""
    item = answers.get(name)
    if not isinstance(item, dict):
        raise JevUnavailable(f"返回里没有 {name}")
    value = item.get("noul")
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise JevUnavailable(f"{name} 没有 noul 值")
    return float(value)
