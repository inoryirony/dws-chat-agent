"""jira/gitlab Channel：终稿发成 issue / MR 评论。

凭据与端点经环境注入（.env）：JIRA_URL/JIRA_USERNAME/JIRA_API_TOKEN、
GITLAB_HOST/GITLAB_TOKEN。标准库 urllib + to_thread（本项目零三方依赖，
照 dws.py 子进程风格），缺凭据时 send 抛错（fail loud 到投递重试）。
能力位 supports_freshness=False——外部系统没有"本人手打接管"信号，
新鲜度检查跳过，人工边界由 Watch 规则的预授权范围承担（蓝图铁律 1）。
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any

from ..core import Contact, HistoryMessage


def _request(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 30,
) -> dict[str, Any]:
    data = None
    effective_headers = {"Accept": "application/json"}
    if headers:
        effective_headers.update(headers)
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        effective_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=effective_headers, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8")
    return json.loads(body) if body.strip() else {}


def _basic_auth(username: str, token: str) -> dict[str, str]:
    pair = base64.b64encode(f"{username}:{token}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {pair}"}


class JiraChannel:
    name = "jira"
    streaming_cards = False
    supports_freshness = False

    def _auth(self) -> tuple[str, dict[str, str]]:
        username = os.environ.get("JIRA_USERNAME", "")
        token = os.environ.get("JIRA_API_TOKEN", "")
        if not username or not token:
            raise ValueError("jira channel requires JIRA_USERNAME/JIRA_API_TOKEN")
        return username, _basic_auth(username, token)

    async def send(
        self, contact: Contact, content: str, send_uuid: str
    ) -> tuple[str | None, Any]:
        # contact.conversation_id 形如 jira:PROJ-123；issue key 即别名。
        issue_key = contact.alias
        _user, headers = self._auth()
        url = f"{os.environ.get('JIRA_URL', '').rstrip('/')}/rest/api/2/issue/{issue_key}/comment"
        payload = await asyncio.to_thread(
            _request, url, method="POST", payload={"body": content}, headers=headers
        )
        return str(payload.get("id") or ""), payload

    async def history(self, contact: Contact) -> list[HistoryMessage]:
        issue_key = contact.alias
        _user, headers = self._auth()
        url = f"{os.environ.get('JIRA_URL', '').rstrip('/')}/rest/api/2/issue/{issue_key}/comment"
        data = await asyncio.to_thread(_request, url, headers=headers)
        comments = data.get("comments", [])
        result: list[HistoryMessage] = []
        for item in comments:
            author = item.get("author") or {}
            created = str(item.get("created") or "")
            try:
                stamp = datetime.fromisoformat(created.replace("Z", "+00:00"))
            except ValueError:
                stamp = datetime.now().astimezone()
            result.append(
                HistoryMessage(
                    message_id=str(item.get("id") or ""),
                    conversation_id=f"jira:{issue_key}",
                    sender=str(author.get("name") or author.get("displayName") or ""),
                    sender_open_dingtalk_id="",
                    content=str(item.get("body") or ""),
                    created_at=stamp,
                )
            )
        return result


class GitLabChannel:
    name = "gitlab"
    streaming_cards = False
    supports_freshness = False

    def _headers(self) -> dict[str, str]:
        token = os.environ.get("GITLAB_TOKEN", "")
        if not token:
            raise ValueError("gitlab channel requires GITLAB_TOKEN")
        return {"PRIVATE-TOKEN": token}

    async def send(
        self, contact: Contact, content: str, send_uuid: str
    ) -> tuple[str | None, Any]:
        # contact.alias 形如 'group/project!mr_number'。
        project, _, number = contact.alias.partition("!")
        host = os.environ.get("GITLAB_HOST", "git.youshu.com.cn")
        encoded = project.replace("/", "%2F")
        url = f"https://{host}/api/v4/projects/{encoded}/merge_requests/{number}/notes"
        payload = await asyncio.to_thread(
            _request,
            url,
            method="POST",
            payload={"body": content},
            headers=self._headers(),
        )
        return str(payload.get("id") or ""), payload

    async def history(self, contact: Contact) -> list[HistoryMessage]:
        return []


__all__ = ["GitLabChannel", "JiraChannel"]
