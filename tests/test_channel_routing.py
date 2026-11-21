from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo

from dws_dm_agent.channels import channel_for
from dws_dm_agent.channels.external import GitLabChannel, JiraChannel
from dws_dm_agent.core import AuditStore, Contact, IncomingEvent
from dws_dm_agent.service import AgentService


TZ = ZoneInfo("Asia/Shanghai")
CONTACT = Contact("ZKAI-505", "jira:ZKAI-505", "jira:ZKAI-505", "")


class _ExternalSession:
    def __init__(self, prepared: SimpleNamespace, session_id: str) -> None:
        self.prepared = prepared
        self.session_id = session_id
        self.done = False
        self.silent = False
        self.error = ""
        self.escalated = False
        self.blocked = False
        self.exit_code = 0
        self.waits = 0

    async def start(self) -> None:
        return None

    async def wait_for_update(self, timeout: float | None = None) -> None:
        self.waits += 1
        if self.waits == 1:
            await asyncio.sleep((timeout or 0) + 0.001)
            raise TimeoutError()
        self.done = True

    def result(self) -> str:
        return "最终正文"

    async def abort(self) -> None:
        return None

    async def close(self) -> None:
        return None


class ExternalChannelRoutingTests(unittest.TestCase):
    def _event(self) -> IncomingEvent:
        return IncomingEvent(
            "event-1",
            "jira:ZKAI-505",
            "jira:ZKAI-505",
            CONTACT,
            "【Watch 命中】检查 issue",
            datetime(2026, 11, 19, 10, 0, tzinfo=TZ),
            origin="watch-jira",
        )

    def test_external_run_has_no_dws_processing_reaction(self) -> None:
        service = AgentService.__new__(AgentService)
        service.dws = SimpleNamespace(
            add_emoji=AsyncMock(),
            remove_emoji=AsyncMock(),
        )

        asyncio.run(service._set_processing_reactions([self._event()], active=True))
        asyncio.run(service._set_processing_reactions([self._event()], active=False))

        service.dws.add_emoji.assert_not_awaited()
        service.dws.remove_emoji.assert_not_awaited()

    def test_external_run_does_not_poll_history_while_worker_runs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = AgentService.__new__(AgentService)
            service.mode = "live"
            service.settings = SimpleNamespace(
                timezone=TZ,
                self_open_id="self-open-id",
                self_name="Operator",
                cooldown=0,
                state_dir=root,
                workspace_root=root,
                worktree_root=root,
                raw={"monitor_interval_seconds": 0.001},
            )
            service.store = AuditStore(root / "audit.sqlite3")
            service.dws = SimpleNamespace(
                history=AsyncMock(),
                add_emoji=AsyncMock(),
                remove_emoji=AsyncMock(),
                streaming_cards=True,
                supports_freshness=True,
            )
            service.runtime_states = {}
            service.live_sessions = {}
            service.progress_card_seqs = {}
            service._write_runtime = Mock()
            service._history = AsyncMock(return_value=[])
            service._render_stage_prompt = Mock(return_value="prompt")
            service._prompt_variables = Mock(return_value={})
            prepared = SimpleNamespace(timeout_seconds=30)
            holder: dict[str, _ExternalSession] = {}
            opened: dict[str, object] = {}

            def open_session(stage, session_id, *args, **kwargs):
                opened.update(kwargs)
                return holder.setdefault(
                    session_id, _ExternalSession(prepared, session_id)
                )

            service.agent_runtime = SimpleNamespace(
                skill_pool={},
                profile_name=Mock(return_value="worker"),
                stage_of_kind=Mock(return_value=None),
                executive_stage=Mock(return_value="worker"),
                resolve_stage=Mock(
                    return_value=SimpleNamespace(
                        profile=SimpleNamespace(
                            protocol="codex-app-server",
                            options={"service_tools_mcp": True},
                        )
                    )
                ),
                open_session=Mock(side_effect=open_session),
            )

            result = asyncio.run(service._run_agent([self._event()], ""))

            self.assertEqual(result.reply, "最终正文")
            service._history.assert_not_awaited()
            service.dws.history.assert_not_awaited()
            self.assertNotIn("external_source", opened)
            self.assertEqual(
                opened["task_context"]["conversation_id"], "jira:ZKAI-505"
            )
            service.store.close()

    def test_external_channel_disables_dws_live_capabilities(self) -> None:
        dws = SimpleNamespace(
            streaming_cards=True,
            processing_reactions=True,
            supports_freshness=True,
        )

        channel = channel_for("jira:ZKAI-505", dws)

        self.assertFalse(channel.streaming_cards)
        self.assertFalse(channel.processing_reactions)
        self.assertFalse(channel.supports_freshness)

    def test_jira_comment_carries_stable_action_marker_and_is_reconciled(self) -> None:
        responses = [
            {"id": "comment-1"},
            {"accountId": "bot-1"},
            {"comments": [{"id": "comment-1", "body": "ok\n<!-- dws-action:action-1 -->", "author": {"accountId": "bot-1"}}]},
        ]
        with patch.dict(
            os.environ,
            {"JIRA_URL": "https://jira.example", "JIRA_USERNAME": "u", "JIRA_API_TOKEN": "t"},
        ), patch(
            "dws_dm_agent.channels.external._request", side_effect=responses
        ) as request:
            channel = JiraChannel()
            asyncio.run(channel.send(CONTACT, "ok", "action-1"))
            receipt = asyncio.run(channel.find_receipt(CONTACT, "action-1"))

        self.assertEqual(receipt, "comment-1")
        self.assertIn("<!-- dws-action:action-1 -->", request.call_args_list[0].kwargs["payload"]["body"])

    def test_gitlab_note_carries_stable_action_marker_and_is_reconciled(self) -> None:
        contact = Contact("g/p!1", "gitlab:g/p!1", "gitlab:g/p!1", "")
        responses = [
            {"id": 7},
            {"id": 11},
            [{"id": 7, "body": "ok\n<!-- dws-action:action-2 -->", "author": {"id": 11}}],
        ]
        with patch.dict(os.environ, {"GITLAB_TOKEN": "t"}), patch(
            "dws_dm_agent.channels.external._request", side_effect=responses
        ) as request:
            channel = GitLabChannel()
            asyncio.run(channel.send(contact, "ok", "action-2"))
            receipt = asyncio.run(channel.find_receipt(contact, "action-2"))

        self.assertEqual(receipt, "7")
        self.assertIn("<!-- dws-action:action-2 -->", request.call_args_list[0].kwargs["payload"]["body"])

    def test_reconciliation_ignores_marker_copied_by_another_author(self) -> None:
        jira_responses = [
            {"accountId": "bot-1"},
            {"comments": [{
                "id": "foreign-comment", "body": "<!-- dws-action:action-1 -->",
                "author": {"accountId": "user-2"},
            }]},
        ]
        with patch.dict(
            os.environ,
            {"JIRA_URL": "https://jira.example", "JIRA_USERNAME": "u", "JIRA_API_TOKEN": "t"},
        ), patch(
            "dws_dm_agent.channels.external._request", side_effect=jira_responses
        ) as request:
            receipt = asyncio.run(JiraChannel().find_receipt(CONTACT, "action-1"))

        self.assertIsNone(receipt)
        self.assertEqual(request.call_count, 2)


class WatchDeliveryContractTests(unittest.TestCase):
    def test_prompt_keeps_external_runs_off_dws_and_source_comments(self) -> None:
        shared = (
            Path(__file__).resolve().parents[1] / "prompts" / "shared.md"
        ).read_text(encoding="utf-8")

        self.assertIn("jira:", shared)
        self.assertIn("gitlab:", shared)
        self.assertIn("不要调用 dws", shared)
        self.assertIn("不要自行写 Jira/GitLab 评论", shared)

    def test_watch_instructions_delegate_final_comment_to_service(self) -> None:
        root = Path(__file__).resolve().parents[1]
        data = json.loads((root / "config.example.json").read_text(encoding="utf-8"))
        for watch in data["watches"]:
            with self.subTest(watch=watch["name"]):
                self.assertIn("由服务", watch["instruction"])
                self.assertIn("不要自行", watch["instruction"])


if __name__ == "__main__":
    unittest.main()
