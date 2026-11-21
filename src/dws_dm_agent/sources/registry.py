"""Source 注册表：source 名 → 源实现类。新源 = 实现类 + 这里一行。"""

from __future__ import annotations

from .external import GitLabSource, JiraSource

SOURCE_TYPES: dict[str, type] = {
    "jira": JiraSource,
    "gitlab": GitLabSource,
}

__all__ = ["SOURCE_TYPES"]
