from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo

from dws_dm_agent.core import AuditStore, Contact, IncomingEvent, TaskScope
from dws_dm_agent.service import AgentService
from dws_dm_agent.watches import WatchRule


TZ = ZoneInfo("Asia/Shanghai")


class OutboxReplayTests(unittest.TestCase):
    def test_legacy_missing_receipts_replay_independently_without_duplication(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = AuditStore(Path(directory) / "audit.sqlite3")
            first_recipient = Contact(
                "first", "First recipient", "user-first", "open-first"
            )
            second_recipient = Contact(
                "second", "Second recipient", "user-second", "open-second"
            )
            first = IncomingEvent(
                "event-first", "message-first", "dm:user-first", first_recipient,
                "first request", datetime(2026, 11, 19, 16, 46, tzinfo=TZ),
            )
            second = IncomingEvent(
                "event-second", "message-second", "dm:user-second", second_recipient,
                "second request", datetime(2026, 11, 19, 17, 13, tzinfo=TZ),
            )
            for action_id, event, content in (
                ("legacy-dws-first", first, "first result"),
                ("legacy-dws-second", second, "second result"),
            ):
                store.claim_event(event)
                store.enqueue_delivery(
                    action_id, event, content, f"session-{action_id}",
                    event_ids=[event.message_id],
                )
            store.connection.execute(
                "UPDATE outgoing SET delivery_status = 'retry_wait', sent_at = ? "
                "WHERE send_uuid = 'legacy-dws-first'",
                (first.created_at.isoformat(),),
            )

            jira = Contact("ZKAI-505", "ZKAI-505", "jira:ZKAI-505", "")
            rule = WatchRule(name="active", source="jira", instruction="处理 issue")
            jira_event = IncomingEvent(
                "event-jira", "message-jira", "jira:ZKAI-505", jira,
                "issue update", datetime(2026, 11, 19, 10, 44, tzinfo=TZ),
                "watch-jira", rule.authority_key,
            )
            store.claim_event(jira_event)
            store.enqueue_delivery(
                "jira-comment", jira_event, "confirmed comment", "watch-session",
                event_ids=[jira_event.message_id],
            )
            store.mark_delivery_sent("jira-comment", "30365")
            store.record_task(
                scope=TaskScope("", "jira:ZKAI-505", "jira:ZKAI-505"),
                goal="ZKAI-505", status="completed",
                evidence_message_ids=[jira_event.message_id],
                evidence_at=jira_event.created_at, now=jira_event.created_at,
            )
            task_id = store.connection.execute("SELECT id FROM tasks").fetchone()[0]
            notify_action = "watch-notify-task-505-1"
            store.enqueue_notification(
                notify_action,
                conversation_id=f"dm:{first_recipient.identity}",
                contact_user_id=first_recipient.identity,
                content="watch completed",
                session_id=task_id,
                now=datetime(2026, 11, 19, 10, 44, tzinfo=TZ),
            )
            store.connection.commit()

            service = AgentService.__new__(AgentService)
            service.mode = "live"
            service.settings = SimpleNamespace(
                timezone=TZ,
                self_open_id="self-open-id",
                cooldown=600,
                contacts=[first_recipient, second_recipient],
            )
            service.store = store
            service.streaming_card_send_uuids = {}
            service._watch_rules = [rule]
            service._history_events = Mock(return_value=[])
            service._enqueue = Mock()

            async def fake_send(_contact, _content, send_uuid):
                if send_uuid == notify_action:
                    raise RuntimeError("dm unavailable")
                return f"remote-{send_uuid}", {}

            service.dws = SimpleNamespace(
                name="dws",
                streaming_cards=False,
                supports_freshness=True,
                retry_safe=True,
                supports_reconciliation=False,
                history=AsyncMock(return_value=[]),
                send=AsyncMock(side_effect=fake_send),
            )

            self.assertEqual(
                store.recover_outgoing(datetime(2026, 11, 19, 18, 0, tzinfo=TZ)),
                {"retry_wait": 1, "remote_uncertain": 0},
            )
            self.assertEqual(
                store.recover_outgoing(datetime(2026, 11, 19, 18, 1, tzinfo=TZ)),
                {"retry_wait": 0, "remote_uncertain": 0},
            )
            class FrozenDateTime(datetime):
                @classmethod
                def now(cls, tz=None):
                    value = datetime(2026, 11, 19, 18, 0, tzinfo=TZ)
                    return value.astimezone(tz) if tz else value.replace(tzinfo=None)

            with patch("dws_dm_agent.service.datetime", FrozenDateTime):
                for _ in range(3):
                    self.assertTrue(asyncio.run(service._dispatch_due_delivery()))

            self.assertEqual(
                store.recover_outgoing(datetime(2026, 11, 19, 18, 2, tzinfo=TZ)),
                {"retry_wait": 0, "remote_uncertain": 0},
            )
            with patch("dws_dm_agent.service.datetime", FrozenDateTime):
                self.assertFalse(asyncio.run(service._dispatch_due_delivery()))
            receipts = {item["action_id"]: item for item in store.delivery_receipts()}
            self.assertEqual(len(receipts), 4)
            self.assertEqual(receipts["jira-comment"]["remote_id"], "30365")
            self.assertEqual(receipts["jira-comment"]["receipt_status"], "confirmed")
            self.assertEqual(receipts["legacy-dws-first"]["receipt_status"], "confirmed")
            self.assertEqual(receipts["legacy-dws-second"]["receipt_status"], "confirmed")
            self.assertEqual(receipts[notify_action]["delivery_status"], "retry_wait")
            self.assertEqual(receipts[notify_action]["receipt_status"], "pending")
            self.assertEqual(service.dws.send.await_count, 3)
            store.close()


if __name__ == "__main__":
    unittest.main()
