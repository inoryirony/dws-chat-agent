"""Watch 规则层：常驻条件 → 命中即建 task + 开处理轮（蓝图步 7/9）。

config 形态（service 层读 raw["watches"]）：

    "watches": [
      {
        "source": "jira",
        "name": "新 issue 指派给我",
        "jql": "assignee = currentUser() AND created >= -1d ORDER BY created DESC",
        "projects": ["ZKAI"],
        "instruction": "分析这个 issue，给出处理建议并回写评论。"
      },
      {
        "source": "gitlab",
        "name": "dws-chat-agent 新 MR",
        "projects": ["inoryirony/dws-chat-agent"],
        "instruction": "审查这个 MR 的变更要点，给出风险提示。"
      }
    ]

蓝图铁律落点：
1. Watch = 预授权边界：规则命中开的轮带 origin="watch-jira"/"watch-gitlab"，
   处理范围由 instruction 限定；范围外无对外 DM 授权（投递出口=
   conversation_id 前缀查 Channel，jira:/gitlab: 只能回评论）。
2. 命中可观测：建 task（board_tasks 即面板可见）+ LOG；不静默起轮。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .sources.base import SourceItem


@dataclass(frozen=True)
class WatchRule:
    name: str
    source: str
    instruction: str
    # 源侧匹配器（JiraSource 用 jql/projects；GitLabSource 用 projects）。
    jql: str = ""
    projects: tuple[str, ...] = ()
    # 终态通知：命中任务完成/取消后给本人发钉钉 DM（人不看 issue，完成
    # 信号必须主动送达）。"none" 关闭通知；缺省即通知。
    notify: str = "dm"

    @property
    def origin(self) -> str:
        return f"watch-{self.source}"


def parse_watches(raw: Any) -> list[WatchRule]:
    """config `watches` 段 → 规则清单；缺省/空段 = 无 Watch（零行为变化）。"""
    if not raw:
        return []
    if not isinstance(raw, (list, tuple)):
        raise ValueError("config watches must be a list")
    rules: list[WatchRule] = []
    for entry in raw:
        if not isinstance(entry, Mapping):
            raise ValueError("watch entry must be an object")
        source = str(entry.get("source") or "")
        name = str(entry.get("name") or f"watch-{source}")
        instruction = str(entry.get("instruction") or "")
        if not source:
            raise ValueError("watch entry requires a source")
        if not instruction:
            raise ValueError(f"watch {name!r} requires an instruction")
        notify = str(entry.get("notify") or "dm")
        if notify not in {"dm", "none"}:
            raise ValueError(f"watch {name!r} notify must be 'dm' or 'none'")
        projects = tuple(str(item) for item in (entry.get("projects") or []))
        rules.append(
            WatchRule(
                name=name,
                source=source,
                instruction=instruction,
                jql=str(entry.get("jql") or ""),
                projects=projects,
                notify=notify,
            )
        )
    return rules


def rule_for_item(
    rules: list[WatchRule], item: SourceItem
) -> WatchRule | None:
    """SourceItem → 首条匹配规则。匹配 = source 相同且（有 projects
    限定时项目命中）；Jira 的 jql 过滤已由 JiraSource 在查询侧完成。"""
    meta = dict(item.meta or {})
    for rule in rules:
        if rule.source != str(meta.get("source") or ""):
            continue
        if not rule.projects:
            return rule
        key = str(meta.get("key") or "")
        project = str(meta.get("project") or "")
        if key and key.rsplit("-", 1)[0] in rule.projects:
            return rule
        if project and project in rule.projects:
            return rule
    return None


__all__ = ["WatchRule", "parse_watches", "rule_for_item"]
