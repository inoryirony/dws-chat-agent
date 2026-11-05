import asyncio
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from dws_dm_agent.config import Settings
from dws_dm_agent.core import AuditStore, Contact, IncomingEvent, TaskScope
from dws_dm_agent.dashboard import DashboardServer
from dws_dm_agent.service import AgentService

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
        self.server_ref = server
        assert server.server is not None
        return f"http://127.0.0.1:{server.server.server_port}"

    def post_json(self, base, path, payload, *, origin=None, token=None):
        headers = {"Content-Type": "application/json"}
        if origin:
            headers["Origin"] = origin
        if token is not None:
            headers["X-Operator-Token"] = token
        request = Request(
            f"{base}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urlopen(request, timeout=5) as response:
            return json.load(response)

    def get_json(self, base, path, *, token=None):
        headers = {}
        if token is not None:
            headers["X-Operator-Token"] = token
        request = Request(f"{base}{path}", headers=headers)
        with urlopen(request, timeout=5) as response:
            return json.load(response)

    def test_board_get_passes_service_snapshot_through(self) -> None:
        base = self.start_server(FakeService())
        payload = self.get_json(base, "/api/board", token=self.server_ref.operator_token)
        self.assertEqual(payload, SAMPLE_BOARD)

    def test_board_read_and_writes_reject_missing_or_wrong_token(self) -> None:
        service = FakeService()
        base = self.start_server(service)
        for token in (None, "wrong-token"):
            with self.assertRaises(HTTPError) as raised:
                self.get_json(base, "/api/board", token=token)
            self.assertEqual(raised.exception.code, 403)
            with self.assertRaises(HTTPError) as raised:
                self.post_json(
                    base,
                    "/api/task/answer",
                    {"task_id": 7, "answer": "同意"},
                    token=token,
                )
            self.assertEqual(raised.exception.code, 403)
        self.assertEqual(service.calls, [])

    def test_open_session_calls_service_with_text_and_alias(self) -> None:
        service = FakeService()
        base = self.start_server(service)
        result = self.post_json(
            base,
            "/api/session/new",
            {"text": " 整理周报 ", "contact_alias": "alice"},
            token=self.server_ref.operator_token,
        )
        self.assertEqual(result, {"sessionId": "sess-1a2b3c4d5e"})
        self.assertEqual(
            service.calls, [("open_operator_session", "整理周报", "alice")]
        )

    def test_open_session_without_alias_passes_none(self) -> None:
        service = FakeService()
        base = self.start_server(service)
        self.post_json(
            base,
            "/api/session/new",
            {"text": "内部巡检"},
            token=self.server_ref.operator_token,
        )
        self.assertEqual(
            service.calls, [("open_operator_session", "内部巡检", None)]
        )

    def test_steer_calls_service_with_session_and_text(self) -> None:
        service = FakeService()
        base = self.start_server(service)
        result = self.post_json(
            base,
            "/api/run/dm-luna-abc123/steer",
            {"text": "先查日志"},
            token=self.server_ref.operator_token,
        )
        self.assertEqual(result, {"ok": True})
        self.assertEqual(
            service.calls, [("steer_run", "dm-luna-abc123", "先查日志")]
        )

    def test_answer_calls_service_with_task_id_and_answer(self) -> None:
        service = FakeService()
        base = self.start_server(service)
        result = self.post_json(
            base,
            "/api/task/answer",
            {"task_id": 7, "answer": "同意"},
            token=self.server_ref.operator_token,
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
            self.post_json(
                base,
                "/api/run/dm-gone/steer",
                {"text": "还在吗"},
                token=self.server_ref.operator_token,
            )
        self.assertEqual(raised.exception.code, 404)

    def test_board_write_rejects_empty_text(self) -> None:
        service = FakeService()
        base = self.start_server(service)
        with self.assertRaises(HTTPError) as raised:
            self.post_json(
                base,
                "/api/session/new",
                {"text": "   "},
                token=self.server_ref.operator_token,
            )
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
                token=self.server_ref.operator_token,
            )
        self.assertEqual(raised.exception.code, 403)
        self.assertEqual(service.calls, [])

    def test_board_endpoints_unavailable_without_service(self) -> None:
        base = self.start_server(None)
        token = self.server_ref.operator_token
        with self.assertRaises(HTTPError) as raised:
            self.get_json(base, "/api/board", token=token)
        self.assertEqual(raised.exception.code, 503)
        with self.assertRaises(HTTPError) as raised:
            self.post_json(
                base, "/api/session/new", {"text": "任何内容"}, token=token
            )
        self.assertEqual(raised.exception.code, 503)

    def test_operator_token_is_random_per_process(self) -> None:
        base_one = self.start_server(FakeService())
        token_one = self.server_ref.operator_token
        base_two = self.start_server(FakeService())
        self.assertNotEqual(token_one, self.server_ref.operator_token)
        self.assertIn("token=", self.server_ref.tokened_url)

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
            # 操作者令牌：URL ?token= 交付一次，localStorage 保存后随
            # 读写请求附 X-Operator-Token 头。
            'localStorage.setItem("dwsPanelToken"',
            'localStorage.getItem("dwsPanelToken"',
            "X-Operator-Token",
        ):
            self.assertIn(needle, page)
        for column_head in ("id=\"colOpen\"", "id=\"colWait\"", "id=\"colDone\""):
            self.assertIn(column_head, page)


