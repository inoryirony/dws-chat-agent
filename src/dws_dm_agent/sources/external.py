"""Jira / GitLab 触发源（蓝图步 7/8）。

Jira：REST JQL 轮询（标准库 urllib，照 channels/external.py 的
_request 复用）。凭据 = JIRA_URL/JIRA_USERNAME/JIRA_API_TOKEN
（hsj-atlassian-mcp launcher 同源凭据，.env 直配）。
GitLab：glab CLI 子进程（本机已登录 git.youshu.com.cn），与
DwsClient._json_command 同款 asyncio 子进程模板。
"""

from __future__ import annotations

import asyncio
import json
import os
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any

from ..core import Contact
from .base import SourceItem
from .http import request_json


class JiraSource:
    """JQL 查询轮询：`jql` 是 watch 配置的匹配器（如
    `assignee = currentUser() AND created >= -1d ORDER BY created DESC`）。
    """

    name = "jira"

    def __init__(self, jql: str, projects: list[str] | None = None) -> None:
        self.jql = jql
        self.projects = projects or []

    def _auth_header(self) -> dict[str, str]:
        from .http import basic_auth_header

        username = os.environ.get("JIRA_USERNAME", "")
        token = os.environ.get("JIRA_API_TOKEN", "")
        if not username or not token:
            raise ValueError("jira source requires JIRA_USERNAME/JIRA_API_TOKEN")
        return basic_auth_header(username, token)

    async def poll(self, since: datetime) -> list[SourceItem]:
        url = (
            os.environ.get("JIRA_URL", "").rstrip("/")
            + "/rest/api/2/search"
        )
        payload = await asyncio.to_thread(
            request_json,
            url,
            params={"jql": self.jql, "maxResults": 50, "fields": "summary,description,created"},
            headers=self._auth_header(),
        )
        items: list[SourceItem] = []
        for issue in payload.get("issues", []):
            key = str(issue.get("key") or "")
            if not key:
                continue
            if self.projects and key.split("-")[0] not in self.projects:
                continue
            fields = issue.get("fields") or {}
            created_raw = str(fields.get("created") or "")
            try:
                created = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
            except ValueError:
                created = None
            if created is not None and created < _aware(since):
                continue
            items.append(
                SourceItem(
                    dedup_key=f"jira:{key}",
                    conversation_id=f"jira:{key}",
                    contact=_virtual_contact(f"jira:{key}", key),
                    title=str(fields.get("summary") or key),
                    body=str(fields.get("description") or ""),
                    url=f"{os.environ.get('JIRA_URL', '').rstrip('/')}/browse/{key}",
                    created_at=created,
                    meta={"source": "jira", "key": key},
                )
            )
        return items


class GitLabSource:
    """GitLab API 轮询打开中的 MR（标准库 urllib，同 Jira 通路；不依赖
    glab CLI——本机 glab repo list 实测返 0，API 直连更可靠）。
    `projects` 是 'group/project' 列表。dedup key 稳定为 mr iid。
    """

    name = "gitlab"

    def __init__(self, projects: list[str]) -> None:
        self.projects = projects

    def _headers(self) -> dict[str, str]:
        token = os.environ.get("GITLAB_TOKEN", "")
        if not token:
            raise ValueError("gitlab source requires GITLAB_TOKEN")
        return {"PRIVATE-TOKEN": token}

    async def poll(self, since: datetime) -> list[SourceItem]:
        from .http import request_json

        host = os.environ.get("GITLAB_HOST", "git.youshu.com.cn")
        headers = self._headers()
        items: list[SourceItem] = []
        for project in self.projects:
            encoded = urllib.parse.quote(project, safe="")
            raw = await asyncio.to_thread(
                request_json,
                f"https://{host}/api/v4/projects/{encoded}/merge_requests",
                params={"state": "opened", "per_page": "50"},
                headers=headers,
            )
            for mr in raw if isinstance(raw, list) else []:
                number = str(mr.get("iid") or "")
                if not number:
                    continue
                updated_raw = str(mr.get("updated_at") or "")
                try:
                    updated = datetime.fromisoformat(
                        updated_raw.replace("Z", "+00:00")
                    )
                except ValueError:
                    updated = None
                if updated is not None and updated < _aware(since):
                    continue
                dedup = f"gitlab:{project}!{number}"
                items.append(
                    SourceItem(
                        dedup_key=dedup,
                        conversation_id=dedup,
                        contact=_virtual_contact(dedup, f"{project}!{number}"),
                        title=str(mr.get("title") or number),
                        body=str(mr.get("description") or ""),
                        url=str(mr.get("web_url") or ""),
                        created_at=updated,
                        meta={"source": "gitlab", "project": project, "iid": number},
                    )
                )
        return items




def _aware(value: datetime) -> datetime:
    """naive 时间戳按本地时区补全；比较侧统一 aware。"""
    if value.tzinfo is None:
        return value.astimezone()
    return value


def _virtual_contact(conversation_id: str, alias: str) -> Contact:
    return Contact(
        alias=alias,
        display_name=conversation_id,
        user_id=conversation_id,
        open_dingtalk_id="",
    )


__all__ = ["GitLabSource", "JiraSource"]
