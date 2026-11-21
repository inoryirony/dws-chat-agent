from __future__ import annotations

import copy
import os
import urllib.error
import urllib.request
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from dws_dm_agent.channels.gitlab_merge import (
    GitLabMergeClient,
    HttpResponse,
    MergeProtocolError,
    MergeRejected,
)


SHA = "a" * 40


def _mr(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "iid": 7,
        "state": "opened",
        "sha": SHA,
        "target_branch": "dev",
        "source_branch": "feature/safe-change",
        "author": {"username": "review-author"},
        "draft": False,
        "work_in_progress": False,
        "detailed_merge_status": "mergeable",
        "has_conflicts": False,
        "blocking_discussions_resolved": True,
        "head_pipeline": {"sha": SHA, "status": "success"},
        "changes_count": "1",
        "web_url": "https://git.example/group/project/-/merge_requests/7",
    }
    value.update(overrides)
    return value


def _page_headers(page: int = 1, total: int = 1) -> dict[str, str]:
    return {
        "X-Page": str(page),
        "X-Per-Page": "100",
        "X-Total-Pages": str(total),
        "X-Next-Page": str(page + 1) if page < total else "",
    }


class FakeGitLab:
    def __init__(self, *, mr: dict[str, object] | None = None) -> None:
        self.mr = _mr() if mr is None else mr
        self.calls: list[dict[str, object]] = []
        self.diffs: dict[int, list[dict[str, object]]] = {
            1: [
                {
                    "old_path": "src/a.py",
                    "new_path": "src/a.py",
                    "diff": "@@ -1 +1 @@\n-old\n+new",
                    "collapsed": False,
                    "too_large": False,
                }
            ]
        }
        self.discussions: dict[int, list[dict[str, object]]] = {
            1: [
                {
                    "id": "discussion-1",
                    "notes": [{"resolvable": True, "resolved": True}],
                }
            ]
        }
        self.diff_total = 1
        self.discussion_total = 1
        self.merge_response: dict[str, object] = {
            **self.mr,
            "state": "merged",
            "merge_commit_sha": "b" * 40,
            "merge_user": {"username": "merge-bot"},
        }
        self.merge_error: Exception | None = None
        self.mr_reads = 0
        self.mr_after_first: dict[str, object] | None = None

    def __call__(
        self,
        url: str,
        *,
        method: str = "GET",
        payload: dict[str, object] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 30,
    ) -> HttpResponse:
        self.calls.append(
            {
                "url": url,
                "method": method,
                "payload": payload,
                "headers": headers,
                "timeout": timeout,
            }
        )
        parsed = urlsplit(url)
        query = parse_qs(parsed.query)
        path = parsed.path
        if path.endswith("/merge"):
            if self.merge_error is not None:
                raise self.merge_error
            return HttpResponse(self.merge_response, {}, 200)
        if path.endswith("/diffs"):
            page = int(query["page"][0])
            return HttpResponse(
                self.diffs.get(page, []), _page_headers(page, self.diff_total), 200
            )
        if path.endswith("/discussions"):
            page = int(query["page"][0])
            return HttpResponse(
                self.discussions.get(page, []),
                _page_headers(page, self.discussion_total),
                200,
            )
        if path.endswith("/merge_requests/7"):
            self.mr_reads += 1
            value = self.mr
            if self.mr_after_first is not None and self.mr_reads > 1:
                value = self.mr_after_first
            return HttpResponse(copy.deepcopy(value), {}, 200)
        raise AssertionError(f"unexpected request: {method} {url}")


class GitLabMergeInspectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_inspect_returns_complete_diff_and_quotes_project(self) -> None:
        fake = FakeGitLab()
        fake.diff_total = 2
        fake.mr["changes_count"] = "2"
        fake.diffs[2] = [
            {
                "old_path": "src/b.py",
                "new_path": "src/b.py",
                "diff": "@@ -0,0 +1 @@\n+value = 1",
                "collapsed": False,
                "too_large": False,
            }
        ]
        client = GitLabMergeClient(host="git.example", token="secret", request=fake)

        result = await client.inspect_merge("group/project", 7, SHA, "dev")

        self.assertEqual(result.target_branch, "dev")
        self.assertEqual(result.source_branch, "feature/safe-change")
        self.assertEqual(result.author, "review-author")
        self.assertEqual(result.sha, SHA)
        self.assertIn("diff --git a/src/a.py b/src/a.py", result.diff)
        self.assertIn("diff --git a/src/b.py b/src/b.py", result.diff)
        self.assertTrue(
            all("/projects/group%2Fproject/" in str(call["url"]) for call in fake.calls)
        )
        self.assertTrue(
            all(call["headers"] == {"PRIVATE-TOKEN": "secret"} for call in fake.calls)
        )

    async def test_inspect_rejects_target_change_even_when_sha_is_unchanged(self) -> None:
        fake = FakeGitLab()
        fake.mr_after_first = _mr(target_branch="main")
        client = GitLabMergeClient(host="git.example", token="secret", request=fake)

        with self.assertRaisesRegex(MergeRejected, "target branch changed"):
            await client.inspect_merge("group/project", 7, SHA, "dev")

    async def test_inspect_rejects_disallowed_or_malformed_targets(self) -> None:
        client = GitLabMergeClient(
            host="git.example", token="secret", request=FakeGitLab()
        )
        for target in ("feature", "", None, ("dev", "main"), ["dev"]):
            with self.subTest(target=target):
                with self.assertRaisesRegex(MergeRejected, "merge target must be"):
                    await client.inspect_merge("group/project", 7, SHA, target)

    async def test_inspect_allows_env_configured_targets(self) -> None:
        client = GitLabMergeClient(
            host="git.example",
            token="secret",
            request=FakeGitLab(mr=_mr(target_branch="test")),
        )
        with patch.dict(
            os.environ, {"GITLAB_MERGE_TARGETS": "test,dev,main"}
        ):
            # 默认集不含 test；env 扩展后应放行（校验目标=远端目标=test）。
            result = await client.inspect_merge("group/project", 7, SHA, "test")
        self.assertEqual(result.target_branch, "test")

    async def test_inspect_fails_closed_on_missing_or_blocking_signals(self) -> None:
        cases = {
            "draft": _mr(draft=True),
            "mergeability": _mr(detailed_merge_status="checking"),
            "pipeline": _mr(head_pipeline={"sha": SHA, "status": "running"}),
            "discussion summary": _mr(blocking_discussions_resolved=None),
        }
        for message, mr in cases.items():
            with self.subTest(message=message):
                client = GitLabMergeClient(
                    host="git.example", token="secret", request=FakeGitLab(mr=mr)
                )
                with self.assertRaisesRegex(MergeRejected, message):
                    await client.inspect_merge("group/project", 7, SHA, "dev")

        fake = FakeGitLab()
        fake.discussions[1][0]["notes"] = [{
            "resolvable": True,
            "resolved": False,
            "body": "This naming deserves a second look.",
            "author": {"username": "reviewer"},
        }]
        client = GitLabMergeClient(host="git.example", token="secret", request=fake)
        result = await client.inspect_merge("group/project", 7, SHA, "dev")
        self.assertIn("reviewer; open", result.discussion_text)
        self.assertIn("naming deserves", result.discussion_text)

    async def test_inspect_accepts_either_draft_synonym(self) -> None:
        for mr in (
            _mr(work_in_progress=None),
            _mr(draft=None, work_in_progress=False),
        ):
            with self.subTest(mr=mr):
                result = await GitLabMergeClient(
                    host="git.example", token="secret", request=FakeGitLab(mr=mr)
                ).inspect_merge("group/project", 7, SHA, "dev")
                self.assertEqual(result.sha, SHA)

        legacy = _mr(detailed_merge_status=None, merge_status="can_be_merged")
        result = await GitLabMergeClient(
            host="git.example", token="secret", request=FakeGitLab(mr=legacy)
        ).inspect_merge("group/project", 7, SHA, "dev")
        self.assertEqual(result.sha, SHA)

    async def test_inspect_rejects_incomplete_diff_and_pagination(self) -> None:
        fake = FakeGitLab()
        fake.diffs[1][0]["collapsed"] = True
        client = GitLabMergeClient(host="git.example", token="secret", request=fake)
        with self.assertRaisesRegex(MergeRejected, "diff omitted"):
            await client.inspect_merge("group/project", 7, SHA, "dev")

        def missing_pagination(*args: object, **kwargs: object) -> HttpResponse:
            response = FakeGitLab()(*args, **kwargs)
            if str(kwargs.get("method", "GET")) == "GET" and str(args[0]).split("?")[0].endswith("/diffs"):
                return HttpResponse(response.data, {}, response.status)
            return response

        client = GitLabMergeClient(
            host="git.example", token="secret", request=missing_pagination
        )
        with self.assertRaisesRegex(MergeProtocolError, "pagination"):
            await client.inspect_merge("group/project", 7, SHA, "dev")

    async def test_legacy_diff_proves_completeness_with_exact_changes_count(self) -> None:
        fake = FakeGitLab()
        fake.diffs[1][0].pop("collapsed")
        fake.diffs[1][0].pop("too_large")
        client = GitLabMergeClient(host="git.example", token="secret", request=fake)

        result = await client.inspect_merge("group/project", 7, SHA, "dev")
        self.assertIn("diff --git a/src/a.py", result.diff)

        for count in ("2", "1000+", None):
            with self.subTest(count=count):
                rejecting = FakeGitLab(mr=_mr(changes_count=count))
                rejecting.diffs[1][0].pop("collapsed")
                rejecting.diffs[1][0].pop("too_large")
                with self.assertRaisesRegex(MergeRejected, "changes_count"):
                    await GitLabMergeClient(
                        host="git.example", token="secret", request=rejecting
                    ).inspect_merge("group/project", 7, SHA, "dev")

    async def test_complete_diff_has_no_arbitrary_page_or_file_ceiling(self) -> None:
        fake = FakeGitLab(mr=_mr(changes_count="21"))
        fake.diff_total = 21
        fake.diffs = {
            page: [{
                "old_path": f"src/{page}.py",
                "new_path": f"src/{page}.py",
                "diff": f"@@ -0,0 +1 @@\n+value = {page}",
                "collapsed": False,
                "too_large": False,
            }]
            for page in range(1, 22)
        }
        result = await GitLabMergeClient(
            host="git.example", token="secret", request=fake
        ).inspect_merge("group/project", 7, SHA, "dev")
        self.assertIn("diff --git a/src/21.py", result.diff)

    async def test_sha_length_is_exactly_40_or_64(self) -> None:
        client = GitLabMergeClient(
            host="git.example", token="secret", request=FakeGitLab()
        )
        for sha in ("a" * 39, "a" * 41, "a" * 63, "a" * 65):
            with self.subTest(length=len(sha)):
                with self.assertRaisesRegex(MergeRejected, "full hexadecimal"):
                    await client.inspect_merge("group/project", 7, sha, "dev")


