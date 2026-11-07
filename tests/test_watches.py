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
