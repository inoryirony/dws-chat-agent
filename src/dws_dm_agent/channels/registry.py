"""Channel 注册表：conversation_id 前缀 → 投递实现。

照 runners/adapters/registry.py 的单点打法。dws 不在注册表里——它是
默认通道（无前缀即 dws），注册表只收增量通道。
"""

from __future__ import annotations

from .external import GitLabChannel, JiraChannel

CHANNEL_PREFIXES = {
    "jira:": JiraChannel(),
    "gitlab:": GitLabChannel(),
}

__all__ = ["CHANNEL_PREFIXES"]
