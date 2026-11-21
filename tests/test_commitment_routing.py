from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

from dws_dm_agent.core import Contact, IncomingEvent, TaskScope
from dws_dm_agent.runners.task_tools import execute_request
from dws_dm_agent.watches import parse_watches
from tests.test_wake_routing import ALICE, BOB, SELF_OPEN_ID, TZ, _service


class CommitmentRoutingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.service = _service(Path(temp.name))
        self.store = self.service.store
        self.addCleanup(self.store.close)
        self.start = datetime(2026, 11, 16, 10, tzinfo=TZ)
        self.scope = TaskScope(SELF_OPEN_ID, ALICE.identity, "thread-A")

    def record(self, **fields):
        return self.store.record_task(scope=self.scope, evidence_at=self.start, now=self.start, **fields)["task"]

    def test_day_four_wake_uses_original_conversation_once_after_archiving_and_restart(self):
        due = self.start + timedelta(days=3)
        task = self.record(goal="回归实际入口", status="waiting", waiting_on=f"@{due:%Y-%m-%d %H:%M}")
        self.assertEqual(self.store.archive_inactive_tasks(self.start + timedelta(days=2, seconds=1), 2), 1)
        self.store.claim_event(IncomingEvent("other", "other", "newer-thread", ALICE, "无关事项", due))
        self.service._enqueue = Mock()
        self.service._scan_time_wakes(due - timedelta(seconds=1))
        self.assertFalse(self.store.due_wakes(due))
        self.service._scan_time_wakes(due)
        wake, = self.store.due_wakes(due)
        self.store.connection.close()
        self.store.__init__(self.store.path)
        asyncio.run(self.service._dispatch_wake(wake, due))
        event, = self.service._enqueue.call_args.args
        self.assertEqual((event.conversation_id, event.origin), ("thread-A", "wake"))
        self.assertIn(task["id"], event.content)
        self.service._scan_time_wakes(due + timedelta(minutes=1))
        self.assertFalse(self.store.due_wakes(due + timedelta(minutes=1)))

    def test_rescheduled_wait_is_checked_again_before_its_queued_wake_runs(self):
        task = self.record(goal="查接口", status="waiting", waiting_on="@2026-11-16 10:00")
        self.service._scan_time_wakes(self.start)
        wake, = self.store.due_wakes(self.start)
        self.record(task_id=task["id"], expected_version=task["version"], waiting_on="@2026-11-20 10:00")
        self.service._enqueue = Mock()
        asyncio.run(self.service._dispatch_wake(wake, self.start))
        self.service._enqueue.assert_not_called()
        self.assertFalse(self.store.due_wakes(self.start))

    def test_watch_wake_uses_original_rule_and_rechecks_it_on_replay(self):
        raw = {"source": "jira", "name": "查资料", "instruction": "只读调查"}
        self.service.settings.raw["watches"] = [raw]
        rule, = parse_watches([raw])
        conversation = "jira:DEMO-1"
        contact = Contact("DEMO-1", conversation, conversation, "")
        event = IncomingEvent("w", "w", conversation, contact, "排查", self.start, rule.origin, rule.authority_key)
        self.store.claim_event(event)
        self.store.update_event_status(["w"], "processed", "sent")
        self.store.record_task(scope=TaskScope(SELF_OPEN_ID, conversation, conversation), goal="复核记录",
                               status="waiting", waiting_on="@2026-11-16 10:00", evidence_message_ids=["w"],
                               evidence_at=self.start, now=self.start)
        self.service._scan_time_wakes(self.start)
        self.service._enqueue = Mock()
        wake, = self.store.due_wakes(self.start)
        asyncio.run(self.service._dispatch_wake(wake, self.start))
        scheduled, = self.service._enqueue.call_args.args
        self.assertEqual((scheduled.conversation_id, scheduled.watch_authority), (conversation, rule.authority_key))
        self.service._enqueue.reset_mock()
        self.service.settings.raw["watches"] = []
        asyncio.run(self.service._recover_pending_events())
        self.service._enqueue.assert_not_called()

    def test_operator_panel_wake_stays_local_through_delivery_and_recovery(self):
        thread = "panel:demo"
        self.store.record_task(scope=TaskScope(SELF_OPEN_ID, SELF_OPEN_ID, thread), goal="更新内部清单",
                               status="waiting", waiting_on="@2026-11-16 10:00", evidence_at=self.start, now=self.start)
        self.service._scan_time_wakes(self.start)
        self.service._enqueue = Mock()
        wake, = self.store.due_wakes(self.start)
        asyncio.run(self.service._dispatch_wake(wake, self.start))
        event, = self.service._enqueue.call_args.args
        self.service._enqueue.reset_mock()
        asyncio.run(self.service._recover_pending_events())
        self.service._enqueue.assert_called_once_with(event)
        self.assertEqual(asyncio.run(self.service._send_after_freshness_check([event], "内部答复", session_id="r")), "panel")
        self.assertEqual(self.store.delivery_receipts(), [])

    def test_one_message_four_bugs_have_independent_evidence_versions_and_scoped_lookup(self):
        source = IncomingEvent("shared", "shared", "thread-A", ALICE, "登录、导出、预览、排序各有一个问题", self.start)
        self.store.claim_event(source)
        ctx = {"db_path": str(self.store.path), "run_id": "run-one", "self_id": SELF_OPEN_ID,
               "contact_id": ALICE.identity, "conversation_id": "thread-A", "message_times": {"shared": self.start.isoformat()}}
        tasks = []
        for name in ["登录", "导出", "预览", "排序"]:
            request = {"tool": "task_record", "tool_call_id": name, "args": {"goal": name, "evidence_message_ids": ["shared"]}}
            task = execute_request(request, ctx)["task"]
            self.assertEqual(execute_request(request, ctx)["task"]["id"], task["id"])
            self.assertEqual(self.store.task_sources(task["id"]), [{"conversation_id": "thread-A", "message_id": "shared"}])
            tasks.append(task)
        self.assertEqual(len({t["id"] for t in tasks}), 4)
        self.record(task_id=tasks[0]["id"], expected_version=1, status="completed", evidence_summary="登录回归通过")
        foreign = IncomingEvent("extra", "extra", "thread-B", BOB, "仅另一个联系人可见", self.start)
        self.store.claim_event(foreign)
        self.assertTrue(self.store.link_task_source(tasks[1]["id"], "extra", SELF_OPEN_ID))
        self.assertEqual(len(self.store.task_sources(tasks[1]["id"])), 2)
        self.assertEqual([t["version"] for t in self.store.board_tasks(scope=self.scope)], [1, 1, 1])
        other = {**ctx, "contact_id": BOB.identity, "conversation_id": "thread-B", "run_id": "run-two"}
        result = execute_request({"tool": "task_lookup", "args": {"include_done": True}}, other)
        self.assertEqual(result["tasks"], [])
        self.assertEqual(self.store.task_run_ids(tasks[1]["id"]), ["run-one"])
