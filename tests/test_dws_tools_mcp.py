from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from dws_dm_agent.runners import mcp_catalog
from dws_dm_agent.runners.adapters import adapter_for


def _run_server(requests: list[dict], env_extra: dict[str, str] | None = None) -> list[dict]:
    root = Path(__file__).resolve().parents[1]
    completed = subprocess.run(
        [sys.executable, "-m", "dws_dm_agent.runners.dws_tools_mcp"],
        input="\n".join(json.dumps(item) for item in requests) + "\n",
        text=True,
        capture_output=True,
        check=True,
        env={**os.environ, "PYTHONPATH": str(root / "src"), **(env_extra or {})},
    )
    return [json.loads(line) for line in completed.stdout.splitlines()]


class DwsToolsMcpTests(unittest.TestCase):
    """dws-tools stdio server：四工具协议 + no_reply/report(final) 行为。"""

    def test_tools_list_exposes_four_service_tools(self) -> None:
        responses = _run_server(
            [
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18"},
                },
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            ]
        )
        names = {tool["name"] for tool in responses[1]["result"]["tools"]}
        self.assertEqual(
            names, {"no_reply", "report", "task_record", "task_lookup"}
        )

    def test_no_reply_returns_ok_without_context(self) -> None:
        responses = _run_server(
            [
                {"jsonrpc": "2.0", "id": 1, "method": "ping"},
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "no_reply", "arguments": {}},
                },
            ]
        )
        self.assertEqual(responses[0]["result"], {})
        body = json.loads(responses[1]["result"]["content"][0]["text"])
        self.assertTrue(body["ok"])

    def test_report_final_writes_report_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report_file = Path(tmp) / "report.json"
            responses = _run_server(
                [
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {
                            "name": "report",
                            "arguments": {"kind": "final", "text": "完成"},
                        },
                    },
                ],
                env_extra={"DWS_REPORT_FILE": str(report_file)},
            )
            body = json.loads(responses[0]["result"]["content"][0]["text"])
            self.assertTrue(body["ok"])
            payload = json.loads(report_file.read_text(encoding="utf-8"))
            self.assertEqual(payload, {"kind": "final", "text": "完成"})

    def test_report_rejects_bad_kind_and_empty_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report_file = Path(tmp) / "report.json"
            responses = _run_server(
                [
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {
                            "name": "report",
                            "arguments": {"kind": "bogus", "text": "x"},
                        },
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {
                            "name": "report",
                            "arguments": {"kind": "blocked", "text": "  "},
                        },
                    },
                ],
                env_extra={"DWS_REPORT_FILE": str(report_file)},
            )
            first = json.loads(responses[0]["result"]["content"][0]["text"])
            second = json.loads(responses[1]["result"]["content"][0]["text"])
            self.assertFalse(first["ok"])
            self.assertFalse(second["ok"])
            self.assertFalse(report_file.exists())

    def test_task_tools_report_context_missing_error(self) -> None:
        responses = _run_server(
            [
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "task_lookup", "arguments": {}},
                }
            ]
        )
        body = json.loads(responses[0]["result"]["content"][0]["text"])
        self.assertFalse(body["ok"])

    def test_unknown_tool_returns_error(self) -> None:
        responses = _run_server(
            [
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "bogus", "arguments": {}},
                }
            ]
        )
        self.assertIn("error", responses[0])


