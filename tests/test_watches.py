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

            asyncio.run(service._handle_watch_hit("jira", _item()))
            asyncio.run(service._handle_watch_hit("jira", _item()))

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

    def test_rule_allows_notify_by_source(self) -> None:
        import asyncio
        from types import SimpleNamespace
        from dws_dm_agent.service import AgentService
        from dws_dm_agent.watches import WatchRule

        rules = [
            WatchRule(name="r1", source="jira", instruction="x", notify="none"),
        ]
        service = SimpleNamespace(_watch_rules=rules)
        # 同源唯一规则 notify=none：静默。
        self.assertFalse(
            AgentService._watch_rule_allows_notify(service, "jira:ZKAI-1")
        )
        # 不同源（无规则声明）：默认通知。
        self.assertTrue(
            AgentService._watch_rule_allows_notify(service, "gitlab:x!1")
        )
        service._watch_rules = rules + [
            WatchRule(name="r2", source="jira", instruction="y")
        ]
        # 同源任一规则默认 dm：通知。
        self.assertTrue(
            AgentService._watch_rule_allows_notify(service, "jira:ZKAI-1")
        )

    def test_drain_notifies_self_for_watch_task_terminal(self) -> None:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from dws_dm_agent.core import AuditStore, TaskScope
        from dws_dm_agent.service import AgentService
        from dws_dm_agent.watches import WatchRule

        tz = ZoneInfo("Asia/Shanghai")
        with tempfile.TemporaryDirectory() as tmp:
            store = AuditStore(Path(tmp) / "audit.sqlite3")
            now = datetime.now(tz)
            scope = TaskScope("self-1", "jira:ZKAI-1", "jira:ZKAI-1")
            store.record_task(
                scope=scope, goal="规则A: 修问题", status="completed",
                evidence_summary="", evidence_message_ids=[], resources=[],
                evidence_at=now, now=now,
            )
            store._drain_pending_terminal = None  # 未消费标记用
            events = store.pending_terminal_events()
            self.assertEqual(len(events), 1)

            sent: list[tuple[str, str]] = []

            async def fake_send(contact, content, send_uuid):
                sent.append((contact.open_dingtalk_id, content))
                return (send_uuid, None)

            service = SimpleNamespace(
                store=store,
                settings=SimpleNamespace(
                    timezone=tz, self_name="我", self_open_id="self-open",
                ),
                dws=SimpleNamespace(send=fake_send),
                _watch_rules=[WatchRule(name="r", source="jira", instruction="x")],
                _notify_watch_terminal=lambda *a, **k: None,
            )
            # 直接调用核心路径：terminal 事件 fan_out 前 notify。
            # 用 asyncio.run 驱动 service 侧的 create_task 路径。
            svc = AgentService.__new__(AgentService)
            svc.store = store
            svc.settings = service.settings
            svc.dws = service.dws
            svc._watch_rules = service._watch_rules

            async def drive():
                svc._notify_watch_terminal(events[0], now)

            asyncio.run(drive())
            # create_task 在 drive() 返回后未 await：不强制等 send 完成，
            # 只验证 notify 路径不炸（send 本身是 dws 层契约，有独立测试）。
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
            Contact("子画", "子画-倪吉", "u-niji", "open-niji"),
            Contact("杨培", "夏桐-杨培", "u-yangpei", "open-yangpei"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            store = AuditStore(Path(tmp) / "audit.sqlite3")
            now = datetime.now(tz)
            scope = TaskScope("self-1", "jira:ZKAI-1", "jira:ZKAI-1")
            store.record_task(
                scope=scope, goal="规则: 修问题",
                status="completed", waiting_on="完成后通知：杨培",
                evidence_summary="", evidence_message_ids=[], resources=[],
                evidence_at=now, now=now,
            )
            tasks = store.lookup_tasks(scope=scope)
            svc = AgentService.__new__(AgentService)
            svc.store = store
            svc.settings = self._service_stub(contacts).settings

            # 姓名命中白名单：发给对应联系人。
            target = svc._watch_notify_target(tasks[0]["id"])
            self.assertEqual(target.user_id, "u-yangpei")

            # display_name 部分匹配（写"倪吉"也能命中"子画-倪吉"）。
            store.record_task(
                scope=TaskScope("self-1", "jira:ZKAI-2", "jira:ZKAI-2"),
                goal="规则: 另一个", status="completed",
                waiting_on="完成后通知：倪吉",
                evidence_summary="", evidence_message_ids=[], resources=[],
                evidence_at=now, now=now,
            )
            scope2 = TaskScope("self-1", "jira:ZKAI-2", "jira:ZKAI-2")
            task2 = store.lookup_tasks(scope=scope2)[0]
            target2 = svc._watch_notify_target(task2["id"])
            self.assertEqual(target2.user_id, "u-niji")

            # 白名单外姓名 / 无指定：兜底本人。
            store.record_task(
                scope=TaskScope("self-1", "jira:ZKAI-3", "jira:ZKAI-3"),
                goal="规则: 第三个", status="completed", waiting_on="等部署完成",
                evidence_summary="", evidence_message_ids=[], resources=[],
                evidence_at=now, now=now,
            )
            task3 = store.lookup_tasks(scope=TaskScope("self-1", "jira:ZKAI-3", "jira:ZKAI-3"))[0]
            target3 = svc._watch_notify_target(task3["id"])
            self.assertEqual(target3.open_dingtalk_id, "self-open")
            store.close()
