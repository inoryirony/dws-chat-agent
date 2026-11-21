from __future__ import annotations

import os
import tempfile

import pytest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from dws_dm_agent.channels.gitlab_merge import MergeRejected, MergeSubmission
from dws_dm_agent.core import AuditStore, Contact, IncomingEvent
from dws_dm_agent.service import AgentService
from dws_dm_agent.watches import WatchRule


NOW = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)
HEAD_SHA = "a" * 40
MERGE_SHA = "b" * 40
PROJECT = "team/repo"
CONVERSATION = f"gitlab:{PROJECT}!7"


def _rule(merge_targets: tuple[str, ...] = ("main",)) -> WatchRule:
    return WatchRule(
        name="merge",
        source="gitlab",
        instruction="review",
        projects=(PROJECT,),
        notify="none",
        merge_targets=merge_targets,
    )


def _event(rule: WatchRule) -> IncomingEvent:
    return IncomingEvent(
        event_id="event-1",
        message_id="message-1",
        conversation_id=CONVERSATION,
        contact=Contact("mr", "MR", CONVERSATION, ""),
        content="review",
        created_at=NOW,
        origin="watch-gitlab",
        watch_authority=rule.authority_key,
        source_revision=HEAD_SHA,
        target_branch="main",
    )


def _store_with_merge(path: Path) -> tuple[AuditStore, IncomingEvent, WatchRule, str]:
    rule = _rule()
    event = _event(rule)
    store = AuditStore(path, self_id="self")
    assert store.claim_event(event)
    store.register_task_run("run-1", NOW)
    store.register_run(
        "run-1",
        contact_id=CONVERSATION,
        stage="executive",
        now=NOW,
        event_ids=[event.message_id],
    )
    action_id, status = store.enqueue_gitlab_merge(
        event,
        host="git.example.com",
        project=PROJECT,
        iid=7,
        source_ref="feature",
        target_ref="main",
        expected_sha=HEAD_SHA,
        assessment="reviewed",
        watch_authority=rule.authority_key,
        session_id="run-1",
        event_ids=[event.message_id],
    )
    assert status == "pending"
    return store, event, rule, action_id


