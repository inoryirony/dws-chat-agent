from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from dws_dm_agent.core import AuditStore, IncomingEvent
from dws_dm_agent.sources.base import SourceItem
from dws_dm_agent.watches import parse_watches, rule_for_item


def _item(key: str = "ZKAI-401", source: str = "jira") -> SourceItem:
    from dws_dm_agent.core import Contact

    return SourceItem(
        dedup_key=f"{source}:{key}",
        conversation_id=f"{source}:{key}",
        contact=Contact(
            alias=key,
            display_name=f"{source}:{key}",
            user_id=f"{source}:{key}",
            open_dingtalk_id="",
        ),
        title="测试命中",
        body="正文",
        url="https://example/browse/ZKAI-401",
        created_at=datetime(2026, 11, 8, 10, 0, tzinfo=None),
        meta={"source": source, "key": key},
    )


class ParseWatchesTests(unittest.TestCase):
    def test_empty_and_missing_watches_mean_none(self) -> None:
        self.assertEqual(parse_watches(None), [])
        self.assertEqual(parse_watches([]), [])

    def test_parse_valid_rules(self) -> None:
        rules = parse_watches(
            [
                {
                    "source": "jira",
                    "name": "新 issue",
                    "jql": "assignee = currentUser()",
                    "projects": ["ZKAI"],
                    "instruction": "分析它。",
                },
                {"source": "gitlab", "projects": ["a/b"], "instruction": "审 MR。"},
            ]
        )
        self.assertEqual(len(rules), 2)
        self.assertEqual(rules[0].origin, "watch-jira")
        self.assertEqual(rules[1].name, "watch-gitlab")
        self.assertEqual(rules[0].projects, ("ZKAI",))

    def test_rule_without_instruction_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "instruction"):
            parse_watches([{"source": "jira", "jql": "x"}])

    def test_rule_for_item_matches_by_source_and_project(self) -> None:
        rules = parse_watches(
            [
                {
                    "source": "jira",
                    "name": "ZKAI 项目",
                    "projects": ["ZKAI"],
                    "instruction": "i",
                }
            ]
        )
        self.assertIsNotNone(rule_for_item(rules, _item("ZKAI-401")))
        self.assertIsNone(rule_for_item(rules, _item("OTHER-9")))
        self.assertIsNone(rule_for_item(rules, _item("1", source="gitlab")))

    def test_unknown_source_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown source 'sms'"):
            parse_watches([{"source": "sms", "instruction": "i"}])


class ChannelRegistryTests(unittest.TestCase):
    """前缀 → 通道：未知前缀必须炸；dws 默认由恒等工厂显式表达。"""

    def _dws(self):
        return SimpleNamespace()

    def test_unknown_prefix_raises(self) -> None:
        from dws_dm_agent.channels import channel_for

        with self.assertRaisesRegex(ValueError, "unregistered channel prefix"):
            channel_for("http://evil", self._dws())

    def test_dm_and_empty_prefix_resolve_to_dws(self) -> None:
        from dws_dm_agent.channels import channel_for

        dws = self._dws()
        self.assertIs(channel_for("dm:alice", dws), dws)
        self.assertIs(channel_for("", dws), dws)
        self.assertIs(channel_for("panel:x", dws), dws)

    def test_external_channels_never_stream_cards(self) -> None:
        from dws_dm_agent.channels import channel_for

        dws = self._dws()
        dws.streaming_cards = True
        self.assertIs(channel_for("jira:PROJ-1", dws).streaming_cards, False)
        self.assertIs(channel_for("gitlab:g/p!1", dws).streaming_cards, False)

    def test_read_path_degrades_to_dws_on_unknown_prefix(self) -> None:
        """读历史遇到未注册前缀降级到 dws；发送路径仍 fail loud。"""
        import asyncio

        from dws_dm_agent.core import Contact
        from dws_dm_agent.service import AgentService

        class _FakeDws:
            def __init__(self) -> None:
                self.calls = 0

            async def history(self, contact):
                self.calls += 1
                return []

        service = AgentService.__new__(AgentService)
        service.dws = _FakeDws()
        contact = Contact(
            alias="someone", display_name="someone", user_id="u1", open_dingtalk_id=""
        )
        self.assertEqual(asyncio.run(service._history(contact, "legacy:abc")), [])
        self.assertEqual(service.dws.calls, 1)