# 模拟可长期存活的 pi RPC 子进程：回一个 prompt 成功响应后挂起。
_WAIT_EMITTER = """
import json, sys, time

request = json.loads(sys.stdin.readline())
print(json.dumps({
    "id": request.get("id"),
    "type": "response",
    "command": "prompt",
    "success": True,
}), flush=True)
while True:
    time.sleep(1)
"""

# 只放慢 `git worktree add` 的 PATH 前置包装：其余 git 调用原样透传。
_SLOW_GIT = """#!/bin/sh
case " $* " in
  *" worktree add "*) sleep 1.5 ;;
esac
exec {real_git} "$@"
"""


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)

    def git(*args: str) -> None:
        subprocess.run(
            ["git", "-C", str(path), *args],
            check=True, capture_output=True, text=True,
        )

    git("init", "-q")
    git("config", "user.email", "board@example.com")
    git("config", "user.name", "board")
    (path / "README.md").write_text("hello", encoding="utf-8")
    git("add", ".")
    git("commit", "-qm", "init")
    return path


def _real_service(
    root: Path,
    *,
    timeout_seconds: float = 30,
    worktree_repo: str = "",
) -> AgentService:
    """构造完整 AgentService：真实 store/runtime/dashboard，无外部副作用。"""
    root.mkdir(parents=True, exist_ok=True)
    (root / "prompts").mkdir(exist_ok=True)
    (root / "prompts" / "worker.md").write_text(
        "worker {{self_name}}", encoding="utf-8"
    )
    (root / "prompts" / "supplement.md").write_text(
        "supplement {{supplement_messages_json}}", encoding="utf-8"
    )
    emitter = root / "wait-emitter.py"
    emitter.write_text(_WAIT_EMITTER, encoding="utf-8")
    alice = {
        "alias": "alice",
        "display_name": "Alice",
        "user_id": "user-alice",
        "open_dingtalk_id": "open-alice",
    }
    raw = {
        "self": {"open_dingtalk_id": "self-open", "name": "天泽"},
        "contacts": [alice],
        "dws": {"streaming_cards": False},
        "worktree_root": str(root / "wt"),
        "agents": {
            "pi-worker": {
                "command": [sys.executable, str(emitter)],
                "protocol": "pi-rpc",
                "model": "test-model",
                "reasoning_effort": "high",
                "read_only": False,
                "timeout_seconds": timeout_seconds,
                "worktree_repo": worktree_repo,
            }
        },
        "workflows": {
            "active": "dm-default",
            "presets": {
                "dm-default": {
                    "supplement_strategy": "steer",
                    "supplement_prompt": "prompts/supplement.md",
                    "auto_messages": {"ack_enabled": False},
                    "worker": {
                        "agent": "pi-worker",
                        "prompt": "prompts/worker.md",
                    },
                }
            },
        },
    }
    config_path = root / "config.json"
    config_path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    settings = Settings(
        raw=raw,
        config_path=config_path,
        env_path=root / ".env",
        timezone=timezone(timedelta(hours=8)),
        contacts=(Contact(**alice),),
        state_dir=root / "state",
        workspace_root=root / "workspace",
        worktree_root=root / "wt",
    )
    settings.workspace_root.mkdir(parents=True, exist_ok=True)
    service = AgentService(settings, "shadow")
    # 0 端口：测试并行时由内核分配，避免撞默认 8765。
    service.dashboard.port = 0
    return service


class RealServiceBoardIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """P0-1/P0-2 集成：真 AgentService + 真实 handler 线程 + 真实 store。"""

    async def test_board_endpoint_serves_snapshot_via_service_loop(self) -> None:
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        service = _real_service(root)
        self.addCleanup(service.close)
        seeded = service.store.record_task(
            scope=TaskScope("self-open", "user-alice", "conversation-1"),
            goal="等部署后回归验证",
            status="waiting",
            waiting_on="对方周四部署",
            evidence_at=datetime.now(service.settings.timezone),
            now=datetime.now(service.settings.timezone),
        )
        self.assertTrue(seeded["ok"])
        service.dashboard.start()
        self.addCleanup(service.dashboard.stop)
        base = (
            f"http://127.0.0.1:"
            f"{service.dashboard.server.server_port}"
        )
        token = service.dashboard.operator_token

        def fetch_board(headers: dict) -> object:
            request = Request(f"{base}/api/board", headers=headers)
            with urlopen(request, timeout=10) as response:
                self.assertEqual(response.status, 200)
                return json.load(response)

        # 经服务事件循环读取：HTTP 线程发起，快照在 loop 线程执行。
        payload = await asyncio.to_thread(fetch_board, {"X-Operator-Token": token})
        self.assertEqual(
            set(payload),
            {
                "runs",
                "tasks",
                "recent_failed",
                "waiting_operator",
                "recent_done",
                "panel_replies",
            },
        )
        self.assertEqual(payload["tasks"][0]["goal"], "等部署后回归验证")

        # 缺 token / 错 token 一律 403（P0-2 集成面）。
        for bad in ({}, {"X-Operator-Token": "wrong"}):
            with self.assertRaises(HTTPError) as raised:
                await asyncio.to_thread(fetch_board, bad)
            self.assertEqual(raised.exception.code, 403)

    async def test_panel_access_files_are_private_and_complete(self) -> None:
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        service = _real_service(root)
        self.addCleanup(service.close)
        service.dashboard.start()
        self.addCleanup(service.dashboard.stop)
        token = service.dashboard.operator_token

        service._write_panel_access_files()

        token_path = service.settings.state_dir / "operator_token"
        url_path = service.settings.state_dir / "panel.url"
        self.assertEqual(
            token_path.read_text(encoding="utf-8").strip(), token
        )
        url = url_path.read_text(encoding="utf-8").strip()
        self.assertEqual(url, service.dashboard.tokened_url)
        self.assertIn(f"?token={token}", url)
        for path in (token_path, url_path):
            self.assertEqual(
                stat.S_IMODE(path.stat().st_mode), 0o600, str(path)
            )
        # token 从不进环境变量，也就不可能渗进任何子进程。
        self.assertNotIn(token, json.dumps(dict(os.environ)))