class GitLabMergeMutationTests(unittest.IsolatedAsyncioTestCase):
    async def test_submit_rechecks_then_puts_exact_sha_and_returns_remote_proof(self) -> None:
        fake = FakeGitLab()
        client = GitLabMergeClient(host="git.example", token="secret", request=fake)

        result = await client.submit_merge("group/project", 7, SHA, "dev")

        self.assertEqual(result.status, "confirmed")
        self.assertEqual(result.remote_id, "b" * 40)
        self.assertEqual(result.sha, SHA)
        merge_calls = [call for call in fake.calls if call["method"] == "PUT"]
        self.assertEqual(len(merge_calls), 1)
        self.assertEqual(merge_calls[0]["payload"], {"sha": SHA})
        self.assertNotIn("merge_when_pipeline_succeeds", merge_calls[0]["payload"])

    async def test_submit_network_uncertainty_does_not_retry_put(self) -> None:
        fake = FakeGitLab()
        fake.merge_error = urllib.error.URLError("connection lost after write")
        client = GitLabMergeClient(host="git.example", token="secret", request=fake)

        result = await client.submit_merge("group/project", 7, SHA, "dev")

        self.assertEqual(result.status, "uncertain")
        self.assertEqual(result.remote_id, "")
        self.assertEqual(
            len([call for call in fake.calls if call["method"] == "PUT"]), 1
        )

    async def test_submit_http_409_is_rejected(self) -> None:
        fake = FakeGitLab()
        fake.merge_error = urllib.error.HTTPError(
            "https://git.example", 409, "conflict", {}, None
        )
        client = GitLabMergeClient(host="git.example", token="secret", request=fake)

        with self.assertRaisesRegex(MergeRejected, "409"):
            await client.submit_merge("group/project", 7, SHA, "dev")

        response_fake = FakeGitLab()
        response_fake.merge_response = {}  # Transport below returns the status itself.

        def status_response(*args: object, **kwargs: object) -> HttpResponse:
            response = response_fake(*args, **kwargs)
            if kwargs.get("method") == "PUT":
                return HttpResponse({}, {}, 409)
            return response

        client = GitLabMergeClient(
            host="git.example", token="secret", request=status_response
        )
        with self.assertRaisesRegex(MergeRejected, "409"):
            await client.submit_merge("group/project", 7, SHA, "dev")

    async def test_reconcile_uses_get_only_and_confirms_remote_merge(self) -> None:
        fake = FakeGitLab(
            mr=_mr(
                state="merged",
                merge_commit_sha="b" * 40,
                merge_user={"username": "merge-bot"},
            )
        )
        client = GitLabMergeClient(host="git.example", token="secret", request=fake)

        result = await client.reconcile_merge("group/project", 7, SHA, "dev")

        self.assertEqual(result.status, "confirmed")
        self.assertEqual(result.remote_id, "b" * 40)
        self.assertEqual(result.merge_user, "merge-bot")
        self.assertEqual([call["method"] for call in fake.calls], ["GET"])

    async def test_reconcile_open_is_uncertain_and_changed_target_is_rejected(self) -> None:
        open_fake = FakeGitLab()
        client = GitLabMergeClient(
            host="git.example", token="secret", request=open_fake
        )
        result = await client.reconcile_merge("group/project", 7, SHA, "dev")
        self.assertEqual(result.status, "uncertain")
        self.assertEqual(result.state, "opened")

        changed = GitLabMergeClient(
            host="git.example",
            token="secret",
            request=FakeGitLab(mr=_mr(target_branch="main")),
        )
        with self.assertRaisesRegex(MergeRejected, "target branch"):
            await changed.reconcile_merge("group/project", 7, SHA, "dev")


class GitLabTransportTests(unittest.TestCase):
    def test_http_transport_refuses_redirect_instead_of_forwarding_token(self) -> None:
        class RedirectingOpener:
            def open(self, request: urllib.request.Request, timeout: float = 30) -> object:
                raise AssertionError("custom no-redirect handler was not installed")

        from dws_dm_agent.channels.gitlab_merge import _NoRedirect, _request_json

        with patch("urllib.request.build_opener") as build:
            build.return_value = RedirectingOpener()
            with self.assertRaises(AssertionError):
                _request_json(
                    "https://git.example/api/v4/projects/p",
                    headers={"PRIVATE-TOKEN": "secret"},
                )
        handler_types = [type(arg) for arg in build.call_args.args]
        self.assertIn(_NoRedirect, handler_types)


if __name__ == "__main__":
    unittest.main()
