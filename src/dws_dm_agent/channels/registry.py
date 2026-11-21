"""Channel 注册表：conversation_id 前缀 → 投递实现工厂。

照 runners/adapters/registry.py 的单点打法。工厂签名是 (dws) -> Channel：
空前缀 / `dm:` / `panel:` 用恒等工厂显式返回 dws（默认通道不再是隐式
fallback），其余前缀各自构造实现。未登记的非空前缀由 channel_for 抛错，
不静默落到钉钉。
"""

from __future__ import annotations

from typing import Any, Callable

from .base import Channel
from .external import GitLabChannel, JiraChannel

CHANNEL_PREFIXES: dict[str, Callable[[Any], Channel]] = {
    "": lambda dws: dws,
    "dm:": lambda dws: dws,
    "panel:": lambda dws: dws,
    "jira:": lambda dws: JiraChannel(),
    "gitlab:": lambda dws: GitLabChannel(),
}

__all__ = ["CHANNEL_PREFIXES"]
