import json
import tempfile
import unittest
from copy import deepcopy
from datetime import timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from dws_dm_agent.dashboard import DashboardServer

SAMPLE_BOARD = {
    "runs": [
        {
            "session_id": "dm-luna-abc123",
            "task_id": "t-1",
            "contact_id": "user-1",
            "stage": "worker",
            "status": "running",
            "headline": "正在整理发布周报",
            "started_at": "2026-11-06T09:00:00+08:00",
            "updated_at": "2026-11-06T09:05:00+08:00",
            "ended_at": None,
            "outcome": None,
            "recent_progress": [
                {
                    "seq": 1,
                    "text": "拉取了三处数据",
                    "created_at": "2026-11-06T09:01:00+08:00",
                },
                {
                    "seq": 2,
                    "text": "初稿成型",
                    "created_at": "2026-11-06T09:04:00+08:00",
                },
            ],
        }
    ],
    "tasks": [
        {
            "id": "t-1",
            "contact_id": "user-1",
            "goal": "整理本周发布周报",
            "status": "open",
            "waiting_on": "",
            "contact_ids": ["user-1"],
            "facts": [
                {
                    "id": 1,
                    "task_id": "t-1",
                    "text": "周报要含失败率",
                    "anchor_message_id": "",
                    "source": "human",
                    "created_at": "2026-11-06T08:00:00+08:00",
                }
            ],
        }
    ],
    "waiting_operator": [
        {
            "id": "t-3",
            "contact_id": "user-3",
            "goal": "是否同意直接上线？",
            "status": "waiting",
            "waiting_on": "天泽",
            "contact_ids": [],
            "facts": [],
        }
    ],
    "recent_done": [],
}


class FakeService:
    def __init__(self):
        self.calls = []

    def board_snapshot(self):
        return deepcopy(SAMPLE_BOARD)

    async def open_operator_session(self, text, contact_alias=None):
        self.calls.append(("open_operator_session", text, contact_alias))
        return "sess-1a2b3c4d5e"

    async def steer_run(self, session_id, text):
        self.calls.append(("steer_run", session_id, text))
        return True

    async def answer_task(self, task_id, answer):
        self.calls.append(("answer_task", task_id, answer))
        return True


class BoardEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        (root / "dashboard.html").write_text(
            "<!doctype html><title>board</title>", encoding="utf-8"
        )

    def start_server(self, service):
        root = Path(self.temporary.name)
        server = DashboardServer(
            root / "audit.sqlite3",
            root / "runtime.json",
            root / "dashboard.html",
            {},
            timezone.utc,
            port=0,
            service=service,
        )
        server.start()
        self.addCleanup(server.stop)
        assert server.server is not None
        return f"http://127.0.0.1:{server.server.server_port}"

    def post_json(self, base, path, payload, *, origin=None):
        headers = {"Content-Type": "application/json"}
        if origin:
            headers["Origin"] = origin
        request = Request(
            f"{base}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urlopen(request, timeout=5) as response:
            return json.load(response)

    def test_board_get_passes_service_snapshot_through(self) -> None:
        base = self.start_server(FakeService())
        with urlopen(f"{base}/api/board", timeout=5) as response:
            self.assertEqual(
                response.headers.get_content_type(), "application/json"
            )
            payload = json.load(response)
        self.assertEqual(payload, SAMPLE_BOARD)

    def test_open_session_calls_service_with_text_and_alias(self) -> None:
        service = FakeService()
        base = self.start_server(service)
        result = self.post_json(
            base,
            "/api/session/new",
            {"text": " 整理周报 ", "contact_alias": "alice"},
        )
        self.assertEqual(result, {"sessionId": "sess-1a2b3c4d5e"})
        self.assertEqual(
            service.calls, [("open_operator_session", "整理周报", "alice")]
        )

    def test_open_session_without_alias_passes_none(self) -> None:
        service = FakeService()
        base = self.start_server(service)
        self.post_json(base, "/api/session/new", {"text": "内部巡检"})
        self.assertEqual(
            service.calls, [("open_operator_session", "内部巡检", None)]
        )

    def test_steer_calls_service_with_session_and_text(self) -> None:
        service = FakeService()
        base = self.start_server(service)
        result = self.post_json(
            base, "/api/run/dm-luna-abc123/steer", {"text": "先查日志"}
        )
        self.assertEqual(result, {"ok": True})
        self.assertEqual(
            service.calls, [("steer_run", "dm-luna-abc123", "先查日志")]
        )

    def test_answer_calls_service_with_task_id_and_answer(self) -> None:
        service = FakeService()
        base = self.start_server(service)
        result = self.post_json(
            base, "/api/task/answer", {"task_id": 7, "answer": "同意"}
        )
        self.assertEqual(result, {"ok": True})
        self.assertEqual(service.calls, [("answer_task", 7, "同意")])

    def test_steer_false_maps_to_not_found(self) -> None:
        class NoSteer(FakeService):
            async def steer_run(self, session_id, text):
                self.calls.append(("steer_run", session_id, text))
                return False

        service = NoSteer()
        base = self.start_server(service)
        with self.assertRaises(HTTPError) as raised:
            self.post_json(base, "/api/run/dm-gone/steer", {"text": "还在吗"})
        self.assertEqual(raised.exception.code, 404)

    def test_board_write_rejects_empty_text(self) -> None:
        service = FakeService()
        base = self.start_server(service)
        with self.assertRaises(HTTPError) as raised:
            self.post_json(base, "/api/session/new", {"text": "   "})
        self.assertEqual(raised.exception.code, 400)
        self.assertEqual(service.calls, [])

    def test_board_write_rejects_foreign_origin(self) -> None:
        service = FakeService()
        base = self.start_server(service)
        with self.assertRaises(HTTPError) as raised:
            self.post_json(
                base,
                "/api/task/answer",
                {"task_id": 7, "answer": "同意"},
                origin="https://outside.example",
            )
        self.assertEqual(raised.exception.code, 403)
        self.assertEqual(service.calls, [])

    def test_board_endpoints_unavailable_without_service(self) -> None:
        base = self.start_server(None)
        with self.assertRaises(HTTPError) as raised:
            urlopen(f"{base}/api/board", timeout=5)
        self.assertEqual(raised.exception.code, 503)
        with self.assertRaises(HTTPError) as raised:
            self.post_json(base, "/api/session/new", {"text": "任何内容"})
        self.assertEqual(raised.exception.code, 503)

    def test_service_resolved_from_bound_contact_search(self) -> None:
        class SearchHolder(FakeService):
            def _search(self, query):
                return [{"name": query}]

        holder = SearchHolder()
        root = Path(self.temporary.name)
        server = DashboardServer(
            root / "audit.sqlite3",
            root / "runtime.json",
            root / "dashboard.html",
            {},
            timezone.utc,
            port=0,
            contact_search=holder._search,
        )
        server.start()
        self.addCleanup(server.stop)
        self.assertIs(server.board_service(), holder)


class BoardPageMarkupTests(unittest.TestCase):
    def test_dashboard_wires_waiting_operator_and_write_endpoints(self) -> None:
        page = (
            Path(__file__).parents[1]
            / "src"
            / "dws_dm_agent"
            / "web"
            / "dashboard.html"
        ).read_text(encoding="utf-8")
        for needle in (
            "/api/board",
            "/api/session/new",
            "/api/run/",
            "/api/task/answer",
            "waiting_operator",
            "等我裁决",
            "在跑会话",
            "▶",
            "⏸",
        ):
            self.assertIn(needle, page)
        for column_head in ("id=\"colOpen\"", "id=\"colWait\"", "id=\"colDone\""):
            self.assertIn(column_head, page)


if __name__ == "__main__":
    unittest.main()
