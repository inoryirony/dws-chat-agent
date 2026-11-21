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
    processing_reactions: bool
    supports_freshness: bool
    retry_safe: bool
    supports_reconciliation: bool

    async def send(
        self, contact: Contact, content: str, send_uuid: str
    ) -> tuple[str | None, Any]:
        """发一条内容；send_uuid 是幂等键（outbox 重试依赖它去重）。"""
        ...

    async def history(self, contact: Contact) -> list[HistoryMessage]:
        """会话历史；manual 信号检测与补充消息检查的数据源。"""
        ...

    async def find_receipt(self, contact: Contact, send_uuid: str) -> str | None:
        """Return the remote id for a previously accepted logical action."""
        ...


def channel_for(conversation_id: str, default: Channel) -> Channel:
    """按 conversation_id 前缀选投递通道。

    `jira:` / `gitlab:` 前缀映射到对应实现，空前缀 / `dm:` / `panel:`
    走恒等工厂返回 default。未登记的非空前缀直接抛错——静默落到默认
    通道会把外部系统的消息投给钉钉。registry 惰性 import，避免环。
    """
    from .registry import CHANNEL_PREFIXES

    prefix = conversation_id.split(":", 1)[0] + ":" if ":" in conversation_id else ""
    factory = CHANNEL_PREFIXES.get(prefix)
    if factory is None:
        raise ValueError(f"unregistered channel prefix: {prefix}")
    return factory(default)


__all__ = ["Channel", "channel_for"]
