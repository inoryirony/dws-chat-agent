"""Channel：投递出口的协议面（蓝图步 6，双向化的"出"半边）。

一个 Channel 回答两个问题：结果往哪发（send）、怎么知道人类先开口了
（manual 信号）。dws 是第一实现（钉钉 DM 全链：卡片/表情/文件）；
jira/gitlab 把终稿发成 issue/MR 评论。

能力声明照 runners/adapters/base.py 的打法：service 层按能力位降级，
不猜方法是否存在。outbox 的投递目标按 conversation_id 前缀查注册表
（`jira:PROJ-123` → jira channel），无前缀默认 dws。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from ..core import Contact, HistoryMessage


@runtime_checkable
class Channel(Protocol):
    name: str
    # 能力位：False 时 service 层跳过对应分支（卡片/表情/文件/新鲜度）。
    streaming_cards: bool
    supports_freshness: bool

    async def send(
        self, contact: Contact, content: str, send_uuid: str
    ) -> tuple[str | None, Any]:
        """发一条内容；send_uuid 是幂等键（outbox 重试依赖它去重）。"""
        ...

    async def history(self, contact: Contact) -> list[HistoryMessage]:
        """会话历史；manual 信号检测与补充消息检查的数据源。"""
        ...


def channel_for(conversation_id: str, default: Channel) -> Channel:
    """按 conversation_id 前缀选投递通道。

    `jira:` / `gitlab:` 前缀映射到对应实现；由 service 在构造时把注册表
    传进来（这里只做名字解析，避免 import 环）。
    """
    from .registry import CHANNEL_PREFIXES

    prefix = conversation_id.split(":", 1)[0] + ":" if ":" in conversation_id else ""
    channel = CHANNEL_PREFIXES.get(prefix)
    return channel if channel is not None else default


__all__ = ["Channel", "channel_for"]
