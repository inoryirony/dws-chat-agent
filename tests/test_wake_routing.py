"""W2a：唤醒路由、两相卡片与操作者面板服务契约（§18 唤醒 2 条 + 卡片两相）。

覆盖：任务终态触发 wake（含 synthetic 回声切断）、@时刻到点入窗唤醒与
窗外顺延、wake/面板合成输入不进证据覆盖集、prompt 独立 <system_wake> 块、
面板内部轮回复不外发、steer 不写 manual、answer_task 恢复 open 并落
human 事实、卡片相1里程碑列表/相2整体覆盖。
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo

from dws_dm_agent.core import (
    AuditStore,
    Contact,
    IncomingEvent,
    SecurityGate,
    TaskScope,
)
from dws_dm_agent.runners import task_tools
from dws_dm_agent.service import AgentService, AgentResult

TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 11, 7, 10, 0, tzinfo=TZ)
ALICE = Contact("alice", "Alice", "user-alice", "open-alice")
BOB = Contact("bob", "Bob", "user-bob", "open-bob")
SELF_OPEN_ID = "self-open"


def _service(
    root: Path,
    *,
    contacts: tuple[Contact, ...] = (ALICE, BOB),
    streaming_cards: bool = True,
) -> AgentService:
    service = AgentService.__new__(AgentService)
    service.mode = "live"
    service.settings = SimpleNamespace(
        timezone=TZ,
        self_open_id=SELF_OPEN_ID,
        self_name="天泽",
        cooldown=0,
        quiet_window=0.01,
        contacts=contacts,
        raw={
            "max_replans": 2,
            "rate_limit": {"per_contact_per_hour": 100, "global_per_hour": 100},
            "task_memory": {"inactive_days": 2},
            "max_reply_chars": 16000,
            "monitor_interval_seconds": 0.01,
        },
        workspace_root=root,
        worktree_root=root,
        state_dir=root,
    )
    service.store = AuditStore(root / "audit.sqlite3")
    service.gate = SecurityGate(12000)
    service.dws = SimpleNamespace(
        history=AsyncMock(return_value=[]),
        streaming_cards=streaming_cards,
        send=AsyncMock(return_value=("sent-message-id", {})),
        send_card=AsyncMock(return_value=("card-biz", "card-message", {})),
        update_card=AsyncMock(),
        add_emoji=AsyncMock(),
        remove_emoji=AsyncMock(),
    )
    service.agent_runtime = SimpleNamespace(
        pipeline=("gate", "front", "worker", "deliver"),
        skill_pool={},
        has_stage=lambda name: False,
        stage_of_kind=lambda kind: None if kind == "triage" else "worker",
        executive_stage=Mock(return_value="worker"),
        supplement_strategy="steer",
        render_auto_message=Mock(return_value="收到，我在处理中。"),
        _stage=Mock(
            return_value=SimpleNamespace(
                name="worker",
                profile=SimpleNamespace(fallbacks=(), options={}),
            )
        ),
    )
    service.runtime_states = {}
    service.streaming_card_send_uuids = {}
    service.live_sessions = {}
    service.progress_card_seqs = {}
    service.queues = {}
    service.workers = {}
    service.configuration_lock = threading.RLock()
    service.global_slots = asyncio.Semaphore(1)
    service.outbox_wakeup = asyncio.Event()
    service._write_runtime = Mock()
    return service


def _event(
    message_id: str,
    text: str,
    *,
    contact: Contact = ALICE,
    conversation_id: str = "conversation-1",
    origin: str = "dm",
) -> IncomingEvent:
    return IncomingEvent(
        f"event-{message_id}",
        message_id,
        conversation_id,
        contact,
        text,
        NOW,
        origin=origin,
    )


async def _cancel_workers(service: AgentService) -> None:
    for worker in service.workers.values():
        worker.cancel()
    for worker in service.workers.values():
        try:
            await worker
        except asyncio.CancelledError:
            pass


class TerminalWakeTests(unittest.TestCase):
    def _waiting_pair(self, store: AuditStore) -> tuple[str, list[str]]:
        """任务 A 由 alice 推动；alice 与 bob 各有一个任务在等 A 终态。

        事项写入按联系人作用域隔离（跨联系人只共享黑板读），所以两个
        等待方是各自作用域里的两条任务。
        """
        alice_scope = TaskScope(SELF_OPEN_ID, ALICE.identity, "conversation-1")
        bob_scope = TaskScope(SELF_OPEN_ID, BOB.identity, "conversation-2")
        created_a = store.record_task(
            scope=alice_scope,
            goal="上线支付回调",
            evidence_at=NOW - timedelta(hours=1),
            now=NOW - timedelta(hours=1),
        )
        waiters = [
            store.record_task(
                scope=scope,
                goal=f"等回调上线后联调-{index}",
                status="waiting",
                waiting_on=f"task:{created_a['task']['id']}",
                evidence_at=NOW - timedelta(hours=1),
                now=NOW - timedelta(hours=1),
            )["task"]["id"]
            for index, scope in enumerate((alice_scope, bob_scope))
        ]
        return created_a["task"]["id"], waiters

    def _terminate(
        self, store: AuditStore, task_id: str, status: str, run_id: str
    ) -> None:
        store.register_run(
            run_id,
            contact_id=ALICE.identity,
            stage="worker",
            now=NOW - timedelta(minutes=5),
            origin="synthetic" if run_id.endswith("-syn") else "live",
        )
        resolved = store.record_task(
            scope=TaskScope(SELF_OPEN_ID, ALICE.identity, "conversation-1"),
            task_id=task_id,
            status=status,
            expected_version=1,
            run_id=run_id,
            evidence_at=NOW,
            now=NOW,
        )
        self.assertTrue(resolved["ok"])

    def test_completed_task_wakes_every_waiting_contact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = _service(Path(directory))
            task_a, _ = self._waiting_pair(service.store)
            self._terminate(service.store, task_a, "completed", "run-live")

            service._drain_terminal_events(NOW)

            wakes = service.store.due_wakes(NOW)
            self.assertEqual(
                {(wake["contact_id"], wake["text"]) for wake in wakes},
                {
                    (ALICE.identity, "任务完成：上线支付回调，结果见 board"),
                    (BOB.identity, "任务完成：上线支付回调，结果见 board"),
                },
            )
            self.assertTrue(all(w["origin_task_id"] == task_a for w in wakes))

            # 事件只排空一次：重复 drain 不重复唤醒。
            service._drain_terminal_events(NOW + timedelta(seconds=5))
            self.assertEqual(
                len(
                    service.store.due_wakes(
                        datetime.now(tz=ZoneInfo("Asia/Shanghai"))
                    )
                ),
                2,
            )
            service.store.close()
    def test_cancelled_task_wakes_with_cancel_summary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = _service(Path(directory))
            task_a, _ = self._waiting_pair(service.store)
            self._terminate(service.store, task_a, "cancelled", "run-live")

            service._drain_terminal_events(NOW)

            texts = {wake["text"] for wake in service.store.due_wakes(NOW)}
            self.assertEqual(
                texts, {"任务取消：上线支付回调，结果见 board"}
            )
            service.store.close()

    def test_synthetic_run_terminal_does_not_wake_again(self) -> None:
        # 回声切断：wake 合成轮（origin=synthetic）把任务推到终态时，
        # 不再对等待方 enqueue 新 wake，循环不会自我放大。
        with tempfile.TemporaryDirectory() as directory:
            service = _service(Path(directory))
            task_a, _ = self._waiting_pair(service.store)
            self._terminate(service.store, task_a, "completed", "run-syn")

            service._drain_terminal_events(NOW)

            self.assertEqual(service.store.due_wakes(NOW), [])
            service.store.close()

    def test_terminal_transition_without_status_change_is_no_event(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = _service(Path(directory))
            alice_scope = TaskScope(SELF_OPEN_ID, ALICE.identity, "conversation-1")
            created = service.store.record_task(
                scope=alice_scope, goal="只补事实", evidence_at=NOW, now=NOW
            )
            service.store.record_task(
                scope=alice_scope,
                task_id=created["task"]["id"],
                expected_version=1,
                facts=[{"text": "新事实", "anchor_message_id": ""}],
                evidence_at=NOW,
                now=NOW,
            )

            self.assertEqual(service.store.pending_terminal_events(), [])
            service.store.close()


class TimeWakeTests(unittest.TestCase):
    def _waiting_at(self, store: AuditStore, waiting_on: str) -> str:
        created = store.record_task(
            scope=TaskScope(SELF_OPEN_ID, ALICE.identity, "conversation-1"),
            goal="周四早上提醒验证",
            status="waiting",
            waiting_on=waiting_on,
            evidence_at=NOW - timedelta(hours=2),
            now=NOW - timedelta(hours=2),
        )
        task_id = created["task"]["id"]
        # 多人一事的 contact_ids 由触碰自动累积；写入按作用域隔离，
        # 这里直接落库两联系人，验证到点只唤醒主联系人（首位）。
        store.connection.execute(
            "UPDATE tasks SET contact_ids = ? WHERE id = ?",
            ('["user-bob", "user-alice"]', task_id),
        )
        store.connection.commit()
        return task_id

    def test_due_moment_inside_window_wakes_primary_contact_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = _service(Path(directory))
            self._waiting_at(service.store, "@2026-11-07 10:00")

            service._scan_time_wakes(datetime(2026, 11, 7, 9, 59, tzinfo=TZ))
            self.assertEqual(service.store.due_wakes(NOW), [])

            service._scan_time_wakes(datetime(2026, 11, 7, 10, 0, tzinfo=TZ))
            wakes = service.store.due_wakes(NOW)
            self.assertEqual(
                [(wake["contact_id"], wake["text"]) for wake in wakes],
                [(BOB.identity, "到点检查：周四早上提醒验证")],
            )

            # 同一等待只入队一次：重复扫描不重复唤醒。
            service._scan_time_wakes(datetime(2026, 11, 7, 10, 5, tzinfo=TZ))
            self.assertEqual(len(service.store.due_wakes(NOW)), 1)
            service.store.close()

    def test_evening_moment_defers_to_next_morning(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = _service(Path(directory))
            self._waiting_at(service.store, "@2026-11-07 20:00")

            service._scan_time_wakes(datetime(2026, 11, 7, 20, 5, tzinfo=TZ))
            service._scan_time_wakes(datetime(2026, 11, 8, 8, 59, tzinfo=TZ))
            self.assertEqual(service.store.due_wakes(NOW), [])

            service._scan_time_wakes(datetime(2026, 11, 8, 9, 0, tzinfo=TZ))
            self.assertEqual(
                [
                    wake["contact_id"]
                    for wake in service.store.due_wakes(
                        datetime(2026, 11, 8, 9, 0, tzinfo=TZ)
                    )
                ],
                [BOB.identity],
            )
            service.store.close()

    def test_early_moment_rolls_to_same_morning_window(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = _service(Path(directory))
            self._waiting_at(service.store, "@2026-11-07 06:00")

            service._scan_time_wakes(datetime(2026, 11, 7, 6, 30, tzinfo=TZ))
            self.assertEqual(service.store.due_wakes(NOW), [])

            service._scan_time_wakes(datetime(2026, 11, 7, 9, 0, tzinfo=TZ))
            self.assertEqual(len(service.store.due_wakes(NOW)), 1)
            service.store.close()

    def test_non_time_waiting_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = _service(Path(directory))
            self._waiting_at(service.store, "对方周四部署")

            service._scan_time_wakes(datetime(2026, 11, 9, 12, 0, tzinfo=TZ))

            self.assertEqual(service.store.due_wakes(NOW), [])
            service.store.close()

    def test_time_wake_dispatch_capped_at_three(self) -> None:
        """任务停在 waiting 且时刻已过：重发封顶 3 轮，绝不 5s 一轮烧钱。"""
        with tempfile.TemporaryDirectory() as directory:
            service = _service(Path(directory))
            task_id = self._waiting_at(service.store, "@2026-11-07 10:00")

            at = datetime(2026, 11, 7, 10, 0, tzinfo=TZ)
            for _ in range(6):
                service._scan_time_wakes(at)
                # 模拟派发循环：把 due wake 全部 claim+finish（无论成败，
                # 派发计数都应累计），任务保持 waiting 不闭环。
                for wake in service.store.due_wakes(at):
                    self.store = service.store
                    service.store.claim_wake(int(wake["id"]), now=at)
                    service.store.finish_wake(int(wake["id"]), now=at)

            self.assertEqual(
                service.store.wake_dispatch_count(task_id, "@2026-11-07 10:00"),
                3,
            )
            self.assertEqual(service.store.due_wakes(at), [])
            service.store.close()


class WakeDispatchTests(unittest.TestCase):
    def test_dispatch_turns_wake_into_synthetic_queue_event(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = _service(root)
            wake_id = service.store.enqueue_wake(
                ALICE.identity,
                "任务完成：上线支付回调，结果见 board",
                due_at=NOW,
                now=NOW,
            )
            wake = service.store.due_wakes(NOW)[0]

            async def scenario() -> None:
                await service._dispatch_wake(wake, NOW + timedelta(seconds=1))
                await _cancel_workers(service)

            asyncio.run(scenario())

            queue = service.queues[f"dm:{ALICE.identity}"]
            self.assertEqual(queue.qsize(), 1)
            event = queue.get_nowait()
            self.assertEqual(event.message_id, f"wake-{wake_id}")
            self.assertEqual(event.event_id, f"wake-{wake_id}")
            self.assertEqual(
                event.content, "任务完成：上线支付回调，结果见 board"
            )
            status = service.store.connection.execute(
                "SELECT status FROM events WHERE message_id = ?",
                (f"wake-{wake_id}",),
            ).fetchone()
            self.assertEqual(status["status"], "queued")
            lifecycle = service.store.connection.execute(
                "SELECT status, consumed_at, error FROM wakes WHERE id = ?",
                (wake_id,),
            ).fetchone()
            self.assertEqual(lifecycle["status"], "done")
            self.assertTrue(lifecycle["consumed_at"])
            self.assertEqual(lifecycle["error"], "")

            # 已完成的 wake 不会被再次派发。
            async def redispatch() -> None:
                await service._dispatch_wake(wake, NOW + timedelta(seconds=2))
                await _cancel_workers(service)

            asyncio.run(redispatch())
            # 已取走一条、重复派发被拒：队列不会出现第二条合成输入。
            self.assertEqual(queue.qsize(), 0)
            service.store.close()

    def test_dispatch_consumes_wake_for_unknown_contact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = _service(Path(directory))
            wake_id = service.store.enqueue_wake(
                "user-ghost", "到点检查：X", due_at=NOW, now=NOW
            )

            async def scenario() -> None:
                await service._dispatch_wake(
                    service.store.due_wakes(NOW)[0],
                    NOW + timedelta(seconds=1),
                )

            asyncio.run(scenario())

            self.assertEqual(service.queues, {})
            lifecycle = service.store.connection.execute(
                "SELECT status, error FROM wakes WHERE id = ?", (wake_id,)
            ).fetchone()
            # 联系人不在白名单：done + error 留痕，不无限重试。
            self.assertEqual(lifecycle["status"], "done")
            self.assertIn("contact_not_whitelisted", lifecycle["error"])
            service.store.close()

    def test_synthetic_events_never_enter_evidence_times(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = _service(Path(directory))
            batch = [
                _event("message-1", "接口好了吗"),
                _event("wake-3", "任务完成：X，结果见 board", origin="wake"),
                _event("panel-abc", "操作者指令", origin="panel"),
            ]

            context = service._task_context_for(batch, (), "run-1")

            self.assertEqual(
                set(context["message_times"]), {"message-1"}
            )
            service.store.close()


class _DoneFakeSession:
    """立即完成的会话替身：跳过监控循环，捕获不到任何异步交互。"""

    def __init__(self) -> None:
        self.prepared = SimpleNamespace(timeout_seconds=30)
        self.done = True
        self.silent = False
        self.error = ""
        self.escalated = False
        self.blocked = False
        self.exit_code = 0
        self.steer_prompts: list[str] = []

    async def start(self) -> None:
        return None

    async def wait_for_update(self, timeout: float | None = None) -> None:
        return None

    async def steer(self, prompt: str) -> bool:
        self.steer_prompts.append(prompt)
        return True

    def result(self) -> str:
        return "最终正文"

    async def abort(self) -> None:
        return None

    async def close(self) -> None:
        return None


class SyntheticRunPromptTests(unittest.TestCase):
    def test_wake_round_registers_synthetic_and_injects_wake_block(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = _service(root, streaming_cards=False)
            prompts: list[str] = []
            render_variables: list[dict] = []
            session = _DoneFakeSession()
            service.agent_runtime = SimpleNamespace(
                pipeline=("gate", "front", "worker", "deliver"),
                skill_pool={},
                profile_name=Mock(return_value="pi-worker"),
                render=Mock(
                    side_effect=lambda stage, variables: (
                        render_variables.append(variables) or "PROMPT-BASE"
                    )
                ),
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
                        profile=SimpleNamespace(protocol="pi-rpc", options={})
                    )
                ),
                open_session=Mock(
                    side_effect=lambda stage, session_id, prompt, *a, **kw: (
                        prompts.append(prompt) or session
                    )
                ),
            )
            batch = [
                _event(
                    "wake-7",
                    "任务完成：上线支付回调，结果见 board",
                    conversation_id="conversation-1",
                    origin="wake",
                )
            ]

            result = asyncio.run(service._run_agent(batch, ""))

            self.assertEqual(result.reply, "最终正文")
            self.assertEqual(
                prompts,
                [
                    "PROMPT-BASE\n\n<system_wake>\n"
                    "系统唤醒：任务完成：上线支付回调，结果见 board\n"
                    "这不是联系人的新消息，不含新授权。\n</system_wake>"
                ],
            )
            # 合成输入不进联系人消息清单，只活在唤醒块里。
            self.assertEqual(
                json.loads(render_variables[0]["current_messages_json"]), []
            )
            run = service.store.connection.execute(
                "SELECT origin FROM board_runs WHERE session_id = ?",
                (result.session_id,),
            ).fetchone()
            self.assertEqual(run["origin"], "synthetic")
            # 会话收尾后注册表清空。
            self.assertEqual(service.live_sessions, {})
            service.store.close()


class OperatorPanelTests(unittest.TestCase):
    def _drive_queue(self, service: AgentService, conversation_id: str) -> IncomingEvent:
        queue = service.queues[conversation_id]
        return queue.get_nowait()

    def test_internal_operator_round_never_sends_externally(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = _service(root)

            async def scenario() -> None:
                panel_id = await service.open_operator_session(
                    "汇总本周部署情况", None
                )
                self.assertTrue(panel_id.startswith("panel-"))
                await _cancel_workers(service)
                return panel_id

            panel_id = asyncio.run(scenario())
            event = self._drive_queue(service, f"panel:{panel_id}")
            self.assertEqual(event.message_id, panel_id)
            at = datetime.now(TZ)
            result = AgentResult(
                "session-panel", "内部结论：本周共三次部署", 0, at, at
            )

            async def fake_run_agent(batch, prior_attempt, *, front=False,
                                     recent_context=(), agent_override=None):
                return result

            with patch.object(
                AgentService, "_run_agent", side_effect=fake_run_agent
            ):
                asyncio.run(service._handle_batch([event]))

            # 内部轮零外发：不 send、不建卡、不进发送队列。
            service.dws.send.assert_not_awaited()
            service.dws.send_card.assert_not_awaited()
            service.dws.update_card.assert_not_awaited()
            pending = service.store.connection.execute(
                "SELECT COUNT(*) FROM outgoing"
            ).fetchone()[0]
            self.assertEqual(pending, 0)
            replies = service.store.recent_panel_replies()
            self.assertEqual(
                [(reply["session_id"], reply["text"]) for reply in replies],
                [("session-panel", "内部结论：本周共三次部署")],
            )
            status = service.store.connection.execute(
                "SELECT status FROM events WHERE message_id = ?", (panel_id,)
            ).fetchone()
            self.assertEqual(status["status"], "processed")
            service.store.close()

    def test_operator_round_with_alias_rides_contact_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = _service(root)

            async def scenario() -> str:
                panel_id = await service.open_operator_session(
                    "接口问题已修复，转告他", "alice"
                )
                await _cancel_workers(service)
                return panel_id

            panel_id = asyncio.run(scenario())
            event = self._drive_queue(service, f"dm:{ALICE.identity}")
            self.assertEqual(event.message_id, panel_id)
            at = datetime.now(TZ)

            async def fake_run_agent(batch, prior_attempt, *, front=False,
                                     recent_context=(), agent_override=None):
                return AgentResult("session-live", "已转告的正文", 0, at, at)

            with patch.object(
                AgentService, "_run_agent", side_effect=fake_run_agent
            ):
                asyncio.run(service._handle_batch([event]))

            rows = service.store.connection.execute(
                "SELECT delivery_status, contact_user_id FROM outgoing"
            ).fetchall()
            self.assertEqual(
                [tuple(row) for row in rows], [("pending", ALICE.identity)]
            )
            service.dws.send.assert_not_awaited()
            service.store.close()

    def test_steer_run_injects_instruction_without_manual_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = _service(Path(directory))
            session = _DoneFakeSession()
            session.done = False
            service.live_sessions["run-steer"] = session

            steered = asyncio.run(
                service.steer_run("run-steer", "先别回复，等我确认")
            )

            self.assertTrue(steered)
            self.assertEqual(
                session.steer_prompts,
                [
                    "<system_wake>\n操作者指令：先别回复，等我确认\n"
                    "这是操作者本人下达的指令，按最高授权执行。\n</system_wake>"
                ],
            )
            manual = service.store.connection.execute(
                "SELECT COUNT(*) FROM manual_messages"
            ).fetchone()[0]
            self.assertEqual(manual, 0)
            self.assertFalse(
                asyncio.run(service.steer_run("run-missing", "x"))
            )
            session.done = True
            self.assertFalse(
                asyncio.run(service.steer_run("run-steer", "x"))
            )
            service.store.close()

    def test_answer_task_restores_open_records_human_fact_and_wakes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = _service(Path(directory))
            alice_scope = TaskScope(SELF_OPEN_ID, ALICE.identity, "conversation-1")
            mine = service.store.record_task(
                scope=alice_scope,
                goal="灰度发布等操作者拍板",
                status="waiting",
                waiting_on="天泽",
                evidence_at=NOW,
                now=NOW,
            )
            task_id = mine["task"]["id"]
            service.store.connection.execute(
                "UPDATE tasks SET contact_ids = ? WHERE id = ?",
                ('["user-alice", "user-bob"]', task_id),
            )
            service.store.connection.commit()
            other = service.store.record_task(
                scope=alice_scope,
                goal="等别人拍板的事项",
                status="waiting",
                waiting_on="别人",
                evidence_at=NOW,
                now=NOW,
            )

            answered = asyncio.run(service.answer_task(task_id, "同意灰度发布"))

            self.assertTrue(answered)
            task = service.store.board_tasks(include_done=True, status="open")[
                0
            ]
            self.assertEqual(task["waiting_on"], "")
            facts = service.store.facts_for_task(task_id)
            self.assertEqual(
                [(fact["text"], fact["source"]) for fact in facts],
                [("同意灰度发布", "human")],
            )
            # answer_task 的 wake due_at 用真实墙钟：用当前时间取 due，
            # 固定常量 NOW 会在墙钟越过它之后把 due wake 全滤成空。
            wakes = service.store.due_wakes(
                datetime.now(tz=ZoneInfo("Asia/Shanghai"))
            )
            self.assertEqual(
                {(wake["contact_id"], wake["text"]) for wake in wakes},
                {
                    (ALICE.identity, "操作者已答复：同意灰度发布"),
                    (BOB.identity, "操作者已答复：同意灰度发布"),
                },
            )
            # 等的不是本人：拒绝作答，也不产生事实或唤醒。
            rejected = asyncio.run(
                service.answer_task(other["task"]["id"], "越权作答")
            )
            self.assertFalse(rejected)
            self.assertEqual(
                service.store.facts_for_task(other["task"]["id"]), []
            )
            self.assertEqual(
                len(
                    service.store.due_wakes(
                        datetime.now(tz=ZoneInfo("Asia/Shanghai"))
                    )
                ),
                2,
            )
            service.store.close()

    def test_board_snapshot_projects_waiting_operator_and_runs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = _service(Path(directory))
            alice_scope = TaskScope(SELF_OPEN_ID, ALICE.identity, "conversation-1")
            for_me = service.store.record_task(
                scope=alice_scope, goal="等我拍板", status="waiting",
                waiting_on="天泽", evidence_at=NOW, now=NOW,
            )
            service.store.record_task(
                scope=alice_scope, goal="等对方", status="waiting",
                waiting_on="对方", evidence_at=NOW, now=NOW,
            )
            service.store.register_run(
                "run-board", contact_id=ALICE.identity, stage="worker",
                headline="正在验证", now=NOW,
            )
            service.store.add_progress(
                "run-board", "复现了问题", now=NOW
            )
            done = service.store.record_task(
                scope=alice_scope, goal="已完成事项", status="completed",
                evidence_at=NOW, now=NOW,
            )
            self.assertTrue(done["ok"])

            snapshot = service.board_snapshot()

            self.assertEqual(
                [run["session_id"] for run in snapshot["runs"]], ["run-board"]
            )
            self.assertEqual(
                [entry["text"] for entry in snapshot["runs"][0]["recent_progress"]],
                ["复现了问题"],
            )
            self.assertEqual(
                [task["id"] for task in snapshot["waiting_operator"]],
                [for_me["task"]["id"]],
            )
            self.assertEqual(
                [task["goal"] for task in snapshot["recent_done"]],
                ["已完成事项"],
            )
            goals = {task["goal"] for task in snapshot["tasks"]}
            self.assertEqual(goals, {"等我拍板", "等对方", "已完成事项"})
            service.store.close()


class TwoPhaseCardTests(unittest.TestCase):
    def test_phase_one_card_shows_only_milestone_list(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = _service(root)
            service.store.register_run(
                "run-card", contact_id=ALICE.identity, stage="worker", now=NOW
            )
            service.store.add_progress("run-card", "定位到脚本", now=NOW)
            service.store.add_progress("run-card", "跑通回归", now=NOW)
            batch = [_event("message-1", "接口好了吗")]

            asyncio.run(service._sync_progress_card(batch, "run-card"))
            asyncio.run(service._sync_progress_card(batch, "run-card"))

            # 里程碑全量幂等渲染：第二次同步复用活跃卡片，不再建新卡。
            service.dws.send_card.assert_awaited_once()
            bodies = [
                call.args[1]
                for call in service.dws.update_card.await_args_list
                if call.args[2] == 2
            ]
            self.assertIn("- 定位到脚本\n- 跑通回归", bodies)

            service.store.add_progress("run-card", "清理完成", now=NOW)
            asyncio.run(service._sync_progress_card(batch, "run-card"))
            service.dws.send_card.assert_awaited_once()
            bodies = [
                call.args[1]
                for call in service.dws.update_card.await_args_list
                if call.args[2] == 2
            ]
            self.assertIn(
                "- 定位到脚本\n- 跑通回归\n- 清理完成", bodies
            )
            # 模型 delta 碎片永不进正文：相1正文只有里程碑列表（含建卡
            # 时的处理编号行），没有任何思考/增量文本。
            for body in bodies:
                self.assertTrue(
                    body.startswith("- ") or "处理编号" in body,
                    body,
                )
            service.store.close()

    def test_no_progress_keeps_card_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = _service(Path(directory))
            service.store.register_run(
                "run-quiet", contact_id=ALICE.identity, stage="worker", now=NOW
            )

            asyncio.run(
                service._sync_progress_card(
                    [_event("message-1", "接口好了吗")], "run-quiet"
                )
            )

            service.dws.send_card.assert_not_awaited()
            service.dws.update_card.assert_not_awaited()
            service.store.close()

    def test_phase_two_delivery_replaces_card_body_wholesale(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = _service(root)
            service.store.register_run(
                "run-card", contact_id=ALICE.identity, stage="worker", now=NOW
            )
            service.store.add_progress("run-card", "定位到脚本", now=NOW)
            source = _event("message-1", "接口好了吗")
            asyncio.run(service._sync_progress_card([source], "run-card"))

            service.store.enqueue_delivery(
                "send-final",
                source,
                "最终正文：接口已验证通过",
                "run-card",
                event_ids=["message-1"],
            )
            asyncio.run(service._dispatch_due_delivery())

            final_calls = [
                call
                for call in service.dws.update_card.await_args_list
                if call.args[2] == 3
            ]
            self.assertEqual(
                [(call.args[0], call.args[1]) for call in final_calls],
                [("card-biz", "最终正文：接口已验证通过")],
            )
            row = service.store.connection.execute(
                "SELECT delivery_status, card_biz_id FROM outgoing"
                " WHERE send_uuid = 'send-final'"
            ).fetchone()
            self.assertEqual(tuple(row), ("sent", "card-biz"))
            service.store.close()


class TerminalFanoutAtomicityTests(unittest.TestCase):
    """P0-6：终态事件 -> wakes 单事务，崩溃重试恰好一次、无半提交。"""

    def _waiting_pair(self, service: AgentService) -> str:
        alice_scope = TaskScope(SELF_OPEN_ID, ALICE.identity, "conversation-1")
        bob_scope = TaskScope(SELF_OPEN_ID, BOB.identity, "conversation-2")
        created = service.store.record_task(
            scope=alice_scope,
            goal="上线支付回调",
            evidence_at=NOW - timedelta(hours=1),
            now=NOW - timedelta(hours=1),
        )
        task_id = created["task"]["id"]
        for index, scope in enumerate((alice_scope, bob_scope)):
            service.store.record_task(
                scope=scope,
                goal=f"等回调上线后联调-{index}",
                status="waiting",
                waiting_on=f"task:{task_id}",
                evidence_at=NOW - timedelta(hours=1),
                now=NOW - timedelta(hours=1),
            )
        resolved = service.store.record_task(
            scope=alice_scope,
            task_id=task_id,
            status="completed",
            expected_version=1,
            run_id="run-live",
            evidence_at=NOW,
            now=NOW,
        )
        self.assertTrue(resolved["ok"])
        return task_id

    def test_crash_mid_fanout_rolls_back_and_retry_wakes_exactly_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = _service(Path(directory))
            task_id = self._waiting_pair(service)
            event = service.store.pending_terminal_events()[0]
            original = AuditStore.enqueue_fanout_wake_tx
            injected = {"count": 0}

            def crash_after_first_insert(
                store: AuditStore, *args: object, **kwargs: object
            ) -> int:
                injected["count"] += 1
                if injected["count"] == 2:
                    # 第一个等待方的 wake 已插入：此刻崩溃，事务必须整体
                    # 回滚，不能留下半提交。
                    raise RuntimeError("injected crash after first insert")
                return original(store, *args, **kwargs)  # type: ignore[arg-type]

            with patch.object(
                AuditStore, "enqueue_fanout_wake_tx", crash_after_first_insert
            ):
                with self.assertRaises(RuntimeError):
                    service.store.fan_out_terminal_event(
                        int(event["id"]), now=NOW
                    )

            # 无半提交：一条 wake 都没留下，事件仍待处理。
            self.assertEqual(service.store.due_wakes(NOW), [])
            self.assertEqual(len(service.store.pending_terminal_events()), 1)

            # 重试：两个等待方各恰好一条 wake，事件收口。
            retry_at = NOW + timedelta(seconds=5)
            service._drain_terminal_events(retry_at)
            wakes = service.store.due_wakes(retry_at)
            self.assertEqual(len(wakes), 2)
            waiters = {
                (row["scope_contact"], row["id"])
                for row in service.store.connection.execute(
                    "SELECT id, scope_contact FROM tasks WHERE waiting_on = ?",
                    (f"task:{task_id}",),
                )
            }
            self.assertEqual(
                {(wake["contact_id"], wake["waiting_task_id"]) for wake in wakes},
                waiters,
            )
            self.assertEqual(service.store.pending_terminal_events(), [])
            service._drain_terminal_events(NOW + timedelta(seconds=10))
            self.assertEqual(
                len(service.store.due_wakes(NOW + timedelta(seconds=10))), 2
            )
            service.store.close()

    def test_execute_request_chain_marks_synthetic_terminal_without_echo(self) -> None:
        # P0-5：经 task_tools.execute_request 全链驱动 synthetic 轮终态，
        # run_id 必须落进 task_terminal_events，回声切断才不失效。
        with tempfile.TemporaryDirectory() as directory:
            service = _service(Path(directory))
            alice_scope = TaskScope(SELF_OPEN_ID, ALICE.identity, "conversation-1")
            created = service.store.record_task(
                scope=alice_scope,
                goal="上线支付回调",
                evidence_at=NOW - timedelta(hours=1),
                now=NOW - timedelta(hours=1),
            )
            task_id = created["task"]["id"]
            service.store.record_task(
                scope=TaskScope(SELF_OPEN_ID, BOB.identity, "conversation-2"),
                goal="等回调上线后联调",
                status="waiting",
                waiting_on=f"task:{task_id}",
                evidence_at=NOW - timedelta(hours=1),
                now=NOW - timedelta(hours=1),
            )
            service.store.register_run(
                "dm-20261107-pi-worker-abc123synthetic",
                contact_id=ALICE.identity,
                stage="worker",
                now=NOW - timedelta(minutes=5),
                origin="synthetic",
            )

            response = task_tools.execute_request(
                {
                    "tool": "task_record",
                    "tool_call_id": "call-complete",
                    "args": {
                        "task_id": task_id,
                        "status": "completed",
                        "expected_version": 1,
                    },
                },
                {
                    "db_path": str(service.store.path),
                    "self_id": SELF_OPEN_ID,
                    "contact_id": ALICE.identity,
                    "conversation_id": "conversation-1",
                    "run_id": "dm-20261107-pi-worker-abc123synthetic",
                    "message_times": {},
                },
            )

            self.assertTrue(response["ok"], response)
            run_id = service.store.connection.execute(
                "SELECT run_id FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()["run_id"]
            self.assertEqual(run_id, "dm-20261107-pi-worker-abc123synthetic")
            event = service.store.pending_terminal_events()[0]
            self.assertEqual(event["run_id"], "dm-20261107-pi-worker-abc123synthetic")

            # synthetic 轮终态：等待方不会被再次唤醒。
            service._drain_terminal_events(NOW)
            self.assertEqual(service.store.due_wakes(NOW), [])
            self.assertEqual(service.store.pending_terminal_events(), [])
            service.store.close()


class WakeLifecycleTests(unittest.TestCase):
    """P0-7：claim -> done 生命周期与崩溃重放。"""

    def test_claim_hides_wake_from_due_and_finish_closes_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = _service(Path(directory))
            wake_id = service.store.enqueue_wake(
                ALICE.identity, "到点检查：X", due_at=NOW, now=NOW
            )

            self.assertTrue(service.store.claim_wake(wake_id, now=NOW))
            self.assertFalse(service.store.claim_wake(wake_id, now=NOW))
            self.assertEqual(service.store.due_wakes(NOW), [])

            service.store.finish_wake(wake_id, now=NOW, error="boom")
            row = service.store.connection.execute(
                "SELECT status, error, consumed_at FROM wakes WHERE id = ?",
                (wake_id,),
            ).fetchone()
            self.assertEqual(row["status"], "done")
            self.assertEqual(row["error"], "boom")
            service.store.close()

    def test_stale_claim_is_replayed_on_startup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = _service(Path(directory))
            wake_id = service.store.enqueue_wake(
                ALICE.identity, "任务完成：X，结果见 board",
                due_at=NOW, now=NOW,
            )
            # 崩溃前刚好 claim：30 分钟内不重放（可能是活着的服务在处理）。
            self.assertTrue(
                service.store.claim_wake(wake_id, now=NOW + timedelta(minutes=1))
            )
            replayed = service.store.recover_stale_wakes(
                NOW + timedelta(minutes=2), max_claim_age=timedelta(minutes=30)
            )
            self.assertEqual(replayed, 0)
            self.assertEqual(service.store.due_wakes(NOW), [])

            # 超过 30 分钟仍未收口：回置 pending，启动后重放。
            replayed = service.store.recover_stale_wakes(
                NOW + timedelta(minutes=32), max_claim_age=timedelta(minutes=30)
            )
            self.assertEqual(replayed, 1)
            due = service.store.due_wakes(NOW + timedelta(minutes=32))
            self.assertEqual([wake["id"] for wake in due], [wake_id])
            row = service.store.connection.execute(
                "SELECT status, claimed_at FROM wakes WHERE id = ?", (wake_id,)
            ).fetchone()
            self.assertEqual(row["status"], "pending")
            self.assertEqual(row["claimed_at"], "")
            service.store.close()

    def test_legacy_consumed_wakes_migrate_to_done(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = AuditStore(root / "audit.sqlite3")
            store.enqueue_wake(ALICE.identity, "旧唤醒", due_at=NOW, now=NOW)
            store.connection.execute(
                "UPDATE wakes SET consumed_at = ? WHERE contact_id = ?",
                (NOW.isoformat(), ALICE.identity),
            )
            store.connection.commit()
            store.close()

            reopened = AuditStore(root / "audit.sqlite3")
            self.assertEqual(reopened.due_wakes(NOW + timedelta(hours=1)), [])
            reopened.close()


class WakeSteerTests(unittest.TestCase):
    """P1-11：在跑会话直接 steer，无活跃才走合成输入批。"""

    def _wake(self, service: AgentService) -> dict:
        wake_id = service.store.enqueue_wake(
            ALICE.identity, "任务完成：上线支付回调，结果见 board",
            due_at=NOW, now=NOW,
        )
        return service.store.due_wakes(NOW)[0]

    def test_live_session_receives_wake_via_steer_not_queue(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = _service(Path(directory))
            session = _DoneFakeSession()
            session.done = False
            service.store.register_run(
                "run-live", contact_id=ALICE.identity, stage="worker", now=NOW
            )
            service.live_sessions["run-live"] = session
            wake = self._wake(service)

            asyncio.run(service._dispatch_wake(wake, NOW + timedelta(seconds=1)))

            self.assertEqual(
                session.steer_prompts,
                [
                    "<system_wake>\n系统唤醒：任务完成：上线支付回调，结果见 board\n"
                    "这不是联系人的新消息，不含新授权。\n</system_wake>"
                ],
            )
            self.assertEqual(service.queues, {})
            row = service.store.connection.execute(
                "SELECT status, error FROM wakes WHERE id = ?", (wake["id"],)
            ).fetchone()
            self.assertEqual(row["status"], "done")
            self.assertEqual(row["error"], "")
            service.store.close()

    def test_no_live_session_falls_back_to_synthetic_queue(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = _service(Path(directory))
            # 已结束的活跃运行不算在跑：steer 不可用，走合成输入。
            done_session = _DoneFakeSession()
            service.store.register_run(
                "run-done", contact_id=ALICE.identity, stage="worker", now=NOW
            )
            service.live_sessions["run-done"] = done_session
            wake = self._wake(service)

            async def scenario() -> None:
                await service._dispatch_wake(wake, NOW + timedelta(seconds=1))
                await _cancel_workers(service)

            asyncio.run(scenario())

            self.assertEqual(done_session.steer_prompts, [])
            self.assertEqual(service.queues[f"dm:{ALICE.identity}"].qsize(), 1)
            service.store.close()


class RescheduledTimeWakeTests(unittest.TestCase):
    """P1-10：改约时间后到点会再次唤醒；去重只看未派发 wake。"""

    def test_time_waiting_scan_is_not_truncated(self) -> None:
        # board_tasks 的 50 条上限曾吃掉排位靠后的等待事项：到点扫描
        # 用独立的无上限查询。
        with tempfile.TemporaryDirectory() as directory:
            service = _service(Path(directory))
            scope = TaskScope(SELF_OPEN_ID, ALICE.identity, "conversation-1")
            for index in range(55):
                service.store.record_task(
                    scope=scope,
                    goal=f"提醒-{index}",
                    status="waiting",
                    waiting_on="@2026-11-08 09:00",
                    evidence_at=NOW - timedelta(hours=3),
                    now=NOW - timedelta(hours=3),
                )

            self.assertEqual(len(service.store.time_waiting_tasks()), 55)
            service.store.close()

    def test_rescheduled_moment_wakes_again_after_first_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = _service(Path(directory))
            scope = TaskScope(SELF_OPEN_ID, ALICE.identity, "conversation-1")
            created = service.store.record_task(
                scope=scope,
                goal="周四早上提醒验证",
                status="waiting",
                waiting_on="@2026-11-07 10:00",
                evidence_at=NOW - timedelta(hours=2),
                now=NOW - timedelta(hours=2),
            )
            task_id = created["task"]["id"]

            service._scan_time_wakes(datetime(2026, 11, 7, 10, 0, tzinfo=TZ))
            first = service.store.due_wakes(NOW)
            self.assertEqual(len(first), 1)

            # 派发完成（done）后改约到 11:00。
            self.assertTrue(service.store.claim_wake(first[0]["id"], now=NOW))
            service.store.finish_wake(first[0]["id"], now=NOW)
            rescheduled = service.store.record_task(
                scope=scope,
                task_id=task_id,
                waiting_on="@2026-11-07 11:00",
                expected_version=1,
                evidence_at=datetime(2026, 11, 7, 10, 30, tzinfo=TZ),
                now=datetime(2026, 11, 7, 10, 30, tzinfo=TZ),
            )
            self.assertTrue(rescheduled["ok"])

            service._scan_time_wakes(datetime(2026, 11, 7, 11, 0, tzinfo=TZ))
            second = service.store.due_wakes(datetime(2026, 11, 7, 11, 0, tzinfo=TZ))
            self.assertEqual(
                [(wake["id"], wake["waiting_on"]) for wake in second],
                [(first[0]["id"] + 1, "@2026-11-07 11:00")],
            )

            # 同一 waiting_on 的 pending wake 不会重复入队。
            service._scan_time_wakes(datetime(2026, 11, 7, 11, 5, tzinfo=TZ))
            self.assertEqual(
                len(service.store.due_wakes(datetime(2026, 11, 7, 11, 5, tzinfo=TZ))),
                1,
            )
            service.store.close()


class BoardOutcomeMappingTests(unittest.TestCase):
    """P1-8：失败 outcome 统一映射为 board status='failed'。"""

    def test_service_error_run_lands_as_failed_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = _service(Path(directory))
            service.store.register_run(
                "run-err", contact_id=ALICE.identity, stage="worker", now=NOW
            )
            batch = [_event("message-1", "接口好了吗")]
            at = datetime.now(TZ)
            result = AgentResult("run-err", None, 1, at, at, error="agent crashed")

            service._record_agent_result(batch, result, status="error")

            row = service.store.connection.execute(
                "SELECT status, outcome FROM board_runs WHERE session_id = 'run-err'"
            ).fetchone()
            self.assertEqual(tuple(row), ("failed", "failed"))
            service.store.close()


class ActiveTasksFactsTests(unittest.TestCase):
    """P1-9：活跃事项快照与面板同源，带最近事实（含 source）。"""

    def test_prompt_variables_carry_recent_facts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = _service(Path(directory))
            service.store.record_task(
                scope=TaskScope(SELF_OPEN_ID, ALICE.identity, "conversation-1"),
                goal="排查线上 502",
                evidence_at=NOW,
                now=NOW,
                facts=[{"text": "网关白名单已扩容", "source": "agent"}],
            )
            stage = SimpleNamespace(name="worker", profile=SimpleNamespace(options={}))
            service.agent_runtime = SimpleNamespace(
                pipeline=("gate", "front", "worker", "deliver"),
                skill_pool={},
                profile_name=Mock(return_value="pi-worker"),
                _stage=Mock(return_value=stage),
            )
            batch = [_event("message-2", "502 好了吗")]

            variables = service._prompt_variables(
                "worker", "sess-1", batch, "", ()
            )

            tasks = json.loads(variables["active_tasks_json"])
            self.assertEqual(len(tasks), 1)
            self.assertEqual(
                [(fact["text"], fact["source"]) for fact in tasks[0]["recent_facts"]],
                [("网关白名单已扩容", "agent")],
            )
            service.store.close()


class PanelProgressIsolationTests(unittest.TestCase):
    """P1-12：面板内部轮的 progress 全程零 DWS 发送调用。"""

    def test_panel_round_progress_never_touches_dws(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = _service(Path(directory))
            service.store.register_run(
                "run-panel", contact_id="operator", stage="worker", now=NOW
            )
            service.store.add_progress("run-panel", "内部巡检完成一半", now=NOW)
            batch = [
                _event(
                    "panel-abc123def456",
                    "汇总本周部署情况",
                    conversation_id="panel:panel-abc123def456",
                    contact=Contact(
                        "operator", "天泽", "", SELF_OPEN_ID
                    ),
                    origin="panel",
                )
            ]

            asyncio.run(service._sync_progress_card(batch, "run-panel"))

            service.dws.send_card.assert_not_awaited()
            service.dws.update_card.assert_not_awaited()
            service.dws.send.assert_not_awaited()
            service.store.close()


class OperatorActionAuditTests(unittest.TestCase):
    """P1-13：操作者 steer/开内部轮落 wakes 审计行。"""

    def _audit_rows(self, store: AuditStore) -> list[tuple]:
        return [
            tuple(row)
            for row in store.connection.execute(
                "SELECT contact_id, text, status, run_session_id"
                " FROM wakes WHERE contact_id = 'operator'"
                " ORDER BY id"
            )
        ]

    def test_steer_and_open_session_leave_audit_trail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = _service(Path(directory))
            session = _DoneFakeSession()
            session.done = False
            service.live_sessions["run-steer"] = session

            async def scenario() -> str:
                steered = await service.steer_run("run-steer", "先查日志再回")
                self.assertTrue(steered)
                panel_id = await service.open_operator_session(
                    "汇总本周部署情况", None
                )
                await _cancel_workers(service)
                return panel_id

            panel_id = asyncio.run(scenario())

            self.assertEqual(
                self._audit_rows(service.store),
                [
                    ("operator", "先查日志再回", "done", "run-steer"),
                    ("operator", "汇总本周部署情况", "done", panel_id),
                ],
            )
            # 审计行是 done 态：不进派发队列。
            self.assertEqual(service.store.due_wakes(NOW), [])
            service.store.close()


if __name__ == "__main__":
    unittest.main()