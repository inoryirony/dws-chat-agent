"""channels 包：投递出口协议（蓝图步 6）。"""

from .base import Channel, channel_for
from .external import GitLabChannel, JiraChannel
from .registry import CHANNEL_PREFIXES

__all__ = [
    "CHANNEL_PREFIXES",
    "Channel",
    "GitLabChannel",
    "JiraChannel",
    "channel_for",
]
