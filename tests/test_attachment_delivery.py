from __future__ import annotations

import asyncio
import hashlib
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

from dws_dm_agent.core import AuditStore, Contact, IncomingEvent
from dws_dm_agent.service import (
    AgentService,
    AttachmentValidationError,
    _delivery_attachment_action_id,
    _delivery_directory,
    _discover_delivery_attachments,
    _validate_delivery_attachment,
)


TZ = ZoneInfo("Asia/Shanghai")
CONTACT = Contact("me", "Me", "user-1", "open-1")


class AttachmentDeliveryTests(unittest.TestCase):
    def _event(self) -> IncomingEvent:
        return IncomingEvent(
            "event-1",
            "message-1",
            "dm:user-1",
            CONTACT,
            "请给我一份 HTML 说明",
            datetime(2026, 11, 19, 10, 0, tzinfo=TZ),
        )

    def _service(self, root: Path) -> AgentService:
        service = AgentService.__new__(AgentService)
        service.mode = "live"
        service.settings = SimpleNamespace(
            timezone=TZ,
            contacts=[CONTACT],
            state_dir=root,
            raw={},
        )
        service.store = AuditStore(root / "audit.sqlite3")
        service.dws = SimpleNamespace(
            streaming_cards=False,
            supports_freshness=True,
            retry_safe=True,
            history=AsyncMock(return_value=[]),
            send=AsyncMock(return_value=("body-message", {})),
            send_file=AsyncMock(return_value=("file-message", {})),
        )
        service.streaming_card_send_uuids = {}
        service._history_events = Mock(return_value=[])
        service._register_manual_messages = Mock(return_value=None)
        service._human_owns_conversation = Mock(return_value=False)
        service.outbox_wakeup = asyncio.Event()
        return service

    def test_delivery_directory_is_session_scoped_and_rejects_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            path = _delivery_directory(root, "dm-session-1", create=True)

            self.assertEqual(
                path, (root / "deliveries" / "dm-session-1").resolve()
            )
            self.assertTrue(path.is_dir())
            with self.assertRaises(AttachmentValidationError):
                _delivery_directory(root, "../other-session", create=True)

    def test_attachment_action_is_stable_and_bound_to_recipient(self) -> None:
        event = self._event()

        first = _delivery_attachment_action_id(
            event,
            "report.html",
            "a" * 64,
            event_ids=[event.message_id],
        )
        replay = _delivery_attachment_action_id(
            event,
            "report.html",
            "a" * 64,
            event_ids=[event.message_id],
        )
        other = _delivery_attachment_action_id(
            IncomingEvent(
                event.event_id,
                event.message_id,
                "dm:user-2",
                Contact("other", "Other", "user-2", "open-2"),
                event.content,
                event.created_at,
            ),
            "report.html",
            "a" * 64,
            event_ids=[event.message_id],
        )
        supplemented = _delivery_attachment_action_id(
            event,
            "report.html",
            "a" * 64,
            event_ids=[event.message_id, "message-2"],
        )

        self.assertEqual(first, replay)
        self.assertNotEqual(first, other)
        self.assertNotEqual(first, supplemented)

    def test_body_and_attachment_have_independent_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = self._service(root)
            event = self._event()
            service.store.claim_event(event)
            delivery_dir = _delivery_directory(root, "dm-session-1", create=True)
            attachment = delivery_dir / "report.html"
            attachment.write_text("<h1>result</h1>", encoding="utf-8")
            service.store.enqueue_delivery(
                "body-action",
                event,
                "正文",
                "dm-session-1",
                event_ids=[event.message_id],
            )

            queued = service._enqueue_delivery_attachments(
                [event], "dm-session-1"
            )
            replayed = service._enqueue_delivery_attachments(
                [event], "dm-session-1"
            )
            replay_dir = _delivery_directory(root, "dm-session-2", create=True)
            (replay_dir / "report.html").write_text(
                "<h1>result</h1>", encoding="utf-8"
            )
            replayed_in_new_session = service._enqueue_delivery_attachments(
                [event], "dm-session-2"
            )

            self.assertEqual(queued, 1)
            self.assertEqual(replayed, 0)
            self.assertEqual(replayed_in_new_session, 0)
            self.assertEqual(
                [item["target_kind"] for item in service.store.delivery_receipts()],
                ["reply", "attachment"],
            )
            asyncio.run(service._dispatch_due_delivery())
            asyncio.run(service._dispatch_due_delivery())
            receipts = service.store.delivery_receipts()
            self.assertEqual(
                [item["receipt_status"] for item in receipts],
                ["confirmed", "confirmed"],
            )
            service.dws.send.assert_awaited_once()
            service.dws.send_file.assert_awaited_once()
            self.assertEqual(service.dws.send_file.call_args.args[1], attachment)
            service.store.close()

    def test_attachment_enqueue_failure_does_not_block_body(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = self._service(root)
            event = self._event()
            service.store.claim_event(event)
            delivery_dir = _delivery_directory(root, "dm-session-1", create=True)
            (delivery_dir / "report.html").write_text("result", encoding="utf-8")
            service.store.enqueue_attachment = Mock(
                side_effect=RuntimeError("attachment store unavailable")
            )
            service._set_runtime = Mock()

            outcome = asyncio.run(
                service._send_after_freshness_check(
                    [event], "正文", session_id="dm-session-1"
                )
            )

            self.assertEqual(outcome, "delivery_pending")
            receipts = service.store.delivery_receipts()
            self.assertEqual(len(receipts), 1)
            self.assertEqual(receipts[0]["target_kind"], "reply")
            service.store.close()

    def test_changed_file_is_cancelled_without_blocking_body(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = self._service(root)
            event = self._event()
            service.store.claim_event(event)
            delivery_dir = _delivery_directory(root, "dm-session-1", create=True)
            attachment = delivery_dir / "report.html"
            attachment.write_text("original", encoding="utf-8")
            service.store.enqueue_delivery(
                "body-action",
                event,
                "正文",
                "dm-session-1",
                event_ids=[event.message_id],
            )
            self.assertEqual(
                service._enqueue_delivery_attachments([event], "dm-session-1"), 1
            )
            attachment.write_text("changed", encoding="utf-8")

            asyncio.run(service._dispatch_due_delivery())
            asyncio.run(service._dispatch_due_delivery())

            receipts = service.store.delivery_receipts()
            self.assertEqual(receipts[0]["receipt_status"], "confirmed")
            self.assertEqual(receipts[1]["receipt_status"], "cancelled")
            self.assertIn("attachment_sha256_changed", receipts[1]["last_error"])
            service.dws.send.assert_awaited_once()
            service.dws.send_file.assert_not_awaited()
            service.store.close()

    def test_manual_takeover_revokes_attachment_before_send(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = self._service(root)
            event = self._event()
            service.store.claim_event(event)
            delivery_dir = _delivery_directory(root, "dm-session-1", create=True)
            (delivery_dir / "report.html").write_text("result", encoding="utf-8")
            self.assertEqual(
                service._enqueue_delivery_attachments([event], "dm-session-1"), 1
            )
            service._human_owns_conversation = Mock(return_value=True)

            asyncio.run(service._dispatch_due_delivery())

            receipt = service.store.delivery_receipts()[0]
            self.assertEqual(receipt["receipt_status"], "cancelled")
            self.assertEqual(receipt["last_error"], "manual_takeover")
            service.dws.send_file.assert_not_awaited()
            service.store.close()

    def test_path_escape_and_symlink_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            delivery_dir = _delivery_directory(root, "dm-session-1", create=True)
            outside = root / "outside.html"
            outside.write_text("secret", encoding="utf-8")
            link = delivery_dir / "report.html"
            link.symlink_to(outside)
            digest = hashlib.sha256(outside.read_bytes()).hexdigest()

            with self.assertRaisesRegex(
                AttachmentValidationError, "attachment_symlink_not_allowed"
            ):
                _validate_delivery_attachment(
                    root, "dm-session-1", link, digest
                )
            with self.assertRaisesRegex(
                AttachmentValidationError, "attachment_path_outside_session"
            ):
                _validate_delivery_attachment(
                    root, "dm-session-1", outside, digest
                )

    def test_user_requested_formats_and_multiple_files_survive_staging(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            delivery_dir = _delivery_directory(root, "dm-session-1", create=True)
            names = {"flow.mmd", "report.html", "notes.txt", "metrics.csv", "slides.pdf"}
            for name in names:
                (delivery_dir / name).write_bytes(b"requested attachment")
            (delivery_dir / ".draft").write_bytes(b"draft")
            (delivery_dir / "outside").symlink_to(root / "audit.sqlite3")
            found = _discover_delivery_attachments(root, "dm-session-1")
            self.assertEqual({item.path.name for item in found}, names)
            for item in found:
                self.assertEqual(
                    _validate_delivery_attachment(root, "dm-session-1", item.path, item.sha256),
                    item.path,
                )

    def test_prompt_forbids_worker_dws_send_and_uses_service_outbox(self) -> None:
        prompt = (
            Path(__file__).parents[1] / "prompts" / "shared.md"
        ).read_text(encoding="utf-8")
        attachments = prompt.split("<attachments>", 1)[1].split(
            "</attachments>", 1
        )[0]

        self.assertNotIn("dws chat message send", attachments)
        self.assertIn("服务", attachments)
        self.assertIn("{{deliveries_dir}}", attachments)


if __name__ == "__main__":
    unittest.main()
