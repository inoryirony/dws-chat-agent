"""黑板核心（§18 W1）：tasks 扩列、facts、board_runs、progress 与工具边界。

覆盖四件事：旧库迁移（新列/新表就位且旧行不丢）、facts 锚定与证据共用
同一覆盖集校验、expected_version 乐观并发在扩列后仍然成立、
runs/progress 的登记-心跳-收口生命周期。
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dws_dm_agent.core import AuditStore, Contact, HistoryMessage, TaskScope
from dws_dm_agent.runners import task_tools

TZ = timezone(timedelta(hours=8))
CONTACT = Contact(
    alias="alice",
    display_name="Alice",
    user_id="user-alice",
    open_dingtalk_id="open-alice",
)
NOW = datetime(2026, 11, 6, 9, 0, tzinfo=TZ)


class BoardStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "audit.sqlite3"
        self.store = AuditStore(self.db_path)
        self.scope = TaskScope("self-open", CONTACT.identity, "conversation-1")

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def _record(self, **overrides):
        values = dict(
            scope=self.scope,
            goal="等部署后回归验证",
            status="open",
            evidence_summary="对方提出验证请求",
            evidence_message_ids=["message-1"],
            evidence_at=NOW,
            now=NOW,
        )
        values.update(overrides)
        return self.store.record_task(**values)


class BoardMigrationTests(BoardStoreTests):
    def test_legacy_schema_gains_board_tables_and_columns(self) -> None:
        self.store.close()
        legacy = Path(self.temp.name) / "legacy.sqlite3"
        connection = sqlite3.connect(legacy)
        connection.executescript(
            """
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY,
                scope_self TEXT NOT NULL,
                scope_contact TEXT NOT NULL,
                scope_conversation TEXT NOT NULL,
                goal TEXT NOT NULL,
                status TEXT NOT NULL,
                waiting_on TEXT NOT NULL DEFAULT '',
                evidence_summary TEXT NOT NULL DEFAULT '',
                evidence_message_ids TEXT NOT NULL DEFAULT '[]',
                last_evidence_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                archived_at TEXT NOT NULL DEFAULT '',
                archive_reason TEXT NOT NULL DEFAULT '',
                version INTEGER NOT NULL DEFAULT 1,
                run_id TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE outgoing (
                send_uuid TEXT PRIMARY KEY,
                message_id TEXT UNIQUE,
                conversation_id TEXT NOT NULL,
                contact_user_id TEXT NOT NULL,
                sent_at TEXT NOT NULL,
                content_sha256 TEXT NOT NULL,
                session_id TEXT NOT NULL,
                card_biz_id TEXT,
                card_flow_status INTEGER,
                payload_text TEXT NOT NULL DEFAULT '',
                delivery_status TEXT NOT NULL DEFAULT 'sent',
                attempt_count INTEGER NOT NULL DEFAULT 0,
                next_attempt_at TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT '',
                event_ids_json TEXT NOT NULL DEFAULT '[]',
                source_received_at TEXT NOT NULL DEFAULT ''
            );
            INSERT INTO tasks VALUES (
                'task-legacy', 'self-open', 'user-alice', 'conversation-1',
                '旧事项', 'open', '', '旧证据', '["m-old"]',
                '2026-10-01T09:00:00+08:00', '2026-10-01T09:00:00+08:00',
                '2026-10-01T09:00:00+08:00', '', '', 3, ''
            );
            """
        )
        connection.commit()
        connection.close()

        migrated = AuditStore(legacy)
        try:
            tables = {
                row[0]
                for row in migrated.connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            self.assertIn("facts", tables)
            self.assertIn("board_runs", tables)
            self.assertIn("progress", tables)

            outgoing_columns = {
                row["name"]
                for row in migrated.connection.execute("PRAGMA table_info(outgoing)")
            }
            tasks_columns = {
                row["name"]
                for row in migrated.connection.execute("PRAGMA table_info(tasks)")
            }
            self.assertIn("source", outgoing_columns)
            self.assertIn("resources", tasks_columns)
            self.assertIn("contact_ids", tasks_columns)

            legacy_row = migrated.connection.execute(
                "SELECT goal, version, resources, contact_ids FROM tasks "
                "WHERE id = 'task-legacy'"
            ).fetchone()
            self.assertEqual(legacy_row["goal"], "旧事项")
            self.assertEqual(legacy_row["version"], 3)
            self.assertEqual(json.loads(legacy_row["resources"]), [])
            self.assertEqual(json.loads(legacy_row["contact_ids"]), [])

            # 旧行经扩列后可继续走乐观版本锁更新。
            updated = migrated.record_task(
                scope=self.scope,
                task_id="task-legacy",
                status="waiting",
                waiting_on="对方周四部署",
                expected_version=3,
                evidence_at=NOW,
                now=NOW,
            )
            self.assertTrue(updated["ok"])
            self.assertEqual(updated["task"]["version"], 4)
            self.assertEqual(updated["task"]["contact_ids"], [CONTACT.identity])
        finally:
            migrated.close()

    def test_record_outgoing_defaults_source_to_agent(self) -> None:
        self.store.record_outgoing(
            "uuid-1", None, "conversation-1", CONTACT.identity,
            NOW, "回复正文", "session-1",
        )
        source = self.store.connection.execute(
            "SELECT source FROM outgoing WHERE send_uuid = 'uuid-1'"
        ).fetchone()["source"]
        self.assertEqual(source, "agent")


class FactAnchorTests(BoardStoreTests):
    def setUp(self) -> None:
        super().setUp()
        self.context = {
            "db_path": str(self.db_path),
            "self_id": "self-open",
            "contact_id": CONTACT.identity,
            "conversation_id": "conversation-1",
            "run_id": "run-1",
            "message_times": {
                "message-1": "2026-11-06T09:00:00+08:00",
                "message-2": "2026-11-06T09:05:00+08:00",
            },
        }

    def test_fact_with_covered_anchor_is_persisted(self) -> None:
        result = task_tools.execute_request(
            {
                "tool": "task_record",
                "tool_call_id": "tool-1",
                "args": {
                    "goal": "整理本月部署清单",
                    "facts": [
                        {
                            "text": "对方确认周四 14:00 部署测试环境",
                            "anchor_message_id": "message-2",
                        }
                    ],
                },
            },
            self.context,
        )
        self.assertTrue(result["ok"])
        task_id = result["task"]["id"]
        facts = self.store.facts_for_task(task_id)
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["text"], "对方确认周四 14:00 部署测试环境")
        self.assertEqual(facts[0]["anchor_message_id"], "message-2")
        self.assertEqual(facts[0]["source"], "agent")

    def test_fact_with_unknown_anchor_is_rejected_wholesale(self) -> None:
        result = task_tools.execute_request(
            {
                "tool": "task_record",
                "tool_call_id": "tool-1",
                "args": {
                    "goal": "整理本月部署清单",
                    "facts": [
                        {"text": "锚到未覆盖消息的事实", "anchor_message_id": "message-x"}
                    ],
                },
            },
            self.context,
        )
        self.assertFalse(result["ok"])
        self.assertIn("unknown_evidence_message_id", result["error"])
        # 校验失败整单拒绝：不建事项，也不落任何事实。
        self.assertEqual(
            self.store.connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
            0,
        )
        self.assertEqual(
            self.store.connection.execute("SELECT COUNT(*) FROM facts").fetchone()[0],
            0,
        )

    def test_expected_version_conflict_keeps_latest_state(self) -> None:
        created = self._record()
        task_id = created["task"]["id"]
        self.store.record_task(
            scope=self.scope, task_id=task_id, status="waiting",
            waiting_on="对方部署", expected_version=1,
            evidence_at=NOW, now=NOW,
        )
        conflict = self.store.record_task(
            scope=self.scope, task_id=task_id, status="completed",
            expected_version=1, evidence_at=NOW, now=NOW,
        )
        self.assertFalse(conflict["ok"])
        self.assertEqual(conflict["error"], "version_conflict")
        self.assertEqual(conflict["task"]["version"], 2)
        self.assertEqual(conflict["task"]["status"], "waiting")

    def test_board_tasks_cross_contact_with_recent_facts(self) -> None:
        created = self._record()
        task_id = created["task"]["id"]
        other_scope = TaskScope("self-open", "user-bob", "conversation-2")
        self.store.record_task(
            scope=other_scope, goal="Bob 的独立事项",
            evidence_at=NOW, now=NOW,
        )
        for index in range(4):
            self.store.add_fact(
                task_id, f"第 {index} 条事实", anchor_message_id="message-1",
                now=NOW + timedelta(minutes=index),
            )

        board = self.store.board_tasks()
        self.assertEqual(
            {task["goal"] for task in board},
            {"等部署后回归验证", "Bob 的独立事项"},
        )
        mine = next(task for task in board if task["id"] == task_id)
        self.assertEqual(
            [fact["text"] for fact in mine["recent_facts"]],
            ["第 1 条事实", "第 2 条事实", "第 3 条事实"],
        )

        only_bob = self.store.board_tasks(contact_id="user-bob")
        self.assertEqual([task["goal"] for task in only_bob], ["Bob 的独立事项"])

        self.store.record_task(
            scope=self.scope, task_id=task_id, status="completed",
            expected_version=1, evidence_at=NOW, now=NOW,
        )
        self.assertNotIn(
            task_id, {task["id"] for task in self.store.board_tasks()}
        )
        self.assertIn(
            task_id,
            {task["id"] for task in self.store.board_tasks(include_done=True)},
        )

    def test_task_lookup_reads_the_whole_board(self) -> None:
        self._record()
        self.store.record_task(
            scope=TaskScope("self-open", "user-bob", "conversation-2"),
            goal="Bob 的独立事项", evidence_at=NOW, now=NOW,
        )
        result = task_tools.execute_request(
            {"tool": "task_lookup", "tool_call_id": "tool-2", "args": {}},
            {
                **self.context,
                "contact_id": CONTACT.identity,
            },
        )
        self.assertTrue(result["ok"])
        self.assertEqual(
            {task["goal"] for task in result["tasks"]},
            {"等部署后回归验证", "Bob 的独立事项"},
        )

        filtered = task_tools.execute_request(
            {
                "tool": "task_lookup",
                "tool_call_id": "tool-3",
                "args": {"contact": "user-bob"},
            },
            self.context,
        )
        self.assertEqual(
            [task["goal"] for task in filtered["tasks"]], ["Bob 的独立事项"]
        )


class RunProgressLifecycleTests(BoardStoreTests):
    def test_register_touch_progress_close_lifecycle(self) -> None:
        self.store.register_run(
            "session-1", contact_id=CONTACT.identity, stage="worker",
            headline="请求摘要占位", now=NOW,
        )
        self.assertEqual([run["session_id"] for run in self.store.active_runs()], ["session-1"])

        seq = self.store.add_progress("session-1", "定位到回归脚本", now=NOW)
        self.assertEqual(seq, 1)
        self.store.touch_run(
            "session-1", headline="定位到回归脚本", now=NOW + timedelta(seconds=5)
        )
        run = self.store.active_runs()[0]
        self.assertEqual(run["headline"], "定位到回归脚本")
        self.assertEqual(run["status"], "running")
        self.assertIsNone(run["ended_at"])

        self.assertEqual(
            self.store.add_progress("session-1", "跑通全量回归", now=NOW + timedelta(seconds=10)),
            2,
        )
        self.assertEqual(
            [entry["text"] for entry in self.store.recent_progress("session-1")],
            ["定位到回归脚本", "跑通全量回归"],
        )

        self.store.close_run("session-1", "final", now=NOW + timedelta(seconds=30))
        run = dict(
            self.store.connection.execute(
                "SELECT * FROM board_runs WHERE session_id = 'session-1'"
            ).fetchone()
        )
        self.assertEqual(run["status"], "done")
        self.assertEqual(run["outcome"], "final")
        self.assertIsNotNone(run["ended_at"])
        self.assertEqual(self.store.active_runs(), [])

        # 首次收口生效：迟到的二次 close 不改写已记结果。
        self.store.close_run("session-1", "error", now=NOW + timedelta(seconds=60))
        after = self.store.connection.execute(
            "SELECT status, outcome FROM board_runs WHERE session_id = 'session-1'"
        ).fetchone()
        self.assertEqual(after["status"], "done")
        self.assertEqual(after["outcome"], "final")

        # 已收口的运行不再接受心跳。
        self.store.touch_run("session-1", headline="迟到的心跳", now=NOW + timedelta(seconds=90))
        after = self.store.connection.execute(
            "SELECT headline FROM board_runs WHERE session_id = 'session-1'"
        ).fetchone()
        self.assertEqual(after["headline"], "定位到回归脚本")

    def test_blocked_outcome_keeps_run_waiting_and_alive(self) -> None:
        self.store.register_run(
            "session-2", contact_id=CONTACT.identity, stage="worker", now=NOW
        )
        self.store.close_run("session-2", "blocked", now=NOW + timedelta(minutes=5))
        run = dict(
            self.store.connection.execute(
                "SELECT status, outcome, ended_at FROM board_runs WHERE session_id = 'session-2'"
            ).fetchone()
        )
        self.assertEqual(run["status"], "waiting")
        self.assertEqual(run["outcome"], "blocked")
        self.assertIsNotNone(run["ended_at"])
        # blocked 已收口：active_runs 只看未收口的运行。
        self.assertEqual(self.store.active_runs(), [])

    def test_abort_stale_runs_sweeps_only_open_lifecycle(self) -> None:
        self.store.register_run(
            "session-live", contact_id=CONTACT.identity, stage="worker", now=NOW
        )
        self.store.register_run(
            "session-closed", contact_id=CONTACT.identity, stage="front", now=NOW
        )
        self.store.close_run("session-closed", "final", now=NOW)

        swept = self.store.abort_stale_runs(NOW + timedelta(hours=1))

        # 返回被扫尾的运行行：调用方据此回收崩溃遗留的 worktree。
        self.assertEqual(
            [row["session_id"] for row in swept], ["session-live"]
        )
        statuses = {
            row["session_id"]: (row["status"], row["outcome"])
            for row in self.store.connection.execute(
                "SELECT session_id, status, outcome FROM board_runs"
            )
        }
        self.assertEqual(statuses["session-live"], ("aborted", "aborted"))
        self.assertEqual(statuses["session-closed"], ("done", "final"))

    def test_report_progress_backend_updates_run_and_progress(self) -> None:
        self.store.register_run(
            "run-report", contact_id=CONTACT.identity, stage="worker", now=NOW
        )
        result = task_tools.execute_request(
            {
                "tool": "report",
                "tool_call_id": "tool-report-1",
                "args": {"kind": "progress", "text": "第一里程碑：复现了问题"},
            },
            {
                "db_path": str(self.db_path),
                "self_id": "self-open",
                "contact_id": CONTACT.identity,
                "conversation_id": "conversation-1",
                "run_id": "run-report",
                "message_times": {"message-1": "2026-11-06T09:00:00+08:00"},
            },
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["seq"], 1)
        run = self.store.active_runs()[0]
        self.assertEqual(run["headline"], "第一里程碑：复现了问题")
        self.assertEqual(
            [entry["text"] for entry in self.store.recent_progress("run-report")],
            ["第一里程碑：复现了问题"],
        )

        # final/blocked 不经后端：后端收到必须拒绝，终止由扩展/runtime 负责。
        rejected = task_tools.execute_request(
            {
                "tool": "report",
                "tool_call_id": "tool-report-2",
                "args": {"kind": "final", "text": "最终正文"},
            },
            {
                "db_path": str(self.db_path),
                "self_id": "self-open",
                "contact_id": CONTACT.identity,
                "conversation_id": "conversation-1",
                "run_id": "run-report",
                "message_times": {"message-1": "2026-11-06T09:00:00+08:00"},
            },
        )
        self.assertFalse(rejected["ok"])


class MessageSourceTests(BoardStoreTests):
    def _message(self, sender: str, text: str, message_id: str = "") -> HistoryMessage:
        return HistoryMessage(
            message_id=message_id,
            conversation_id="conversation-1",
            sender=sender,
            sender_open_dingtalk_id=sender,
            content=text,
            created_at=NOW,
        )

    def test_agent_human_contact_are_classified_explicitly(self) -> None:
        self.store.record_outgoing(
            "uuid-src", "msg-agent", "conversation-1", CONTACT.identity,
            NOW, "机器人回复", "session-1",
        )
        agent = self._message("self-open", "机器人回复", "msg-agent")
        self.assertEqual(
            self.store.message_source(
                agent, self_open_id="self-open", contact_open_id="open-alice"
            ),
            "agent",
        )

        human = self._message("self-open", "人工手打的回复", "msg-human")
        self.assertEqual(
            self.store.message_source(
                human, self_open_id="self-open", contact_open_id="open-alice"
            ),
            "human",
        )

        contact = self._message("open-alice", "对方发来的问题", "msg-contact")
        self.assertEqual(
            self.store.message_source(
                contact, self_open_id="self-open", contact_open_id="open-alice"
            ),
            "contact",
        )

class FactDedupTests(FactAnchorTests):
    def test_resubmitting_identical_fact_does_not_extend_lifetime(self) -> None:
        created = task_tools.execute_request(
            {
                "tool": "task_record",
                "tool_call_id": "tool-1",
                "args": {
                    "goal": "去重验证",
                    "facts": [
                        {"text": "同一条事实", "anchor_message_id": "message-1"}
                    ],
                },
            },
            self.context,
        )
        self.assertTrue(created["ok"])
        task_id = created["task"]["id"]
        first = self.store._load_task(self.scope, task_id)
        self.assertEqual(first["version"], 1)

        again = task_tools.execute_request(
            {
                "tool": "task_record",
                "tool_call_id": "tool-2",
                "args": {
                    "task_id": task_id,
                    "expected_version": 1,
                    "facts": [
                        {"text": "同一条事实", "anchor_message_id": "message-1"}
                    ],
                },
            },
            self.context,
        )
        self.assertTrue(again["ok"])
        # 重复 facts 不算变更：版本不动、facts 不重复插入。
        self.assertEqual(again["task"]["version"], 1)
        rows = self.store.connection.execute(
            "SELECT COUNT(*) FROM facts WHERE task_id = ?", (task_id,)
        ).fetchone()[0]
        self.assertEqual(rows, 1)


class ArchiveVisibilityTests(FactAnchorTests):
    def test_archived_task_is_visible_only_with_include_archived(self) -> None:
        created = self._record()
        task_id = created["task"]["id"]
        self.store.connection.execute(
            "UPDATE tasks SET archived_at = ?, archive_reason = 'inactive' "
            "WHERE id = ?",
            (NOW.isoformat(), task_id),
        )
        self.store.connection.commit()

        default = self.store.board_tasks()
        self.assertNotIn(task_id, {task["id"] for task in default})

        archived = self.store.board_tasks(include_archived=True)
        self.assertIn(task_id, {task["id"] for task in archived})

        via_tool = task_tools.execute_request(
            {
                "tool": "task_lookup",
                "tool_call_id": "tool-archived",
                "args": {"include_archived": True},
            },
            self.context,
        )
        self.assertTrue(via_tool["ok"])
        self.assertIn(task_id, {task["id"] for task in via_tool["tasks"]})


class TruncationReportTests(FactAnchorTests):
    def test_over_limit_facts_report_what_was_dropped(self) -> None:
        facts = [
            {"text": f"事实 {index}", "anchor_message_id": "message-1"}
            for index in range(10)
        ]
        result = task_tools.execute_request(
            {
                "tool": "task_record",
                "tool_call_id": "tool-trunc",
                "args": {"goal": "截断验证", "facts": facts},
            },
            self.context,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["truncated"], {"facts": 2})



if __name__ == "__main__":
    unittest.main()
