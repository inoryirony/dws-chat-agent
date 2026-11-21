"""受控 GitLab MR 合并出口。

只使用 GitLab 官方 REST API：
https://docs.gitlab.com/api/merge_requests/
https://docs.gitlab.com/api/discussions/

模块从现有 ``GITLAB_HOST`` / ``GITLAB_TOKEN`` 取固定端点和凭据；调用方
只传项目、MR、已审阅 SHA 和单一目标分支，worker 不接触 token，也不能
构造任意 URL。PUT 结果不能提供远端合并凭据时返回 uncertain，后续只能
用 ``reconcile_merge`` GET 对账，不能盲目重放 PUT。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal


_PER_PAGE = 100
_SHA_RE = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})\Z")
_HOST_RE = re.compile(r"[A-Za-z0-9.-]+(?::[0-9]{1,5})?\Z")


def _merge_targets() -> frozenset[str]:
    """允许的合并目标分支：默认 dev/main，可用 GITLAB_MERGE_TARGETS 扩展（逗号分隔，如 test,dev,main）。"""
    raw = os.environ.get("GITLAB_MERGE_TARGETS", "")
    branches = {
        branch.strip()
        for branch in raw.split(",")
        if branch.strip()
    }
    return frozenset(branches or {"dev", "main"})


class GitLabMergeError(RuntimeError):
    """Base error for the controlled merge boundary."""


class MergeRejected(GitLabMergeError):
    """The reviewed intent or current remote state is not safe to merge."""


class MergeProtocolError(GitLabMergeError):
    """GitLab omitted or contradicted a signal required for a safe decision."""


@dataclass(frozen=True)
class HttpResponse:
    data: Any
    headers: Mapping[str, str]
    status: int


@dataclass(frozen=True)
class MergeInspection:
    project: str
    iid: int
    sha: str
    target_branch: str
    source_branch: str
    author: str
    diff: str
    discussion_text: str
    web_url: str


@dataclass(frozen=True)
class MergeSubmission:
    status: Literal["confirmed", "uncertain"]
    project: str
    iid: int
    sha: str
    target_branch: str
    source_branch: str
    author: str
    state: str
    remote_id: str
    web_url: str
    merge_user: str = ""
    reason: str = ""


Request = Callable[..., HttpResponse]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Mapping[str, str],
        newurl: str,
    ) -> None:
        return None


def _request_json(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, object] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 30,
) -> HttpResponse:
    data = None
    effective_headers = {"Accept": "application/json", **(headers or {})}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        effective_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        url, data=data, headers=effective_headers, method=method
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    with opener.open(request, timeout=timeout) as response:
        body = response.read().decode("utf-8")
        try:
            parsed = json.loads(body) if body.strip() else {}
        except json.JSONDecodeError as exc:
            raise MergeProtocolError("GitLab returned invalid JSON") from exc
        return HttpResponse(parsed, dict(response.headers.items()), int(response.status))


class GitLabMergeClient:
    def __init__(
        self,
        *,
        host: str,
        token: str,
        request: Request = _request_json,
        timeout: float = 30,
    ) -> None:
        normalized_host = host.strip()
        if not _HOST_RE.fullmatch(normalized_host):
            raise MergeProtocolError("GITLAB_HOST must be one fixed host name")
        if not token:
            raise MergeProtocolError("GITLAB_TOKEN is required")
        self._base_url = f"https://{normalized_host}/api/v4"
        self._headers = {"PRIVATE-TOKEN": token}
        self._request = request
        self._timeout = timeout

    async def inspect_merge(
        self,
        project: str,
        iid: int,
        expected_sha: str,
        target_branch: str,
    ) -> MergeInspection:
        project_path, expected_sha, target = self._validated_intent(
            project, iid, expected_sha, target_branch
        )
        first = await self._get_merge_request(project_path, iid)
        self._validate_open_merge_request(first, iid, expected_sha, target)

        root = f"/projects/{project_path}/merge_requests/{iid}"
        diffs = await self._get_all_pages(f"{root}/diffs")
        discussions = await self._get_all_pages(f"{root}/discussions")
        last = await self._get_merge_request(project_path, iid)
        self._validate_stable_identity(first, last)
        self._validate_open_merge_request(last, iid, expected_sha, target)
        diff = self._complete_diff(diffs, self._changes_count(last.get("changes_count")))
        identity = self._remote_identity(last, iid, expected_sha, target)
        return MergeInspection(
            project=project,
            iid=iid,
            sha=expected_sha,
            target_branch=target,
            source_branch=identity[0],
            author=identity[1],
            diff=diff,
            discussion_text=self._discussion_text(discussions),
            web_url=identity[2],
        )

    async def submit_merge(
        self,
        project: str,
        iid: int,
        expected_sha: str,
        target_branch: str,
    ) -> MergeSubmission:
        inspection = await self.inspect_merge(
            project, iid, expected_sha, target_branch
        )
        project_path = self._project_path(project)
        url = self._url(
            f"/projects/{project_path}/merge_requests/{iid}/merge"
        )
        try:
            response = await self._call(
                url, method="PUT", payload={"sha": inspection.sha}
            )
        except urllib.error.HTTPError as exc:
            if 400 <= exc.code < 500:
                raise MergeRejected(f"GitLab merge rejected with HTTP {exc.code}") from exc
            return self._uncertain(inspection, f"HTTP {exc.code} after merge request")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return self._uncertain(inspection, type(exc).__name__)
        except MergeProtocolError as exc:
            return self._uncertain(inspection, str(exc))
        return self._submission_from_response(response.data, inspection)

    async def reconcile_merge(
        self,
        project: str,
        iid: int,
        expected_sha: str,
        target_branch: str,
    ) -> MergeSubmission:
        project_path, expected_sha, target = self._validated_intent(
            project, iid, expected_sha, target_branch
        )
        merge_request = await self._get_merge_request(project_path, iid)
        source_branch, author, web_url = self._remote_identity(
            merge_request, iid, expected_sha, target
        )
        state = str(merge_request.get("state") or "")
        if state == "opened":
            return MergeSubmission(
                "uncertain",
                project,
                iid,
                expected_sha,
                target,
                source_branch,
                author,
                state,
                "",
                web_url,
                reason="merge request is still open",
            )
        if state != "merged":
            raise MergeRejected(f"merge request state is {state or 'missing'}")
        return self._confirmed_or_uncertain(
            merge_request,
            project=project,
            iid=iid,
            sha=expected_sha,
            target_branch=target,
            source_branch=source_branch,
            author=author,
            web_url=web_url,
        )

    def _validated_intent(
        self,
        project: str,
        iid: int,
        expected_sha: str,
        target_branch: str,
    ) -> tuple[str, str, str]:
        project_path = self._project_path(project)
        if isinstance(iid, bool) or not isinstance(iid, int) or iid <= 0:
            raise MergeRejected("merge request IID must be a positive integer")
        if not _SHA_RE.fullmatch(expected_sha):
            raise MergeRejected("expected SHA must be a full hexadecimal commit SHA")
        if (
            not isinstance(target_branch, str)
            or target_branch not in _merge_targets()
        ):
            raise MergeRejected(
                f"merge target must be one of: {sorted(_merge_targets())}"
            )
        return project_path, expected_sha.lower(), target_branch

    @staticmethod
    def _project_path(project: str) -> str:
        if not project or any(ord(char) < 32 for char in project):
            raise MergeRejected("project path is required")
        return urllib.parse.quote(project, safe="")

    def _url(self, path: str, params: Mapping[str, object] | None = None) -> str:
        url = f"{self._base_url}{path}"
        return f"{url}?{urllib.parse.urlencode(params or {})}" if params else url

    async def _call(
        self,
        url: str,
        *,
        method: str = "GET",
        payload: dict[str, object] | None = None,
    ) -> HttpResponse:
        response = await asyncio.to_thread(
            self._request,
            url,
            method=method,
            payload=payload,
            headers=self._headers,
            timeout=self._timeout,
        )
        if not isinstance(response, HttpResponse):
            raise MergeProtocolError("GitLab transport returned an invalid response")
        if response.status < 200 or response.status >= 300:
            raise urllib.error.HTTPError(
                url, response.status, f"GitLab returned HTTP {response.status}", None, None
            )
        return response

    async def _get_merge_request(
        self, project_path: str, iid: int
    ) -> Mapping[str, Any]:
        url = self._url(f"/projects/{project_path}/merge_requests/{iid}")
        try:
            response = await self._call(url)
        except urllib.error.HTTPError as exc:
            raise MergeRejected(f"GitLab read rejected with HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise MergeProtocolError("GitLab read did not complete") from exc
        if not isinstance(response.data, Mapping):
            raise MergeProtocolError("merge request response is not an object")
        return response.data

    async def _get_all_pages(self, path: str) -> list[Mapping[str, Any]]:
        items: list[Mapping[str, Any]] = []
        total_pages: int | None = None
        page = 1
        while True:
            response = await self._call(
                self._url(path, {"page": page, "per_page": _PER_PAGE})
            )
            if not isinstance(response.data, list) or any(
                not isinstance(item, Mapping) for item in response.data
            ):
                raise MergeProtocolError("paginated GitLab response is not an object list")
            headers = {str(key).lower(): str(value) for key, value in response.headers.items()}
            required = {"x-page", "x-per-page", "x-total-pages", "x-next-page"}
            if not required.issubset(headers):
                raise MergeProtocolError("GitLab pagination headers are incomplete")
            try:
                current = int(headers["x-page"])
                per_page = int(headers["x-per-page"])
                reported_total = int(headers["x-total-pages"])
            except ValueError as exc:
                raise MergeProtocolError("GitLab pagination headers are invalid") from exc
            if (
                current != page
                or per_page != _PER_PAGE
                or reported_total < 1
                or (total_pages is not None and total_pages != reported_total)
            ):
                raise MergeProtocolError("GitLab pagination sequence is inconsistent")
            total_pages = reported_total
            expected_next = str(page + 1) if page < total_pages else ""
            if headers["x-next-page"] != expected_next:
                raise MergeProtocolError("GitLab pagination sequence is incomplete")
            items.extend(response.data)
            if not expected_next:
                return items
            page += 1

    @staticmethod
    def _validate_open_merge_request(
        merge_request: Mapping[str, Any], iid: int, sha: str, target: str
    ) -> None:
        if merge_request.get("iid") != iid:
            raise MergeRejected("merge request IID does not match")
        if str(merge_request.get("state") or "") != "opened":
            raise MergeRejected("merge request is not open")
        if str(merge_request.get("sha") or "").lower() != sha:
            raise MergeRejected("merge request SHA does not match reviewed SHA")
        if str(merge_request.get("target_branch") or "") != target:
            raise MergeRejected("merge request target branch is not allowed")
        draft = merge_request.get("draft")
        work_in_progress = merge_request.get("work_in_progress")
        if draft is True or work_in_progress is True:
            raise MergeRejected("draft merge request cannot be merged")
        if draft not in {True, False} and work_in_progress not in {True, False}:
            raise MergeProtocolError("draft status signal is missing")
        detailed_status = str(merge_request.get("detailed_merge_status") or "")
        merge_status = str(merge_request.get("merge_status") or "")
        if not (
            detailed_status == "mergeable"
            or (not detailed_status and merge_status == "can_be_merged")
        ):
            raise MergeRejected("mergeability is not clean and current")
        if merge_request.get("blocking_discussions_resolved") is not True:
            raise MergeRejected("discussion summary is missing or unresolved")
        pipeline = merge_request.get("head_pipeline")
        if not isinstance(pipeline, Mapping) or (
            str(pipeline.get("sha") or "").lower() != sha
            or str(pipeline.get("status") or "") != "success"
        ):
            raise MergeRejected("pipeline for the reviewed SHA is not successful")

    @staticmethod
    def _validate_stable_identity(
        first: Mapping[str, Any], last: Mapping[str, Any]
    ) -> None:
        if str(first.get("sha") or "") != str(last.get("sha") or ""):
            raise MergeRejected("head SHA changed during inspection")
        if str(first.get("target_branch") or "") != str(
            last.get("target_branch") or ""
        ):
            raise MergeRejected("target branch changed during inspection")
        if str(first.get("source_branch") or "") != str(
            last.get("source_branch") or ""
        ):
            raise MergeRejected("source branch changed during inspection")
        if first.get("changes_count") != last.get("changes_count"):
            raise MergeRejected("changes_count changed during inspection")

    @staticmethod
    def _discussion_text(discussions: Sequence[Mapping[str, Any]]) -> str:
        comments: list[str] = []
        for discussion in discussions:
            notes = discussion.get("notes")
            if not isinstance(notes, list):
                raise MergeProtocolError("discussion notes signal is missing")
            for note in notes:
                if not isinstance(note, Mapping):
                    raise MergeProtocolError("discussion note is not an object")
                body = str(note.get("body") or "").strip()
                if not body or note.get("system") is True:
                    continue
                author_value = note.get("author")
                author = (
                    str(author_value.get("username") or author_value.get("name") or "")
                    if isinstance(author_value, Mapping)
                    else ""
                )
                state = "resolved" if note.get("resolved") is True else "open"
                comments.append(f"[{author or 'unknown'}; {state}] {body}")
        return "\n\n".join(comments)

    @staticmethod
    def _complete_diff(
        entries: Sequence[Mapping[str, Any]], changes_count: int | None,
    ) -> str:
        if not entries:
            raise MergeRejected("merge request diff is empty")
        sections: list[str] = []
        legacy_flags = False
        for entry in entries:
            has_collapsed = "collapsed" in entry
            has_too_large = "too_large" in entry
            if has_collapsed != has_too_large:
                raise MergeRejected("diff completeness signals are incomplete")
            if has_collapsed and (
                entry.get("collapsed") is not False
                or entry.get("too_large") is not False
            ):
                raise MergeRejected("diff omitted, collapsed, or too large")
            legacy_flags = legacy_flags or not has_collapsed
            old_path = entry.get("old_path")
            new_path = entry.get("new_path")
            body = entry.get("diff")
            if not all(isinstance(value, str) for value in (old_path, new_path, body)):
                raise MergeProtocolError("diff path or body signal is missing")
            if not body:
                raise MergeRejected("diff body is empty or omitted")
            if not old_path or not new_path:
                raise MergeProtocolError("diff path is empty")
            sections.append(
                f"diff --git a/{old_path} b/{new_path}\n{body.rstrip()}\n"
            )
        if changes_count is not None and changes_count != len(entries):
            raise MergeRejected("changes_count does not match the complete diff")
        if legacy_flags and changes_count is None:
            raise MergeRejected(
                "legacy diff requires an exact changes_count completeness proof"
            )
        return "".join(sections)

    @staticmethod
    def _changes_count(value: object) -> int | None:
        if value is None:
            return None
        text = str(value)
        if not text.isdigit():
            raise MergeRejected("changes_count is not an exact number")
        return int(text)

    @staticmethod
    def _remote_identity(
        merge_request: Mapping[str, Any], iid: int, sha: str, target: str
    ) -> tuple[str, str, str]:
        if merge_request.get("iid") != iid:
            raise MergeRejected("merge request IID does not match")
        if str(merge_request.get("sha") or "").lower() != sha:
            raise MergeRejected("merge request SHA does not match reviewed SHA")
        if str(merge_request.get("target_branch") or "") != target:
            raise MergeRejected("merge request target branch is not allowed")
        source_branch = str(merge_request.get("source_branch") or "")
        author_value = merge_request.get("author")
        author = (
            str(author_value.get("username") or "")
            if isinstance(author_value, Mapping)
            else ""
        )
        web_url = str(merge_request.get("web_url") or "")
        if not source_branch or not author or not web_url:
            raise MergeProtocolError("merge request identity signals are incomplete")
        return source_branch, author, web_url

    def _submission_from_response(
        self, value: Any, inspection: MergeInspection
    ) -> MergeSubmission:
        if not isinstance(value, Mapping):
            return self._uncertain(inspection, "merge response is not an object")
        try:
            source_branch, author, web_url = self._remote_identity(
                value,
                inspection.iid,
                inspection.sha,
                inspection.target_branch,
            )
        except GitLabMergeError as exc:
            return self._uncertain(inspection, str(exc))
        return self._confirmed_or_uncertain(
            value,
            project=inspection.project,
            iid=inspection.iid,
            sha=inspection.sha,
            target_branch=inspection.target_branch,
            source_branch=source_branch,
            author=author,
            web_url=web_url,
        )

    @staticmethod
    def _confirmed_or_uncertain(
        value: Mapping[str, Any],
        *,
        project: str,
        iid: int,
        sha: str,
        target_branch: str,
        source_branch: str,
        author: str,
        web_url: str,
    ) -> MergeSubmission:
        state = str(value.get("state") or "")
        remote_id = str(value.get("merge_commit_sha") or "")
        merge_user_value = value.get("merge_user") or value.get("merged_by")
        merge_user = (
            str(
                merge_user_value.get("username")
                or merge_user_value.get("name")
                or merge_user_value.get("id")
                or ""
            )
            if isinstance(merge_user_value, Mapping)
            else ""
        )
        confirmed = state == "merged" and bool(_SHA_RE.fullmatch(remote_id))
        return MergeSubmission(
            "confirmed" if confirmed else "uncertain",
            project,
            iid,
            sha,
            target_branch,
            source_branch,
            author,
            state,
            remote_id if confirmed else "",
            web_url,
            merge_user=merge_user if confirmed else "",
            reason="" if confirmed else "remote response did not prove a completed merge",
        )

    @staticmethod
    def _uncertain(inspection: MergeInspection, reason: str) -> MergeSubmission:
        return MergeSubmission(
            "uncertain",
            inspection.project,
            inspection.iid,
            inspection.sha,
            inspection.target_branch,
            inspection.source_branch,
            inspection.author,
            "",
            "",
            inspection.web_url,
            reason=reason,
        )


def _client_from_env() -> GitLabMergeClient:
    return GitLabMergeClient(
        host=os.environ.get("GITLAB_HOST", "git.youshu.com.cn"),
        token=os.environ.get("GITLAB_TOKEN", ""),
    )


async def inspect_merge(
    project: str,
    iid: int,
    expected_sha: str,
    target_branch: str,
) -> MergeInspection:
    return await _client_from_env().inspect_merge(
        project, iid, expected_sha, target_branch
    )


async def submit_merge(
    project: str,
    iid: int,
    expected_sha: str,
    target_branch: str,
) -> MergeSubmission:
    return await _client_from_env().submit_merge(
        project, iid, expected_sha, target_branch
    )


async def reconcile_merge(
    project: str,
    iid: int,
    expected_sha: str,
    target_branch: str,
) -> MergeSubmission:
    return await _client_from_env().reconcile_merge(
        project, iid, expected_sha, target_branch
    )


__all__ = [
    "GitLabMergeClient",
    "GitLabMergeError",
    "HttpResponse",
    "MergeInspection",
    "MergeProtocolError",
    "MergeRejected",
    "MergeSubmission",
    "inspect_merge",
    "reconcile_merge",
    "submit_merge",
]
