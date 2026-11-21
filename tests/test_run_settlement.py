from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

from dws_dm_agent.core import AuditStore, Contact, HistoryMessage, IncomingEvent
from dws_dm_agent.runners.adapters.pi import recover_pi_settlement
from dws_dm_agent.service import AgentService


TZ = ZoneInfo("Asia/Shanghai")
CONTACT = Contact("teammate", "Teammate", "u-1", "open-contact")
NOW = datetime(2026, 11, 16, 17, 55, 31, tzinfo=TZ)


class _SettlingSession:
    def __init__(self, prepared: SimpleNamespace, session_id: str) -> None:
        self.prepared = prepared
        self.session_id = session_id
        self.done = False
        self.silent = False
        self.error = ""
        self.escalated = False
        self.blocked = False
        self.exit_code = 0
        self.closed = False
        self._updates: asyncio.Queue[None] = asyncio.Queue(maxsize=1)

    async def start(self) -> None:
        async def settle() -> None:
            await asyncio.sleep(0.01)
            self.done = True
            self._updates.put_nowait(None)

        asyncio.create_task(settle())

    async def wait_for_update(self, timeout: float | None = None) -> None:
        if timeout is None:
            await self._updates.get()
        else:
            await asyncio.wait_for(self._updates.get(), timeout)

    async def steer(self, prompt: str) -> bool:
        return False

    def result(self) -> str:
        return "已完成"

    async def abort(self) -> None:
        return None

    async def close(self) -> None:
        self.closed = True


class NormalSettlementTests(unittest.TestCase):
    def test_settlement_is_not_blocked_by_progress_card_io(self) -> None:
        async def scenario(root: Path) -> None:
            service = AgentService.__new__(AgentService)
            service.mode = "live"
            service.settings = SimpleNamespace(
                timezone=TZ,
                self_open_id="self-open-id",
                self_name="Operator",
                cooldown=0,
                quiet_window=0.01,
                state_dir=root,
                workspace_root=root,
                worktree_root=root,
                raw={"monitor_interval_seconds": 0.05, "max_replans": 5},
            )
            service.store = AuditStore(root / "audit.sqlite3", self_id="self-open-id")
            service.dws = SimpleNamespace(history=AsyncMock(return_value=[]))
            service.runtime_states = {}
            service.live_sessions = {}
            service.progress_card_seqs = {}
            service._write_runtime = Mock()
            never_returns = asyncio.Event()

            async def block_progress_card(*args) -> None:
                await never_returns.wait()

            service._sync_progress_card = AsyncMock(side_effect=block_progress_card)
            prepared = SimpleNamespace(
                timeout_seconds=30,
                task_context_path=None,
                worktree_path=None,
            )
            holder: dict[str, _SettlingSession] = {}
            service.agent_runtime = SimpleNamespace(
                pipeline=("gate", "worker", "deliver"),
                skill_pool={},
                profile_name=Mock(return_value="pi-worker"),
                render=Mock(return_value="prompt"),
                render_supplement=Mock(return_value="steer prompt"),
                supplement_strategy="steer",
                stage_of_kind=lambda kind: "front" if kind == "triage" else "worker",
                executive_stage=Mock(return_value="worker"),
                _stage=Mock(
                    return_value=SimpleNamespace(
                        name="worker",
                        prompt_path=root / "prompts" / "worker.md",
                        profile=SimpleNamespace(protocol="pi-rpc", options={}),
                    )
                ),
                resolve_stage=Mock(
                    return_value=SimpleNamespace(
                        name="worker",
                        profile=SimpleNamespace(protocol="pi-rpc", options={}),
                    )
                ),
                open_session=Mock(
                    side_effect=lambda stage, session_id, *args, **kwargs: holder.setdefault(
                        session_id, _SettlingSession(prepared, session_id)
                    )
                ),
            )
            batch = [
                IncomingEvent(
                    "event-1",
                    "message-1",
                    "conversation-1",
                    CONTACT,
                    "请检查运行状态",
                    NOW,
                )
            ]
            service.store.claim_event(batch[0])

            result = await asyncio.wait_for(service._run_agent(batch, ""), timeout=0.5)

            self.assertEqual(result.reply, "已完成")
            self.assertTrue(holder[result.session_id].closed)
            service.store.close()

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(Path(directory)))