class WatchHitTests(unittest.TestCase):
    def _service(self, root: Path):
        from dws_dm_agent.service import AgentService

        service = AgentService.__new__(AgentService)
        service.mode = "shadow"
        service.settings = SimpleNamespace(
            timezone=__import__("zoneinfo").ZoneInfo("Asia/Shanghai"),
            self_open_id="self-open-id",
            raw={
                "watches": [
                    {
                        "source": "jira",
                        "name": "新 issue",
                        "projects": ["ZKAI"],
                        "instruction": "分析这个 issue。",
                    }
                ]
            },
        )
        service.store = AuditStore(root / "audit.sqlite3")
        service.queues = {}
        service.workers = {}
        service.configuration_lock = __import__("threading").RLock()
        from dws_dm_agent.watches import parse_watches

        service._watch_rules = parse_watches(service.settings.raw["watches"])
        return service

    def test_hit_creates_task_and_event_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = self._service(root)
            queued: list[IncomingEvent] = []
            service._enqueue = lambda event: queued.append(event)

            asyncio.run(service._handle_watch_hit(service._watch_rules[0], _item()))
            asyncio.run(service._handle_watch_hit(service._watch_rules[0], _item()))

            from dws_dm_agent.core import IncomingEvent as E

            self.assertIsInstance(queued[0], E)
            self.assertEqual(queued[0].origin, "watch-jira")
            self.assertIn("分析这个 issue。", queued[0].content)
            self.assertEqual(len(queued), 1, "claim_event 去重：同 key 只入队一次")

            tasks = service.store.board_tasks(include_done=True)
            self.assertEqual(len(tasks), 1)
            self.assertIn("新 issue", tasks[0]["goal"])
            self.assertEqual(tasks[0]["status"], "open")
            service.store.close()

    def test_no_rules_means_idle_dispatcher(self) -> None:
        from dws_dm_agent.service import AgentService

        service = AgentService.__new__(AgentService)
        service.settings = SimpleNamespace(raw={}, timezone=None)
        service.store = AuditStore(Path(tempfile.mkdtemp()) / "a.sqlite3")
        service.stop_event = asyncio.Event()
        self.assertIsNone(asyncio.run(service._consume_watches()))
        service.store.close()

    def test_two_goals_on_one_issue_remain_independent_across_repoll(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(Path(tmp))
            self.addCleanup(service.store.close)
            service._enqueue = Mock()
            item = _item()
            rules = parse_watches([
                {"source": "jira", "name": "修复", "instruction": "修复并验证问题"},
                {"source": "jira", "name": "客户说明", "instruction": "准备面向客户的说明"},
            ])
            for rule in rules * 2:
                asyncio.run(service._handle_watch_hit(rule, item))
            first, second = [call.args[0] for call in service._enqueue.call_args_list]
            self.assertEqual(first.conversation_id, second.conversation_id)
            self.assertNotEqual(first.message_id, second.message_id)
            self.assertEqual({first.watch_authority, second.watch_authority}, {r.authority_key for r in rules})
            tasks = service.store.board_tasks()
            self.assertEqual({task["goal"].split(":")[0] for task in tasks}, {"客户说明", "修复"})
            for task in tasks:
                source, = service.store.task_sources(task["id"])
                self.assertEqual(source["conversation_id"], item.conversation_id)

    def test_upgrade_does_not_restart_a_watch_goal_already_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(Path(tmp))
            self.addCleanup(service.store.close)
            service._enqueue = Mock()
            item = _item()
            rule = service._watch_rules[0]
            old = IncomingEvent("old-watch", item.dedup_key, item.conversation_id, item.contact,
                                f"【Watch 命中：{rule.name}】{item.title}", item.created_at)
            service.store.claim_event(old)
            service.store.connection.execute("UPDATE events SET input_v1 = NULL")
            service.store.connection.commit()
            asyncio.run(service._handle_watch_hit(rule, item))
            service._enqueue.assert_not_called()


class GitLabSourceTests(unittest.TestCase):
    def test_poll_parses_api_response(self) -> None:
        from dws_dm_agent.sources.external import GitLabSource

        raw = [
            {
                "iid": 42,
                "title": "MR 标题",
                "description": "d",
                "web_url": "u",
                "updated_at": "2026-11-08T10:00:00+08:00",
            }
        ]
        with patch.dict(os.environ, {"GITLAB_TOKEN": "t"}), patch(
            "dws_dm_agent.sources.external.request_json", return_value=raw
        ) if False else patch(
            "dws_dm_agent.sources.http.request_json", return_value=raw
        ):
            source = GitLabSource(["inoryirony/dws-chat-agent"])
            items = asyncio.run(
                source.poll(datetime(2026, 11, 7, tzinfo=None))
            )
        self.assertEqual(len(items), 1)
        self.assertEqual(
            items[0].dedup_key, "gitlab:inoryirony/dws-chat-agent!42"
        )
        self.assertEqual(items[0].contact.alias, "inoryirony/dws-chat-agent!42")

    def test_poll_skips_stale_updates(self) -> None:
        from dws_dm_agent.sources.external import GitLabSource

        raw = [
            {
                "iid": 43,
                "title": "旧 MR",
                "updated_at": "2026-11-01T10:00:00+08:00",
            }
        ]
        with patch.dict(os.environ, {"GITLAB_TOKEN": "t"}), patch(
            "dws_dm_agent.sources.http.request_json", return_value=raw
        ):
            source = GitLabSource(["a/b"])
            items = asyncio.run(source.poll(datetime(2026, 11, 7, tzinfo=None)))
        self.assertEqual(items, [])


if __name__ == "__main__":
    unittest.main()


class WatchNotifyTests(unittest.TestCase):
    """watch 任务终态的钉钉 DM 通知（人不看 issue，完成信号主动送达）。"""

    def test_notify_field_defaults_dm_and_rejects_unknown(self) -> None:
        rules = parse_watches([
            {"source": "jira", "instruction": "处理。"},
        ])
        self.assertEqual(rules[0].notify, "dm")
        with self.assertRaisesRegex(ValueError, "notify"):
            parse_watches([
                {"source": "jira", "instruction": "处理。", "notify": "sms"},
            ])

    def test_rule_allows_notify_by_linked_watch_authority(self) -> None:
        from types import SimpleNamespace
        from dws_dm_agent.service import AgentService
        from dws_dm_agent.watches import WatchRule

        rules = [
            WatchRule(name="r1", source="jira", instruction="x", notify="none"),
            WatchRule(name="r2", source="jira", instruction="y", notify="dm"),
        ]
        store = SimpleNamespace(task_watch_authorities=Mock(return_value=[]))
        service = SimpleNamespace(_watch_rules=rules, store=store)
        self.assertFalse(AgentService._watch_rule_allows_notify(service, "task-1"))
        store.task_watch_authorities.return_value = [rules[0].authority_key]
        self.assertFalse(AgentService._watch_rule_allows_notify(service, "task-1"))
        store.task_watch_authorities.return_value = [rules[1].authority_key]
        self.assertTrue(AgentService._watch_rule_allows_notify(service, "task-1"))
        store.task_watch_authorities.return_value = [
            rules[0].authority_key, rules[1].authority_key,
        ]
        self.assertFalse(AgentService._watch_rule_allows_notify(service, "task-1"))

    def test_later_cross_conversation_source_cannot_grant_watch_notify(self) -> None:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from dws_dm_agent.core import AuditStore, Contact, TaskScope
        from dws_dm_agent.watches import WatchRule

        tz = ZoneInfo("Asia/Shanghai")
        now = datetime.now(tz)
        original_rule = WatchRule(
            name="silent", source="jira", instruction="x", notify="none"
        )
        linked_rule = WatchRule(
            name="notify", source="jira", instruction="y", notify="dm"
        )
        with tempfile.TemporaryDirectory() as tmp:
            store = AuditStore(Path(tmp) / "audit.sqlite3", self_id="self-1")
            original = IncomingEvent(
                "watch-1", "message-1", "jira:ZKAI-1",
                Contact("ZKAI-1", "jira:ZKAI-1", "jira:ZKAI-1", ""),
                "原始命中", now, "watch-jira", original_rule.authority_key,
            )
            linked = IncomingEvent(
                "watch-2", "message-2", "jira:ZKAI-2",
                Contact("ZKAI-2", "jira:ZKAI-2", "jira:ZKAI-2", ""),
                "后来关联", now, "watch-jira", linked_rule.authority_key,
            )
            store.claim_event(original)
            store.claim_event(linked)
            recorded = store.record_task(
                scope=TaskScope("self-1", "jira:ZKAI-1", "jira:ZKAI-1"),
                goal="处理 ZKAI-1", status="open",
                evidence_message_ids=[original.message_id], evidence_at=now, now=now,
            )
            task_id = recorded["task"]["id"]
            self.assertTrue(store.link_task_source(task_id, linked.message_id, "self-1"))

            self.assertEqual(
                store.task_watch_authorities(task_id),
                [original_rule.authority_key],
            )
            store.close()

    def test_drain_queues_persistent_notification_for_explicit_recipient(self) -> None:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from dws_dm_agent.core import AuditStore, Contact, TaskScope
        from dws_dm_agent.service import AgentService
        from dws_dm_agent.watches import WatchRule

        tz = ZoneInfo("Asia/Shanghai")
        with tempfile.TemporaryDirectory() as tmp:
            store = AuditStore(Path(tmp) / "audit.sqlite3", self_id="self-1")
            now = datetime.now(tz)
            scope = TaskScope("self-1", "jira:ZKAI-1", "jira:ZKAI-1")
            rule = WatchRule(name="r", source="jira", instruction="x")
            source = IncomingEvent(
                "watch-event", "watch-message", "jira:ZKAI-1",
                Contact("ZKAI-1", "jira:ZKAI-1", "jira:ZKAI-1", ""),
                "处理 issue", now, origin="watch-jira",
                watch_authority=rule.authority_key,
            )
            store.claim_event(source)
            recorded = store.record_task(
                scope=scope, goal="规则A: 修问题", status="completed",
                waiting_on="完成后通知：联系人A",
                evidence_summary="", evidence_message_ids=[source.message_id], resources=[],
                evidence_at=now, now=now,
            )
            store._drain_pending_terminal = None  # 未消费标记用
            events = store.pending_terminal_events()
            self.assertEqual(len(events), 1)

            target = __import__("dws_dm_agent.core", fromlist=["Contact"]).Contact(
                "联系人A", "联系人A", "u-contact-a", "open-contact-a"
            )
            svc = AgentService.__new__(AgentService)
            svc.store = store
            svc.settings = SimpleNamespace(
                timezone=tz,
                self_name="我",
                self_open_id="self-open",
                contacts=(target,),
            )
            svc._watch_rules = [rule]
            svc.outbox_wakeup = asyncio.Event()

            svc._drain_terminal_events(now)

            receipts = store.delivery_receipts()
            self.assertEqual(len(receipts), 1)
            self.assertEqual(receipts[0]["target_kind"], "watch_notification")
            self.assertEqual(receipts[0]["destination"], "u-contact-a")
            self.assertEqual(receipts[0]["delivery_status"], "pending")
            self.assertNotIn("已作为评论发回", store.connection.execute(
                "SELECT payload_text FROM outgoing WHERE send_uuid = ?",
                (receipts[0]["action_id"],),
            ).fetchone()["payload_text"])
            self.assertEqual(store.pending_terminal_events(), [])
            self.assertTrue(recorded["ok"])
            store.close()

    def test_confirmed_comment_and_failed_dm_keep_independent_receipts(self) -> None:
        from zoneinfo import ZoneInfo
        from dws_dm_agent.core import Contact, TaskScope
        from dws_dm_agent.service import AgentService
        from dws_dm_agent.watches import WatchRule

        tz = ZoneInfo("Asia/Shanghai")
        now = datetime.now(tz)
        with tempfile.TemporaryDirectory() as tmp:
            store = AuditStore(Path(tmp) / "audit.sqlite3", self_id="self")
            jira_contact = Contact("ZKAI-505", "jira:ZKAI-505", "jira:ZKAI-505", "")
            source = IncomingEvent(
                "jira-event", "jira-message", "jira:ZKAI-505", jira_contact,
                "处理 issue", now, origin="watch-jira",
            )
            rule = WatchRule(name="r", source="jira", instruction="x")
            source = IncomingEvent(
                source.event_id, source.message_id, source.conversation_id,
                source.contact, source.content, source.created_at, source.origin,
                rule.authority_key,
            )
            store.claim_event(source)
            store.enqueue_delivery(
                "jira-comment-action", source, "已确认修复", "watch-run",
                event_ids=[source.message_id],
            )
            store.mark_delivery_sent("jira-comment-action", "comment-505")
            recorded = store.record_task(
                scope=TaskScope("self", "jira:ZKAI-505", "jira:ZKAI-505"),
                goal="修复 ZKAI-505", status="completed",
                waiting_on="完成后通知：联系人A",
                evidence_message_ids=[source.message_id], evidence_at=now, now=now,
            )
            target = Contact("联系人A", "联系人A", "u-contact-a", "open-contact-a")
            svc = AgentService.__new__(AgentService)
            svc.mode = "live"
            svc.store = store
            svc.settings = SimpleNamespace(
                timezone=tz, self_name="我", self_open_id="self-open",
                contacts=(target,), raw={}, cooldown=600,
            )
            svc._watch_rules = [rule]
            svc.outbox_wakeup = asyncio.Event()
            svc.streaming_card_send_uuids = {}
            svc._history_events = Mock(return_value=[])
            svc.dws = SimpleNamespace(
                name="dws", streaming_cards=False, supports_freshness=True,
                retry_safe=True, supports_reconciliation=False,
                history=AsyncMock(return_value=[]),
                send=AsyncMock(side_effect=RuntimeError("dm unavailable")),
            )

            svc._drain_terminal_events(now)
            asyncio.run(svc._dispatch_due_delivery())

            receipts = {item["action_id"]: item for item in store.delivery_receipts()}
            notify_id = f"watch-notify-{recorded['task']['id']}-1"
            self.assertEqual(receipts["jira-comment-action"]["receipt_status"], "confirmed")
            self.assertEqual(receipts[notify_id]["delivery_status"], "retry_wait")
            self.assertEqual(receipts[notify_id]["receipt_status"], "pending")
            store.close()


class WatchNotifyTargetTests(unittest.TestCase):
    """「完成后通知：<姓名>」协议：worker 决策收件人，服务侧解析发钉钉。"""

    def _service_stub(self, contacts):
        from types import SimpleNamespace
        from dws_dm_agent.core import Contact
        return SimpleNamespace(
            store=None,
            settings=SimpleNamespace(
                timezone=None, self_name="我", self_open_id="self-open",
                contacts=tuple(contacts),
            ),
        )

    def test_target_resolves_notify_name_to_whitelist_contact(self) -> None:
        from dws_dm_agent.core import AuditStore, Contact, TaskScope
        from dws_dm_agent.service import AgentService
        from datetime import datetime
        from zoneinfo import ZoneInfo

        tz = ZoneInfo("Asia/Shanghai")
        contacts = [
            Contact("联系人N", "联系人N-联系人N", "u-contact-x", "open-contact-x"),
            Contact("联系人A", "联系人A", "u-contact-a", "open-contact-a"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            store = AuditStore(Path(tmp) / "audit.sqlite3")
            now = datetime.now(tz)
            scope = TaskScope("self-1", "jira:ZKAI-1", "jira:ZKAI-1")
            store.record_task(
                scope=scope, goal="规则: 修问题",
                status="completed", waiting_on="完成后通知：联系人A",
                evidence_summary="", evidence_message_ids=[], resources=[],
                evidence_at=now, now=now,
            )
            tasks = store.lookup_tasks(scope=scope)
            svc = AgentService.__new__(AgentService)
            svc.store = store
            svc.settings = self._service_stub(contacts).settings

            # 姓名命中白名单：发给对应联系人。
            target = svc._watch_notify_target(tasks[0]["id"])
            self.assertEqual(target.user_id, "u-contact-a")

            # 非精确显示名不匹配，避免同名/部分命中误发。
            store.record_task(
                scope=TaskScope("self-1", "jira:ZKAI-2", "jira:ZKAI-2"),
                goal="规则: 另一个", status="completed",
                waiting_on="完成后通知：联系人N-联系人",
                evidence_summary="", evidence_message_ids=[], resources=[],
                evidence_at=now, now=now,
            )
            scope2 = TaskScope("self-1", "jira:ZKAI-2", "jira:ZKAI-2")
            task2 = store.lookup_tasks(scope=scope2)[0]
            target2 = svc._watch_notify_target(task2["id"])
            self.assertIsNone(target2)

            # 白名单外姓名 / 无指定：不隐式改发本人。
            store.record_task(
                scope=TaskScope("self-1", "jira:ZKAI-3", "jira:ZKAI-3"),
                goal="规则: 第三个", status="completed", waiting_on="等部署完成",
                evidence_summary="", evidence_message_ids=[], resources=[],
                evidence_at=now, now=now,
            )
            task3 = store.lookup_tasks(scope=TaskScope("self-1", "jira:ZKAI-3", "jira:ZKAI-3"))[0]
            target3 = svc._watch_notify_target(task3["id"])
            self.assertIsNone(target3)
            store.close()