class SlowWorktreeLoopTests(unittest.IsolatedAsyncioTestCase):
    """P0-4：慢仓建 worktree 期间事件循环仍可调度。"""

    async def test_worktree_creation_does_not_block_the_loop(self) -> None:
        root = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        _git_repo(root / "workspace" / "repo")
        service = _real_service(root, timeout_seconds=2, worktree_repo="repo")
        self.addCleanup(service.close)
        bin_dir = root / "bin"
        bin_dir.mkdir()
        real_git = subprocess.run(
            ["which", "git"], capture_output=True, text=True, check=True
        ).stdout.strip()
        (bin_dir / "git").write_text(
            _SLOW_GIT.format(real_git=real_git), encoding="utf-8"
        )
        (bin_dir / "git").chmod(0o755)
        original_path = os.environ["PATH"]
        os.environ["PATH"] = f"{bin_dir}{os.pathsep}{original_path}"
        self.addCleanup(lambda: os.environ.__setitem__("PATH", original_path))
        contact = service.settings.contacts[0]
        at = datetime.now(service.settings.timezone)
        batch = [
            IncomingEvent(
                "event-m1", "message-1", "conversation-1", contact,
                "接口好了吗", at,
            )
        ]

        async def scenario() -> tuple[float, object]:
            loop = asyncio.get_running_loop()
            started = loop.time()
            creation = asyncio.create_task(service._run_agent(batch, ""))
            short = asyncio.create_task(asyncio.sleep(0.25))
            await asyncio.wait_for(short, timeout=10)
            loop_free_elapsed = loop.time() - started
            run_result = await creation
            return loop_free_elapsed, run_result

        elapsed, result = await scenario()

        # worktree add 被 wrapper 压住 1.5s：若它阻塞事件循环，0.25s
        # 的短任务要等它让出才能完成（elapsed >= 1.5s）。
        self.assertLess(elapsed, 1.0, "event loop was blocked by worktree add")
        self.assertEqual(result.error, "agent_timeout")
        # prepare 成功的运行把 worktree 路径写回 board_runs（P1-14 接线）。
        row = service.store.connection.execute(
            "SELECT worktree_path FROM board_runs WHERE session_id = ?",
            (result.session_id,),
        ).fetchone()
        self.assertIsNotNone(row["worktree_path"])
        self.assertTrue(
            str(row["worktree_path"]).startswith(
                str(service.settings.worktree_root)
            )
        )


class OrphanWorktreeReclaimTests(unittest.TestCase):
    """P1-14：崩溃运行的 worktree 在启动扫尾时回收。"""

    def test_startup_reclaim_removes_registered_worktree_under_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            _git_repo(root / "repo")

            def git(*args: str) -> str:
                return subprocess.run(
                    ["git", "-C", str(root / "repo"), *args],
                    check=True, capture_output=True, text=True,
                ).stdout

            orphan = root / "wt" / "repo-orphan"
            git("worktree", "add", "--detach", str(orphan), "HEAD")
            outside = root / "outside-wt"
            git("worktree", "add", "--detach", str(outside), "HEAD")
            plain = root / "wt" / "repo-not-a-worktree"
            plain.mkdir(parents=True)
            self.assertTrue(orphan.exists())

            service = AgentService.__new__(AgentService)
            service.settings = SimpleNamespace(worktree_root=root / "wt")
            service.store = AuditStore(root / "audit.sqlite3")
            self.addCleanup(service.store.close)
            for session_id, path in (
                ("run-orphan", orphan),
                ("run-outside", outside),
                ("run-plain", plain),
            ):
                service.store.register_run(
                    session_id, contact_id="user-alice", stage="worker",
                    now=datetime.now(timezone.utc),
                )
                service.store.set_run_worktree(session_id, path)

            swept = service.store.abort_stale_runs(
                datetime.now(timezone.utc) + timedelta(hours=1)
            )
            service._reclaim_orphan_worktrees(swept)

            # 根内且 git 登记的 worktree 被回收。
            self.assertFalse(orphan.exists())
            self.assertNotIn("repo-orphan", git("worktree", "list"))
            # 根外与未登记的目录原样保留：回收只动确定的孤儿。
            self.assertTrue(outside.exists())
            self.assertTrue(plain.exists())


if __name__ == "__main__":
    unittest.main()