class StartupSettlementTests(unittest.TestCase):
    def _service(self, root: Path) -> AgentService:
        service = AgentService.__new__(AgentService)
        service.mode = "live"
        service.settings = SimpleNamespace(
            timezone=TZ,
            state_dir=root,
            contacts=[CONTACT],
            self_open_id="self-open-id",
            cooldown=600,
            raw={"max_reply_chars": 16000},
        )
        service.store = AuditStore(root / "audit.sqlite3", self_id="self-open-id")
        service.outbox_wakeup = asyncio.Event()
        service.runtime_states = {}
        service._write_runtime = Mock()
        return service

    def _register_orphan(
        self,
        service: AgentService,
        root: Path,
        session_id: str,
        *,
        legacy: bool = False,
    ) -> Path:
        event = IncomingEvent(
            f"event-{session_id}",
            f"message-{session_id}",
            "conversation-1",
            CONTACT,
            "原始请求",
            NOW,
        )
        service.store.claim_event(event)
        session_dir = root / "sessions" / f"{session_id}-evidence"
        session_dir.mkdir(parents=True)
        if legacy:
            service.store.register_run(
                session_id,
                contact_id=CONTACT.identity,
                stage="worker",
                now=NOW,
            )
            (session_dir / "task-context.json").write_text(
                json.dumps(
                    {
                        "message_times": {event.message_id: NOW.isoformat()},
                        "self_id": "self-open-id",
                        "contact_id": CONTACT.identity,
                        "conversation_id": event.conversation_id,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        else:
            service.store.register_run(
                session_id,
                contact_id=CONTACT.identity,
                stage="worker",
                now=NOW,
                event_ids=[event.message_id],
                session_dir=session_dir,
            )
        return session_dir

    @staticmethod
    def _write_settled_final(session_dir: Path, text: str = "已处理完成") -> None:
        events = [
            {
                "type": "agent_end",
                "messages": [
                    {"role": "assistant", "content": [{"type": "text", "text": text}]}
                ],
            },
            {"type": "agent_settled"},
        ]
        (session_dir / "events.jsonl").write_text(
            "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in events),
            encoding="utf-8",
        )
        (session_dir / "result.txt").write_text(text, encoding="utf-8")

    def test_startup_adopts_authenticated_settled_final_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = self._service(root)
            session_id = "dm-recovered-final"
            session_dir = self._register_orphan(
                service, root, session_id
            )
            self._write_settled_final(session_dir)
            old = (datetime.now(TZ) - timedelta(days=3)).timestamp()
            os.utime(session_dir / "events.jsonl", (old, old))

            service._recover_orphan_runs()
            service._recover_orphan_runs()

            delivery = service.store.connection.execute(
                "SELECT send_uuid, delivery_status, payload_text FROM outgoing "
                "WHERE session_id = ? AND event_ids_json != '[]'",
                (session_id,),
            ).fetchall()
            self.assertEqual(len(delivery), 1)
            self.assertEqual(
                delivery[0]["send_uuid"],
                "e18c1dd1-9c5c-5623-91b6-713325e35e96",
            )
            self.assertEqual(delivery[0]["delivery_status"], "pending")
            self.assertEqual(delivery[0]["payload_text"], "已处理完成")
            event = service.store.connection.execute(
                "SELECT status FROM events WHERE message_id = ?",
                (f"message-{session_id}",),
            ).fetchone()
            self.assertEqual(event["status"], "delivery_pending")
            run = service.store.connection.execute(
                "SELECT status, outcome, event_ids_json, session_dir "
                "FROM board_runs WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            self.assertEqual((run["status"], run["outcome"]), ("done", "final"))
            self.assertEqual(json.loads(run["event_ids_json"]), [f"message-{session_id}"])
            self.assertEqual(run["session_dir"], str(session_dir))
            service.store.close()

    def test_normal_delivery_uses_the_same_logical_action_id(self) -> None:
        async def scenario(root: Path) -> None:
            service = self._service(root)
            service._set_runtime = Mock()
            event = IncomingEvent(
                "event-normal",
                "message-dm-recovered-final",
                "conversation-1",
                CONTACT,
                "原始请求",
                NOW,
            )
            service.store.claim_event(event)

            await service._send_after_freshness_check(
                [event], "已处理完成", session_id="dm-recovered-final"
            )

            row = service.store.connection.execute(
                "SELECT send_uuid FROM outgoing WHERE session_id = ?",
                ("dm-recovered-final",),
            ).fetchone()
            self.assertEqual(
                row["send_uuid"], "e18c1dd1-9c5c-5623-91b6-713325e35e96"
            )
            service.store.close()

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(Path(directory)))

    def test_recovery_respects_manual_takeover_and_nonqueued_input(self) -> None:
        for case in ("manual", "processed", "newer"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                service = self._service(root)
                session_id = f"dm-{case}-final"
                session_dir = self._register_orphan(service, root, session_id)
                self._write_settled_final(session_dir)
                if case == "manual":
                    service.store.record_manual(
                        HistoryMessage(
                            "human-1",
                            "conversation-1",
                            "Operator",
                            "self-open-id",
                            "人工已处理",
                            NOW + timedelta(seconds=1),
                        )
                    )
                else:
                    if case == "processed":
                        service.store.update_event_status(
                            [f"message-{session_id}"], "processed", "already_handled"
                        )
                    else:
                        service.store.claim_event(
                            IncomingEvent(
                                "event-newer",
                                "message-newer",
                                "conversation-1",
                                CONTACT,
                                "稍后的新输入",
                                NOW + timedelta(seconds=2),
                            )
                        )

                service._recover_orphan_runs()

                self.assertEqual(
                    service.store.connection.execute(
                        "SELECT COUNT(*) FROM outgoing WHERE session_id = ?",
                        (session_id,),
                    ).fetchone()[0],
                    0,
                )
                event = service.store.connection.execute(
                    "SELECT status, reason FROM events WHERE message_id = ?",
                    (f"message-{session_id}",),
                ).fetchone()
                if case == "manual":
                    self.assertEqual(
                        (event["status"], event["reason"]),
                        ("skipped", "manual_takeover"),
                    )
                elif case == "processed":
                    self.assertEqual(
                        (event["status"], event["reason"]),
                        ("processed", "already_handled"),
                    )
                else:
                    self.assertEqual(
                        (event["status"], event["reason"]),
                        ("error", "startup_newer_input_manual_review"),
                    )
                service.store.close()

    def test_completed_update_of_another_watch_goal_does_not_block_startup_reply(self) -> None:
        from dws_dm_agent.watches import parse_watches

        with tempfile.TemporaryDirectory() as directory:
            service = self._service(Path(directory))
            self.addCleanup(service.store.close)
            watches = [
                {"source": "jira", "name": "调查", "instruction": "检查原因"},
                {"source": "jira", "name": "回归", "instruction": "验证修复"},
            ]
            service.settings.raw["watches"] = watches
            investigation, regression = parse_watches(watches)
            conversation = "jira:DEMO-2"
            contact = Contact("DEMO-2", "DEMO-2", conversation, "")
            def watch_event(rule, message_id, seconds):
                return IncomingEvent(
                    message_id, message_id, conversation, contact, rule.instruction,
                    NOW + timedelta(seconds=seconds), rule.origin, rule.authority_key,
                )
            original = watch_event(investigation, "original", 0)
            service.store.claim_event(original)
            service.store.register_run("run", contact_id=conversation, stage="worker",
                                       now=NOW, event_ids=[original.message_id])
            run = {"session_id": "run", "origin": "live"}
            for rule, message_id, delay, expected in (
                (regression, "other", 1, ""),
                (investigation, "followup", 2, "startup_newer_input_manual_review"),
            ):
                event = watch_event(rule, message_id, delay)
                service.store.claim_event(event)
                service.store.update_event_status([event.message_id], "processed", "sent")
                self.assertEqual(service._orphan_recovery_block_reason(run, [original]), expected)

    def test_recovery_fails_closed_on_account_mode_and_origin_changes(self) -> None:
        cases = {
            "account": "startup_input_invalid_manual_review",
            "mode": "startup_mode_manual_review",
            "origin": "startup_source_manual_review",
        }
        for case, reason in cases.items():
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                service = self._service(root)
                session_id = f"dm-{case}-final"
                session_dir = self._register_orphan(service, root, session_id)
                self._write_settled_final(session_dir)
                if case == "account":
                    row = service.store.connection.execute(
                        "SELECT input_v1 FROM events WHERE message_id = ?",
                        (f"message-{session_id}",),
                    ).fetchone()
                    payload = json.loads(row["input_v1"])
                    payload["self_id"] = "other-account"
                    service.store.connection.execute(
                        "UPDATE events SET input_v1 = ? WHERE message_id = ?",
                        (json.dumps(payload), f"message-{session_id}"),
                    )
                    service.store.connection.commit()
                elif case == "mode":
                    service.mode = "shadow"
                else:
                    service.store.connection.execute(
                        "UPDATE board_runs SET origin = 'synthetic' WHERE session_id = ?",
                        (session_id,),
                    )
                    service.store.connection.commit()

                service._recover_orphan_runs()

                self.assertEqual(
                    service.store.connection.execute(
                        "SELECT COUNT(*) FROM outgoing WHERE session_id = ?",
                        (session_id,),
                    ).fetchone()[0],
                    0,
                )
                event = service.store.connection.execute(
                    "SELECT status, reason FROM events WHERE message_id = ?",
                    (f"message-{session_id}",),
                ).fetchone()
                self.assertEqual((event["status"], event["reason"]), ("error", reason))
                service.store.close()

    def test_teardown_ack_preserves_settlement_but_new_activity_does_not(self) -> None:
        ack = {"type": "response", "command": "abort", "success": True}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_settled_final(root, "原终稿")
            events = root / "events.jsonl"
            settled = events.read_text(encoding="utf-8")
            for tail, expected in [
                ([ack], ("final", "原终稿")),
                ([ack, ack], ("final", "原终稿")),
                ([{**ack, "success": False}], ("", "")),
                ([{"type": "agent_start"}, ack], ("", "")),
                ([{**ack, "command": "prompt"}, ack], ("", "")),
            ]:
                with self.subTest(tail=tail):
                    events.write_text(
                        settled + "".join(json.dumps(row) + "\n" for row in tail),
                        encoding="utf-8",
                    )
                    self.assertEqual(recover_pi_settlement(events), expected)

    def test_pi_recovery_uses_only_the_last_settled_turn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            events = root / "events.jsonl"
            report = root / "report.json"
            report.write_text(
                json.dumps({"kind": "final", "text": "旧报告"}), encoding="utf-8"
            )
            rows = [
                {"type": "tool_execution_end", "toolName": "report", "isError": False},
                {"type": "agent_settled"},
                {
                    "type": "agent_end",
                    "messages": [
                        {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "新终稿"}],
                        }
                    ],
                },
                {"type": "agent_settled"},
            ]
            events.write_text(
                "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
                encoding="utf-8",
            )
            self.assertEqual(
                recover_pi_settlement(events, report), ("final", "新终稿")
            )

            rows.pop()
            events.write_text(
                "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
                encoding="utf-8",
            )
            self.assertEqual(recover_pi_settlement(events, report), ("", ""))

    def test_execution_backlog_is_bounded_and_contains_no_body(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = AuditStore(root / "audit.sqlite3", self_id="self-open-id")
            queued = IncomingEvent(
                "event-queued", "message-queued", "conversation-1", CONTACT,
                "绝不能出现在 backlog 的正文", NOW,
            )
            manual = IncomingEvent(
                "event-manual", "message-manual", "conversation-1", CONTACT,
                "同样不能展示", NOW + timedelta(seconds=1),
            )
            store.claim_event(queued)
            store.claim_event(manual)
            store.update_event_status(
                [manual.message_id], "error", "startup_settled_manual_review"
            )
            store.register_run(
                "run-owned", contact_id=CONTACT.identity, stage="worker", now=NOW
            )
            store.register_run(
                "run-orphan", contact_id=CONTACT.identity, stage="worker",
                now=NOW + timedelta(seconds=2),
            )

            backlog = store.execution_backlog(
                owned_session_ids={"run-owned"}, limit=3
            )

            self.assertEqual(
                [item["kind"] for item in backlog],
                ["queued", "startup_manual_review", "unowned_run"],
            )
            self.assertTrue(all("content" not in item for item in backlog))
            self.assertTrue(all("request_preview" not in item for item in backlog))
            self.assertTrue(all("payload_text" not in item for item in backlog))
            store.close()

    def test_startup_requeues_even_a_recent_claimed_wake(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = AuditStore(root / "audit.sqlite3", self_id="self-open-id")
            wake_id = store.enqueue_wake(
                CONTACT.identity, "检查事项", due_at=NOW, now=NOW
            )
            self.assertTrue(
                store.claim_wake(wake_id, now=NOW + timedelta(minutes=1))
            )

            self.assertEqual(store.recover_claimed_wakes(), 1)

            row = store.connection.execute(
                "SELECT status, claimed_at FROM wakes WHERE id = ?", (wake_id,)
            ).fetchone()
            self.assertEqual((row["status"], row["claimed_at"]), ("pending", ""))
            store.close()

    def test_result_file_without_authenticated_settlement_is_interrupted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = self._service(root)
            session_id = "dm-result-only"
            session_dir = self._register_orphan(service, root, session_id)
            (session_dir / "events.jsonl").write_text(
                "\n".join(
                    [
                        json.dumps(
                            {"type": "message_update", "text": "排障流文本"},
                            ensure_ascii=False,
                        ),
                        json.dumps({"type": "agent_settled"}),
                    ]
                ),
                encoding="utf-8",
            )
            (session_dir / "result.txt").write_text("排障流文本", encoding="utf-8")

            service._recover_orphan_runs()

            self.assertEqual(
                service.store.connection.execute(
                    "SELECT COUNT(*) FROM outgoing WHERE session_id = ?",
                    (session_id,),
                ).fetchone()[0],
                0,
            )
            event = service.store.connection.execute(
                "SELECT status, reason FROM events WHERE message_id = ?",
                (f"message-{session_id}",),
            ).fetchone()
            self.assertEqual(event["status"], "error")
            self.assertEqual(event["reason"], "startup_interrupted_manual_review")
            run = service.store.connection.execute(
                "SELECT status, outcome FROM board_runs WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            self.assertEqual((run["status"], run["outcome"]), ("aborted", "aborted"))
            service.store.close()

    def test_legacy_settlement_without_original_input_requires_review(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = self._service(root)
            session_id = "dm-old-final"
            session_dir = self._register_orphan(service, root, session_id, legacy=True)
            self._write_settled_final(session_dir)
            service.store.connection.execute(
                "UPDATE events SET input_v1 = NULL WHERE message_id = ?",
                (f"message-{session_id}",),
            )
            service.store.connection.commit()

            service._recover_orphan_runs()

            count = service.store.connection.execute(
                "SELECT COUNT(*) FROM outgoing WHERE session_id = ?",
                (session_id,),
            ).fetchone()[0]
            self.assertEqual(count, 0)
            event = service.store.connection.execute(
                "SELECT status, reason FROM events WHERE message_id = ?",
                (f"message-{session_id}",),
            ).fetchone()
            self.assertEqual(
                (event["status"], event["reason"]),
                ("error", "startup_legacy_input_manual_review"),
            )
            audit = service.store.connection.execute(
                "SELECT action, status FROM runs WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            self.assertEqual((audit["action"], audit["status"]), ("reply", "manual_review"))
            service.store.close()

    def test_cancelled_delivery_is_not_resurrected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = self._service(root)
            session_id = "dm-cancelled-final"
            session_dir = self._register_orphan(service, root, session_id)
            self._write_settled_final(session_dir)
            event = service._run_input_events(session_id, CONTACT.identity)[0]
            service.store.enqueue_delivery(
                "cancelled-send",
                event,
                "已处理完成",
                session_id,
                event_ids=[event.message_id],
            )
            service.store.mark_delivery_cancelled(
                "cancelled-send", "stale_supplement", "new_message_before_delivery"
            )

            service._recover_orphan_runs()

            outgoing = service.store.connection.execute(
                "SELECT delivery_status, payload_text FROM outgoing WHERE session_id = ?",
                (session_id,),
            ).fetchall()
            self.assertEqual(len(outgoing), 1)
            self.assertEqual(
                (outgoing[0]["delivery_status"], outgoing[0]["payload_text"]),
                ("cancelled", ""),
            )
            event_row = service.store.connection.execute(
                "SELECT status, reason FROM events WHERE message_id = ?",
                (event.message_id,),
            ).fetchone()
            self.assertEqual(
                (event_row["status"], event_row["reason"]),
                ("skipped", "new_message_before_delivery"),
            )
            service.store.close()

    def test_existing_pending_and_sent_receipts_are_not_rewritten(self) -> None:
        for delivery_status in ("pending", "sent"):
            with self.subTest(delivery_status=delivery_status), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                service = self._service(root)
                session_id = f"dm-{delivery_status}-receipt"
                session_dir = self._register_orphan(service, root, session_id)
                self._write_settled_final(session_dir)
                event = service._run_input_events(session_id, CONTACT.identity)[0]
                service.store.enqueue_delivery(
                    "existing-action",
                    event,
                    "已处理完成",
                    session_id,
                    event_ids=[event.message_id],
                )
                if delivery_status == "sent":
                    service.store.mark_delivery_sent(
                        "existing-action", message_id="receipt-message-id"
                    )
                before = dict(
                    service.store.connection.execute(
                        "SELECT send_uuid, message_id, delivery_status, receipt_status, "
                        "event_ids_json FROM outgoing WHERE session_id = ?",
                        (session_id,),
                    ).fetchone()
                )

                service._recover_orphan_runs()

                after = dict(
                    service.store.connection.execute(
                        "SELECT send_uuid, message_id, delivery_status, receipt_status, "
                        "event_ids_json FROM outgoing WHERE session_id = ?",
                        (session_id,),
                    ).fetchone()
                )
                self.assertEqual(after, before)
                service.store.close()


if __name__ == "__main__":
    unittest.main()
