from __future__ import annotations

import asyncio
import io
import json
import tempfile
import unittest
from http.client import BadStatusLine, IncompleteRead
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import test_service as fixtures
from dws_dm_agent import jev
from dws_dm_agent.core import IncomingEvent, HistoryMessage, TaskScope
from dws_dm_agent.runners import task_tools
from dws_dm_agent.service import AgentResult, AgentService


CONFIG = {"enabled": True, "model": "jev-1.14.0", "api_key": "test-key"}
ACK = {"status": "ok", "message_action": "ack_only", "confidence": 1.0}


class TransportTests(unittest.TestCase):
    def response(self, **changes):
        answer = {"type": "choice", "choice": "notification", "confidence": 0.95,
                  "probabilities": {key: float(key == "notification") for key in jev.ACTIONS}}
        answer.update(changes)
        return io.BytesIO(json.dumps({"answers": {"message_action": answer}}).encode())

    def test_request_and_low_confidence(self):
        state = {"current_messages": [{"text": "已部署"}], "active_tasks": [{"goal": "部署后验证"}]}
        with patch("urllib.request.urlopen", return_value=self.response(confidence=0.6)) as call:
            result = asyncio.run(jev.advise(CONFIG, state))
        request = call.call_args.args[0]
        self.assertEqual(json.loads(request.data)["state"], state)
        self.assertEqual(request.get_header("Authorization"), "Bearer test-key")
        self.assertEqual(call.call_args.kwargs["timeout"], 5)
        self.assertEqual(result["message_action"], "ambiguous")
        self.assertEqual(result["status"], "uncertain")
        self.assertNotIn("test-key", jev.prompt_advice(result))

    def test_invalid_distributions_do_not_become_advice(self):
        for changes in ({"choice": "no_reply"}, {"confidence": True}, {"confidence": float("nan")},
                        {"probabilities": {}}, {"probabilities": {k: 1 for k in jev.ACTIONS}}):
            with self.subTest(changes=changes), patch("urllib.request.urlopen", return_value=self.response(**changes)):
                self.assertEqual(asyncio.run(jev.advise(CONFIG, {}))["status"], "unavailable")

    def test_outage_and_timeout_do_not_raise(self):
        for error in (OSError("offline"), TimeoutError(), ValueError("invalid json"),
                      IncompleteRead(b"partial"), BadStatusLine("bad")):
            with self.subTest(error=error), patch.object(jev, "request_advice", side_effect=error):
                self.assertEqual(asyncio.run(jev.advise(CONFIG, {}))["message_action"], "ambiguous")

    def test_cancellation_propagates(self):
        def cancel(coro, **kwargs):
            coro.close()
            raise asyncio.CancelledError
        with patch("asyncio.wait_for", side_effect=cancel):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(jev.advise(CONFIG, {}))

    def test_configuration(self):
        jev.validate_config({})
        jev.validate_config(CONFIG)
        for invalid in (None, {"enabled": "false"}, {"enabled": True}, {**CONFIG, "model": ""}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                jev.validate_config(invalid)


class MemoryIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = fixtures.SilentOutcomeTests()._service(Path(self.temp.name), has_front=True)
        self.addCleanup(self.service.store.close)
        self.service.settings.raw["jev"] = dict(CONFIG)
        self.service.agent_runtime.profile_name = lambda stage: "pi-test"
        self.now = datetime.now(fixtures.TZ)
        self.event = IncomingEvent("event", "notice", "conversation", fixtures.CONTACT,
                                   "接口已经联调完成，不用回复。", self.now)
        self.service.store.claim_event(self.event)
        self.scope = TaskScope("self-open-id", fixtures.CONTACT.identity, "conversation")
        self.task = self.service.store.record_task(scope=self.scope, goal="等接口联调完成",
            status="waiting", waiting_on="对方结果", evidence_at=self.now - timedelta(hours=1), now=self.now)["task"]

    def record(self, batch, run_id, **changes):
        self.service.store.register_task_run(run_id, self.now)
        response = task_tools.execute_request({"tool": "task_record", "tool_call_id": "update",
            "args": {"task_id": self.task["id"], "expected_version": self.task["version"],
                     "evidence_message_ids": [batch[-1].message_id], **changes}},
            self.service._task_context_for(batch, (), run_id))
        self.assertTrue(response["ok"], response)
        self.task = response["task"]
        self.service.store.revoke_task_run(run_id, "finished", self.now)

    def test_confident_ack_still_records_completion_before_silence(self):
        async def front(batch, prior, **kwargs):
            self.assertEqual(kwargs["jev_advice"], ACK)
            self.assertEqual(batch[0].content, self.event.content)
            self.record(batch, "front", status="completed", evidence_summary="对方确认联调完成")
            return AgentResult("front", None, 0, self.now, self.now, silent=True)

        with patch("dws_dm_agent.service.jev_advise", new=AsyncMock(return_value=ACK)) as advise, \
                patch.object(self.service, "_run_agent", side_effect=front) as run:
            asyncio.run(self.service._handle_batch([self.event]))
        self.assertEqual(run.call_count, 1)
        self.assertEqual(advise.await_count, 1)
        state = advise.call_args.args[1]
        self.assertEqual(state["active_tasks"][0]["id"], self.task["id"])
        self.assertTrue(state["task_snapshot_is_partial"])
        self.assertEqual(self.task["status"], "completed")
        self.assertFalse(self.service.store.event_is_pending("notice"))
        self.assertEqual(self.service.store.connection.execute("SELECT COUNT(*) FROM outgoing").fetchone()[0], 0)

    def test_front_updates_are_visible_to_worker_and_advice_is_reused(self):
        calls = []
        async def agent(batch, prior, *, front=False, **kwargs):
            calls.append(front)
            self.assertEqual(kwargs["jev_advice"], ACK)
            if front:
                self.record(batch, "front", status="open", evidence_summary="部署完成，承诺的验证待执行")
                return AgentResult("front", None, 0, self.now, self.now, escalated=True)
            variables = self.service._prompt_variables("worker", "worker", batch, prior, ())
            self.assertEqual(json.loads(variables["active_tasks_json"])[0]["status"], "open")
            self.record(batch, "worker", status="completed", evidence_summary="隔离替身已完成验证")
            return AgentResult("worker", None, 0, self.now, self.now, silent=True)

        with patch("dws_dm_agent.service.jev_advise", new=AsyncMock(return_value=ACK)) as advise, \
                patch.object(self.service, "_run_agent", side_effect=agent):
            asyncio.run(self.service._handle_batch([self.event]))
        self.assertEqual(calls, [True, False])
        self.assertEqual(advise.await_count, 1)
        self.assertEqual(self.task["status"], "completed")

    def test_unavailable_advice_keeps_worker_and_memory(self):
        self.service.agent_runtime.has_stage = lambda stage: stage == "worker"
        async def worker(batch, prior, **kwargs):
            self.assertEqual(kwargs["jev_advice"]["status"], "unavailable")
            self.record(batch, "worker", status="completed", evidence_summary="完成通知仍被处理")
            return AgentResult("worker", None, 0, self.now, self.now, silent=True)
        with patch.object(jev, "request_advice", side_effect=IncompleteRead(b"partial")), \
                patch.object(self.service, "_run_agent", side_effect=worker):
            asyncio.run(self.service._handle_batch([self.event]))
        self.assertEqual(self.task["status"], "completed")

    def test_disabled_does_not_call_jev_or_add_kwargs(self):
        self.service.settings.raw["jev"]["enabled"] = False
        async def agent(batch, prior, **kwargs):
            self.assertNotIn("jev_advice", kwargs)
            return AgentResult("front", None, 0, self.now, self.now, silent=True)
        with patch("dws_dm_agent.service.jev_advise", new=AsyncMock()) as advise, \
                patch.object(self.service, "_run_agent", side_effect=agent):
            asyncio.run(self.service._handle_batch([self.event]))
        advise.assert_not_called()

    def test_fallback_reuses_advice(self):
        self.service.agent_runtime._stage = Mock(return_value=SimpleNamespace(profile=SimpleNamespace(fallbacks=("backup",))))
        results = [AgentResult("failed", None, 1, self.now, self.now),
                   AgentResult("backup", None, 0, self.now, self.now, silent=True)]
        with patch.object(self.service, "_run_agent", side_effect=results) as run:
            asyncio.run(self.service._run_worker_with_fallback([self.event], "", jev_advice=ACK))
        self.assertEqual([c.kwargs["jev_advice"] for c in run.call_args_list], [ACK, ACK])

    def test_front_steered_messages_clear_stale_advice_for_worker(self):
        added = IncomingEvent("extra", "extra", "conversation", fixtures.CONTACT,
                              "更正：还没有完成", self.now + timedelta(seconds=1))
        async def agent(batch, prior, *, front=False, **kwargs):
            if front:
                return AgentResult("front", None, 0, self.now, self.now, escalated=True,
                                   consumed_supplements=(added,))
            self.assertNotIn("jev_advice", kwargs)
            self.assertIn(added, batch)
            return AgentResult("worker", None, 0, self.now, self.now, silent=True)
        with patch("dws_dm_agent.service.jev_advise", new=AsyncMock(return_value=ACK)), \
                patch.object(self.service, "_run_agent", side_effect=agent):
            asyncio.run(self.service._handle_batch([self.event]))

    def test_fallback_preserves_steered_messages_and_clears_old_advice(self):
        self.service.agent_runtime._stage = Mock(return_value=SimpleNamespace(profile=SimpleNamespace(fallbacks=("backup",))))
        added = IncomingEvent("extra", "extra", "conversation", fixtures.CONTACT,
                              "更正：还没有完成", self.now + timedelta(seconds=1))
        async def agent(batch, prior, *, agent_override=None, **kwargs):
            if agent_override is None:
                return AgentResult("failed", None, 1, self.now, self.now, consumed_supplements=[added])
            self.assertEqual([event.message_id for event in batch], ["notice", "extra"])
            self.assertNotIn("jev_advice", kwargs)
            self.record(batch, "backup", status="waiting", evidence_summary=added.content)
            return AgentResult("backup", None, 0, self.now, self.now, silent=True)
        with patch.object(self.service, "_run_agent", side_effect=agent):
            result = asyncio.run(self.service._run_worker_with_fallback([self.event], "", jev_advice=ACK))
        self.assertEqual(result.consumed_supplements, [added])
        self.assertEqual(self.task["evidence_message_ids"], ["extra"])

    def test_real_run_agent_keeps_prompt_and_steered_evidence(self):
        root = Path(self.temp.name)
        context_path = root / "task-context.json"
        context_path.write_text("{}", encoding="utf-8")
        added = HistoryMessage("extra", "conversation", fixtures.CONTACT.display_name,
            fixtures.CONTACT.open_dingtalk_id, "更正：还没有完成", self.now + timedelta(seconds=1))
        self.service.dws.history = AsyncMock(return_value=[added])
        self.service.settings.raw["monitor_interval_seconds"] = 0.01
        captured = {}
        def open_session(stage, session_id, prompt, *args, **kwargs):
            captured.update(prompt=prompt, context=kwargs["task_context"])
            return fixtures._SteerableFakeSession(SimpleNamespace(timeout_seconds=5, task_context_path=context_path), session_id)
        self.service.agent_runtime = SimpleNamespace(profile_name=lambda stage: "pi-test",
            render=lambda stage, variables: json.dumps(variables, default=str, ensure_ascii=False),
            render_supplement=lambda *args, **kwargs: "更正：还没有完成", supplement_strategy="steer",
            resolve_stage=lambda *args: SimpleNamespace(profile=SimpleNamespace(protocol="pi-rpc", options={})),
            open_session=open_session)
        result = asyncio.run(self.service._run_agent([self.event], "", jev_advice=ACK))
        self.assertEqual(result.reply, "最终正文")
        self.assertIn("jev_message_advice", captured["prompt"])
        self.assertIn(self.event.content, captured["prompt"])
        self.assertIn("active_tasks_json", captured["prompt"])
        self.assertEqual(set(json.loads(context_path.read_text())["message_times"]), {"notice", "extra"})


if __name__ == "__main__":
    unittest.main()