class WorkerParityTests(unittest.TestCase):
    """四个 worker adapter 的 MCP/服务工具装配对等。"""

    def _fake_profile(self, options: dict) -> object:
        class Profile:
            protocol = "placeholder"
            read_only = False
            name = "test-worker"
            extra_args: list[str] = []
            model = "m"
            reasoning_effort = None
            environment: dict[str, str] = {}
            worktree_repo = None
            fallbacks: list[str] = []

            def __init__(self) -> None:
                self.options = options
                self.command = ["bin"]

        return Profile()

    def test_catalog_helpers_point_at_real_scripts(self) -> None:
        self.assertTrue(mcp_catalog.escalate_script_path().is_file())
        self.assertTrue(mcp_catalog.tools_script_path().is_file())
        server = mcp_catalog.service_tools_server()
        self.assertEqual(server["type"], "stdio")
        self.assertTrue(Path(server["args"][0]).is_file())

    def test_claude_worker_mcp_config_mounts_catalog_and_dws_tools(self) -> None:
        import tempfile

        adapter = adapter_for("claude-stream-json")
        profile = self._fake_profile(
            {
                "mcp": {"grafana-dev": True},
                "service_tools_mcp": True,
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = adapter.mcp_config(profile, Path(tmp), "worker")
            payload = json.loads(path.read_text(encoding="utf-8"))
        servers = payload["mcpServers"]
        self.assertIn("grafana-dev", servers)
        self.assertIn("dws-tools", servers)
        self.assertEqual(servers["dws-tools"]["args"][0], str(mcp_catalog.tools_script_path()))

    def test_claude_front_mcp_config_only_escalate(self) -> None:
        import tempfile

        adapter = adapter_for("claude-stream-json")
        profile = self._fake_profile({"service_tools_mcp": True})
        profile.read_only = True
        with tempfile.TemporaryDirectory() as tmp:
            path = adapter.mcp_config(profile, Path(tmp), "front")
            payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(set(payload["mcpServers"]), {"dws-escalate"})
        self.assertEqual(
            payload["mcpServers"]["dws-escalate"]["args"][0],
            str(mcp_catalog.escalate_script_path()),
        )

    def test_claude_worker_command_consumes_mcp_config(self) -> None:
        adapter = adapter_for("claude-stream-json")
        profile = self._fake_profile(
            {"service_tools_mcp": True}
        )
        argv = adapter.command(profile, Path("/tmp/session/mcp.json"))
        self.assertIn("--mcp-config", argv)
        self.assertIn("--strict-mcp-config", argv)

    def test_codex_worker_mcp_config_and_command_injection(self) -> None:
        import tempfile

        adapter = adapter_for("codex-app-server")
        profile = self._fake_profile(
            {"mcp": {"grafana-dev": True}, "service_tools_mcp": True}
        )
        profile.command = ["codex"]
        with tempfile.TemporaryDirectory() as tmp:
            path = adapter.mcp_config(profile, Path(tmp), "worker")
            self.assertTrue(path.is_file())
            argv = adapter.command(profile, path)
        joined = " ".join(argv)
        self.assertIn("mcp_servers.dws-tools.command=", joined)
        self.assertIn("mcp_servers.grafana-dev.command=", joined)

    def test_dsh_worker_session_patch_generated_dynamically(self) -> None:
        """dsh 的 MCP/skills 装配走 session 级动态 patch，静态文件只留模型层。"""
        import tempfile

        adapter = adapter_for("dsh-headless-jsonl")
        profile = self._fake_profile(
            {
                "service_tools_mcp": True,
                "skills": ["demo"],
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            session = Path(tmp)
            (session / "skills").mkdir()
            path = adapter.mcp_config(profile, session, "worker")
            self.assertTrue(path.is_file())
            text = path.read_text(encoding="utf-8")
        self.assertIn("mcp-dws-tools", text)
        self.assertIn('"dws-tools"', text)
        self.assertIn("skill-filesystem", text)
        self.assertIn("dsh-skill-filesystem", text)
        self.assertIn("includeDefaultRoots: false", text)
        static_patch = (
            Path(__file__).resolve().parents[1]
            / "deploy"
            / "dsh"
            / "dws-worker.patch.yml"
        )
        static_text = static_patch.read_text(encoding="utf-8")
        self.assertNotIn("dsh-mcp-client", static_text)


if __name__ == "__main__":
    unittest.main()


class ToolGateTests(unittest.TestCase):
    """mcp_tool_gate：directTools 白名单的协议无关闸门。"""

    def _spawn(self, allow: str):
        gate = Path(__file__).resolve().parents[1] / "src" / "dws_dm_agent" / "runners" / "mcp_tool_gate.py"
        fake = Path(__file__).resolve().parent / "_fake_mcp_server.py"
        return subprocess.Popen(
            [sys.executable, str(gate), "--allow", allow, "--",
             sys.executable, str(fake)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        )

    def test_gate_filters_tools_list_and_blocks_calls(self) -> None:
        p = self._spawn("alpha,gamma")
        try:
            def rpc(obj):
                p.stdin.write((json.dumps(obj) + "\n").encode())
                p.stdin.flush()

            rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                            "clientInfo": {"name": "t", "version": "0"}}})
            p.stdout.readline()
            rpc({"jsonrpc": "2.0", "method": "notifications/initialized"})
            rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                 "params": {"name": "beta", "arguments": {}}})
            # blocked 响应在 pump 线程、tools/list 响应在主线程转发，
            # 到达顺序不保证：按 id 收集而不是按行序读。
            by_id = {}
            while len(by_id) < 2:
                msg = json.loads(p.stdout.readline())
                if msg.get("id") in (2, 3):
                    by_id[msg["id"]] = msg
            self.assertEqual(
                sorted(t["name"] for t in by_id[2]["result"]["tools"]),
                ["alpha", "gamma"],
            )
            self.assertIn("not in the allowlist", by_id[3]["error"]["message"])
        finally:
            p.kill()
            p.wait()

    def test_gate_missing_separator_fails_loud(self) -> None:
        gate = Path(__file__).resolve().parents[1] / "src" / "dws_dm_agent" / "runners" / "mcp_tool_gate.py"
        with self.assertRaises(subprocess.CalledProcessError):
            subprocess.run(
                [sys.executable, str(gate), "--allow", "a"],
                input="", capture_output=True, text=True, check=True,
            )

    def test_apply_tool_gate_wraps_stdio_command(self) -> None:
        from dws_dm_agent.runners import mcp_catalog
        server = mcp_catalog.apply_tool_gate({
            "command": "/bin/upstream", "args": ["--flag"],
            "directTools": ["a", "b"],
        })
        argv = [server["command"], *server["args"]]
        self.assertEqual(argv[0], sys.executable)
        self.assertIn("mcp_tool_gate.py", argv[1])
        self.assertEqual(argv[2], "--allow")
        self.assertEqual(argv[3], "a,b")
        self.assertEqual(argv[4], "--")
        self.assertEqual(argv[5], "/bin/upstream")
        self.assertEqual(argv[6], "--flag")
        self.assertNotIn("directTools", server)

    def test_apply_tool_gate_passes_through_without_directtools(self) -> None:
        from dws_dm_agent.runners import mcp_catalog
        server = {"command": "/bin/upstream"}
        self.assertEqual(mcp_catalog.apply_tool_gate(dict(server)), server)
        with self.assertRaises(ValueError):
            mcp_catalog.apply_tool_gate(
                {"url": "http://x", "directTools": ["a"]}
            )
