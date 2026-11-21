from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock

from dws_dm_agent.core import AuditStore, Contact, IncomingEvent
from dws_dm_agent.service import AgentService


NOW = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)
CONTACT = Contact("user", "User", "user-1", "")


def _event(message_id: str) -> IncomingEvent:
    return IncomingEvent(
        event_id=f"event-{message_id}",
        message_id=message_id,
        conversation_id="conversation-1",
        contact=CONTACT,
        content="question",
        created_at=NOW - timedelta(hours=26),
    )


def _enqueue(store: AuditStore, action_id: str, message_id: str) -> None:
    event = _event(message_id)
    assert store.claim_event(event)
    assert store.enqueue_delivery(
        action_id, event, "reply", f"run-{message_id}", event_ids=[message_id]
    ) == "pending"


class FakeDws:
    streaming_cards = False
    supports_freshness = False
    retry_safe = True
    supports_reconciliation = False

    def __init__(self) -> None:
        self.send = AsyncMock(return_value=("remote-message", {}))


def _service(store: AuditStore, dws: FakeDws) -> AgentService:
    service = AgentService.__new__(AgentService)
    service.store = store
    service.dws = dws
    service.settings = SimpleNamespace(
        timezone=timezone.utc,
        contacts=(CONTACT,),
        cooldown=timedelta(0),
    )
    service._watch_rules = []
    return service


def test_daemon_recovery_respects_dws_24_hour_idempotency_window(
    tmp_path: Path,
) -> None:
    store = AuditStore(tmp_path / "audit.sqlite3", self_id="self")
    _enqueue(store, "within-window", "message-within")
    _enqueue(store, "outside-window", "message-outside")
    _enqueue(store, "never-attempted", "message-pending")
    store.connection.execute(
        """UPDATE outgoing SET delivery_status = 'sending', sent_at = ?
           WHERE send_uuid = 'within-window'""",
        ((NOW - timedelta(hours=23)).isoformat(),),
    )
    store.connection.execute(
        """UPDATE outgoing SET delivery_status = 'sending', sent_at = ?
           WHERE send_uuid = 'outside-window'""",
        ((NOW - timedelta(hours=25)).isoformat(),),
    )
    store.connection.execute(
        "UPDATE outgoing SET sent_at = ? WHERE send_uuid = 'never-attempted'",
        ((NOW - timedelta(days=3)).isoformat(),),
    )
    store.connection.commit()

    assert store.recover_outgoing(NOW) == {
        "retry_wait": 1,
        "remote_uncertain": 1,
    }
    statuses = {
        row["send_uuid"]: row["delivery_status"]
        for row in store.connection.execute(
            "SELECT send_uuid, delivery_status FROM outgoing"
        )
    }
    assert statuses == {
        "within-window": "retry_wait",
        "outside-window": "remote_uncertain",
        "never-attempted": "pending",
    }
    store.close()


class DwsRetryWindowDispatchTests(IsolatedAsyncioTestCase):
    async def test_old_attempt_is_not_resent_but_old_pending_gets_first_send(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as directory:
            store = AuditStore(Path(directory) / "audit.sqlite3", self_id="self")
            _enqueue(store, "old-retry", "message-retry")
            _enqueue(store, "old-pending", "message-pending")
            old = (NOW - timedelta(hours=25)).isoformat()
            store.connection.execute(
                """UPDATE outgoing SET delivery_status = 'retry_wait', attempt_count = 1,
                          sent_at = ?, next_attempt_at = ''
                   WHERE send_uuid = 'old-retry'""",
                (old,),
            )
            store.connection.execute(
                "UPDATE outgoing SET sent_at = ? WHERE send_uuid = 'old-pending'",
                (old,),
            )
            store.connection.commit()
            dws = FakeDws()
            service = _service(store, dws)

            self.assertTrue(await service._dispatch_due_delivery())
            dws.send.assert_not_awaited()
            retry = store.connection.execute(
                "SELECT delivery_status, receipt_status FROM outgoing WHERE send_uuid = 'old-retry'"
            ).fetchone()
            self.assertEqual(
                (retry["delivery_status"], retry["receipt_status"]),
                ("remote_uncertain", "remote_uncertain"),
            )

            self.assertTrue(await service._dispatch_due_delivery())
            dws.send.assert_awaited_once()
            pending = store.connection.execute(
                "SELECT delivery_status, receipt_status FROM outgoing WHERE send_uuid = 'old-pending'"
            ).fetchone()
            self.assertEqual(
                (pending["delivery_status"], pending["receipt_status"]),
                ("sent", "confirmed"),
            )
            store.close()