def test_store_rejects_target_outside_configured_merge_targets(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = AuditStore(Path(tmp) / "store.db", self_id="self")
        rule = _rule(merge_targets=("test",))
        event = IncomingEvent(
            event_id="event-1",
            message_id="message-1",
            conversation_id=CONVERSATION,
            contact=Contact("mr", "MR", CONVERSATION, ""),
            content="review",
            created_at=NOW,
            origin="watch-gitlab",
            watch_authority=rule.authority_key,
            source_revision=HEAD_SHA,
            target_branch="test",
        )
        assert store.claim_event(event)
        store.register_task_run("run-1", NOW)
        store.register_run(
            "run-1",
            contact_id=CONVERSATION,
            stage="executive",
            now=NOW,
            event_ids=[event.message_id],
        )
        # env 未放宽时，store 层拒绝 test 目标（与 client 层同源判定）。
        monkeypatch.delenv("GITLAB_MERGE_TARGETS", raising=False)
        with pytest.raises(ValueError, match="merge_source_mismatch"):
            store.enqueue_gitlab_merge(
                event,
                host="git.example.com",
                project=PROJECT,
                iid=7,
                source_ref="feature",
                target_ref="test",
                expected_sha=HEAD_SHA,
                assessment="reviewed",
                watch_authority=rule.authority_key,
                session_id="run-1",
                event_ids=[event.message_id],
            )
        # env 放宽后，store 层与 client 层放行一致。
        monkeypatch.setenv("GITLAB_MERGE_TARGETS", "test,dev,main")
        action_id, status = store.enqueue_gitlab_merge(
            event,
            host="git.example.com",
            project=PROJECT,
            iid=7,
            source_ref="feature",
            target_ref="test",
            expected_sha=HEAD_SHA,
            assessment="reviewed",
            watch_authority=rule.authority_key,
            session_id="run-1",
            event_ids=[event.message_id],
        )
        assert status == "pending"


def _service(store: AuditStore, rule: WatchRule) -> AgentService:
    service = AgentService.__new__(AgentService)
    service.store = store
    service.settings = SimpleNamespace(
        timezone=timezone.utc,
        raw={
            "watches": [
                {
                    "name": rule.name,
                    "source": rule.source,
                    "instruction": rule.instruction,
                    "projects": list(rule.projects),
                    "notify": rule.notify,
                    "merge_targets": list(rule.merge_targets),
                }
            ]
        },
    )
    service._watch_rules = [rule]
    return service


def _confirmed() -> MergeSubmission:
    return MergeSubmission(
        "confirmed",
        PROJECT,
        7,
        HEAD_SHA,
        "main",
        "feature",
        "author",
        "merged",
        MERGE_SHA,
        "https://git.example.com/team/repo/-/merge_requests/7",
    )


def test_merge_intent_requires_live_registered_run_and_is_stable(tmp_path: Path) -> None:
    rule = _rule()
    event = _event(rule)
    store = AuditStore(tmp_path / "audit.sqlite3", self_id="self")
    assert store.claim_event(event)
    kwargs = dict(
        host="git.example.com",
        project=PROJECT,
        iid=7,
        source_ref="feature",
        target_ref="main",
        expected_sha=HEAD_SHA,
        assessment="reviewed",
        watch_authority=rule.authority_key,
        session_id="run-1",
        event_ids=[event.message_id],
    )
    try:
        store.enqueue_gitlab_merge(event, **kwargs)
        raise AssertionError("unregistered run must be rejected")
    except ValueError as exc:
        assert str(exc) == "merge_run_not_writable"

    store.register_task_run("run-1", NOW)
    store.register_run(
        "run-1",
        contact_id=CONVERSATION,
        stage="executive",
        now=NOW,
        event_ids=[event.message_id],
    )
    action_id, status = store.enqueue_gitlab_merge(event, **kwargs)
    assert status == "pending"
    duplicate_id, duplicate_status = store.enqueue_gitlab_merge(
        event, **{**kwargs, "assessment": "different wording"}
    )
    assert (duplicate_id, duplicate_status) == (action_id, "pending")
    store.mark_delivery_cancelled(action_id, "merge_rejected", "stopped")
    assert store.enqueue_gitlab_merge(event, **kwargs) == (action_id, "cancelled")
    assert store.connection.execute(
        "SELECT count(*) FROM outgoing WHERE target_kind = 'gitlab_merge'"
    ).fetchone()[0] == 1
    store.close()


def test_ancillary_receipts_do_not_overwrite_final_input_state(tmp_path: Path) -> None:
    store, event, _rule_value, merge_action = _store_with_merge(
        tmp_path / "audit.sqlite3"
    )
    final_action = "final-action"
    assert store.enqueue_delivery(
        final_action,
        event,
        "comment",
        "run-1",
        event_ids=[event.message_id],
    ) == "pending"
    store.mark_delivery_sent(
        merge_action,
        f"gitlab:git.example.com/{PROJECT}!7:merge:{MERGE_SHA}",
    )
    event_row = store.connection.execute(
        "SELECT status, reason FROM events WHERE message_id = ?", (event.message_id,)
    ).fetchone()
    assert (event_row["status"], event_row["reason"]) == ("delivery_pending", "")
    assert store.connection.execute(
        "SELECT delivery_status FROM outgoing WHERE send_uuid = ?", (final_action,)
    ).fetchone()[0] == "pending"

    store.mark_delivery_sent(final_action, "note-1")
    attachment = tmp_path / "report.html"
    attachment.write_text("report", encoding="utf-8")
    assert store.enqueue_attachment(
        "attachment-action",
        event,
        attachment,
        "hash",
        "run-1",
        event_ids=[event.message_id],
    )
    store.mark_delivery_cancelled(
        "attachment-action", "attachment_invalid", "attachment revoked"
    )
    event_row = store.connection.execute(
        "SELECT status, reason FROM events WHERE message_id = ?", (event.message_id,)
    ).fetchone()
    assert (event_row["status"], event_row["reason"]) == ("processed", "sent")
    store.close()


class MergeDispatchTests(IsolatedAsyncioTestCase):
    async def test_pending_merge_submits_once_and_records_scoped_receipt(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as directory:
            store, _event_value, rule, action_id = _store_with_merge(
                Path(directory) / "audit.sqlite3"
            )
            service = _service(store, rule)
            submit = AsyncMock(return_value=_confirmed())
            with patch.dict(os.environ, {"GITLAB_HOST": "git.example.com"}), patch(
                "dws_dm_agent.channels.gitlab_merge.submit_merge", submit
            ):
                self.assertTrue(await service._dispatch_due_delivery())
            row = store.connection.execute(
                "SELECT delivery_status, receipt_status, message_id FROM outgoing WHERE send_uuid = ?",
                (action_id,),
            ).fetchone()
            self.assertEqual((row["delivery_status"], row["receipt_status"]), ("sent", "confirmed"))
            self.assertEqual(
                row["message_id"],
                f"gitlab:git.example.com/{PROJECT}!7:merge:{MERGE_SHA}",
            )
            submit.assert_awaited_once()
            store.close()

    async def test_run_finished_keeps_persisted_merge_intent_dispatchable(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as directory:
            store, _event_value, rule, action_id = _store_with_merge(
                Path(directory) / "audit.sqlite3"
            )
            store.revoke_task_run("run-1", "run_finished", NOW)
            service = _service(store, rule)
            submit = AsyncMock(return_value=_confirmed())
            with patch.dict(os.environ, {"GITLAB_HOST": "git.example.com"}), patch(
                "dws_dm_agent.channels.gitlab_merge.submit_merge", submit
            ):
                self.assertTrue(await service._dispatch_due_delivery())
            row = store.connection.execute(
                "SELECT delivery_status, receipt_status FROM outgoing WHERE send_uuid = ?",
                (action_id,),
            ).fetchone()
            self.assertEqual(
                (row["delivery_status"], row["receipt_status"]),
                ("sent", "confirmed"),
            )
            submit.assert_awaited_once()
            store.close()

    async def test_new_same_conversation_input_cancels_pending_merge_without_submit(
        self,
    ) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as directory:
            store, _event_value, rule, action_id = _store_with_merge(
                Path(directory) / "audit.sqlite3"
            )
            supplement = IncomingEvent(
                event_id="event-2",
                message_id="message-2",
                conversation_id=CONVERSATION,
                contact=Contact("mr", "MR", CONVERSATION, ""),
                content="new review input",
                created_at=NOW + timedelta(minutes=1),
                origin="watch-gitlab",
                watch_authority=rule.authority_key,
                source_revision=HEAD_SHA,
                target_branch="main",
            )
            self.assertTrue(store.claim_event(supplement))
            service = _service(store, rule)
            submit = AsyncMock()
            with patch.dict(os.environ, {"GITLAB_HOST": "git.example.com"}), patch(
                "dws_dm_agent.channels.gitlab_merge.submit_merge", submit
            ):
                self.assertTrue(await service._dispatch_due_delivery())
            row = store.connection.execute(
                "SELECT delivery_status, last_error FROM outgoing WHERE send_uuid = ?",
                (action_id,),
            ).fetchone()
            self.assertEqual(row["delivery_status"], "cancelled")
            self.assertEqual(row["last_error"], "merge_new_input")
            submit.assert_not_awaited()
            store.close()

    async def test_recovered_merge_reconciles_only_and_rejection_stays_uncertain(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as directory:
            store, _event_value, rule, action_id = _store_with_merge(
                Path(directory) / "audit.sqlite3"
            )
            claimed = store.claim_due_delivery(NOW)
            self.assertEqual(claimed["send_uuid"], action_id)
            self.assertEqual(store.recover_outgoing(NOW)["remote_uncertain"], 1)
            service = _service(store, rule)
            submit = AsyncMock()
            reconcile = AsyncMock(side_effect=MergeRejected("target changed"))
            with patch.dict(os.environ, {"GITLAB_HOST": "git.example.com"}), patch(
                "dws_dm_agent.channels.gitlab_merge.submit_merge", submit
            ), patch(
                "dws_dm_agent.channels.gitlab_merge.reconcile_merge", reconcile
            ):
                self.assertTrue(await service._dispatch_due_delivery())
            row = store.connection.execute(
                "SELECT delivery_status, receipt_status, last_error FROM outgoing WHERE send_uuid = ?",
                (action_id,),
            ).fetchone()
            self.assertEqual(
                (row["delivery_status"], row["receipt_status"]),
                ("remote_uncertain", "remote_uncertain"),
            )
            self.assertIn("merge_reconcile_rejected", row["last_error"])
            submit.assert_not_awaited()
            reconcile.assert_awaited_once()
            store.close()

    async def test_revoked_pending_merge_never_calls_remote(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as directory:
            store, _event_value, rule, action_id = _store_with_merge(
                Path(directory) / "audit.sqlite3"
            )
            store.revoke_task_run("run-1", "manual_takeover", NOW)
            service = _service(store, rule)
            submit = AsyncMock()
            with patch.dict(os.environ, {"GITLAB_HOST": "git.example.com"}), patch(
                "dws_dm_agent.channels.gitlab_merge.submit_merge", submit
            ):
                self.assertTrue(await service._dispatch_due_delivery())
            row = store.connection.execute(
                "SELECT delivery_status, last_error FROM outgoing WHERE send_uuid = ?",
                (action_id,),
            ).fetchone()
            self.assertEqual(row["delivery_status"], "cancelled")
            self.assertEqual(row["last_error"], "merge_run_revoked")
            submit.assert_not_awaited()
            store.close()
