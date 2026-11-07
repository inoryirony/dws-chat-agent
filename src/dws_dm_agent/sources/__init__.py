"""InboundSource 抽象：外部世界变化 → 标准 SourceItem（蓝图步 7/8）。"""

from .base import InboundSource, SourceItem
from .external import GitLabSource, JiraSource

__all__ = ["GitLabSource", "InboundSource", "JiraSource", "SourceItem"]
