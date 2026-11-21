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
) -> Any:
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


def _action_marker(send_uuid: str) -> str:
    return f"<!-- dws-action:{send_uuid} -->"


def _identity_values(value: Any) -> set[str]:
    if not isinstance(value, dict):
        return set()
    return {
        str(value.get(field))
        for field in ("id", "accountId", "key", "name", "username", "emailAddress")
        if value.get(field) not in (None, "")
    }


class JiraChannel:
    name = "jira"
    streaming_cards = False
    processing_reactions = False
    supports_freshness = False
    retry_safe = False
    supports_reconciliation = True

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
            _request,
            url,
            method="POST",
            payload={"body": f"{content}\n\n{_action_marker(send_uuid)}"},
            headers=headers,
        )
        return str(payload.get("id") or ""), payload

    async def find_receipt(self, contact: Contact, send_uuid: str) -> str | None:
        _user, headers = self._auth()
        base = os.environ.get('JIRA_URL', '').rstrip('/')
        current_user = await asyncio.to_thread(
            _request, f"{base}/rest/api/2/myself", headers=headers
        )
        identities = _identity_values(current_user)
        if not identities:
            return None
        url = (
            f"{base}/rest/api/2/issue/"
            f"{contact.alias}/comment"
        )
        data = await asyncio.to_thread(_request, url, headers=headers)
        marker = _action_marker(send_uuid)
        for item in data.get("comments", []):
            body = item.get("body") or ""
            if (
                marker in (body if isinstance(body, str) else json.dumps(body))
                and identities.intersection(_identity_values(item.get("author")))
            ):
                return str(item.get("id") or "") or None
        return None

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
    processing_reactions = False
    supports_freshness = False
    retry_safe = False
    supports_reconciliation = True

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
            payload={"body": f"{content}\n\n{_action_marker(send_uuid)}"},
            headers=self._headers(),
        )
        return str(payload.get("id") or ""), payload

    async def find_receipt(self, contact: Contact, send_uuid: str) -> str | None:
        project, _, number = contact.alias.partition("!")
        host = os.environ.get("GITLAB_HOST", "git.youshu.com.cn")
        encoded = project.replace("/", "%2F")
        current_user = await asyncio.to_thread(
            _request, f"https://{host}/api/v4/user", headers=self._headers()
        )
        identities = _identity_values(current_user)
        if not identities:
            return None
        url = f"https://{host}/api/v4/projects/{encoded}/merge_requests/{number}/notes"
        notes = await asyncio.to_thread(_request, url, headers=self._headers())
        marker = _action_marker(send_uuid)
        for item in notes:
            if (
                marker in str(item.get("body") or "")
                and identities.intersection(_identity_values(item.get("author")))
            ):
                return str(item.get("id") or "") or None
        return None

    async def history(self, contact: Contact) -> list[HistoryMessage]:
        return []


__all__ = ["GitLabChannel", "JiraChannel"]
