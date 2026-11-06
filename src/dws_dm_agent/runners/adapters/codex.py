"""codex-app-server adapter：JSON-RPC 握手 + thread/turn 生命周期。"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Mapping

from .. import codex as codex_command
from .. import mcp_catalog


class CodexAdapter:
    protocol = "codex-app-server"
    images = "native"  # localImage 输入项，路径由 runtime 传
    native_steer = True
    service_tools = False

    def command(
        self,
        profile: Any,
        mcp_config_path: Path | None = None,
        model_override: str | None = None,
        *,
        mount_escalate: bool = False,
    ) -> list[str]:
        # codex 的模型经 thread_options 走（含 model_override），
        # command 层不感知；mcp 走 -c mcp_servers.* 注入。
        return codex_command.command(profile, mount_escalate)

    def thread_options(self, profile: Any, workspace_root: Path) -> dict[str, Any]:
        return codex_command.thread_options(profile, workspace_root)

    def mcp_config(
        self, profile: Any, session_dir: Path, stage_name: str
    ) -> Path | None:
        if stage_name != "front":
            return None
        script = Path(__file__).parents[2] / "escalate_mcp.py"
        return mcp_catalog.write_session_config(
            session_dir / "mcp.json",
            servers={},
            escalate_script=script,
            auto_enable_codemode=True,
        )

    async def start(self, session: Any) -> None:
        prepared = session.prepared
        initialized = await session._jsonrpc_call(
            "initialize",
            {
                "clientInfo": {
                    "name": "dws-chat-agent",
                    "title": "DWS Chat Agent",
                    "version": "0.1.0",
                }
            },
        )
        if "result" not in initialized:
            raise RuntimeError("Codex app-server initialization failed")
        await session._write_json(
            {"jsonrpc": "2.0", "method": "initialized", "params": {}}
        )
        started = await session._jsonrpc_call(
            "thread/start", dict(prepared.thread_options)
        )
        session.thread_id = str(
            started.get("result", {}).get("thread", {}).get("id") or ""
        )
        if not session.thread_id:
            raise RuntimeError("Codex app-server did not return a thread id")
        inputs: list[dict[str, str]] = [{"type": "text", "text": prepared.prompt}]
        inputs.extend(
            {"type": "localImage", "path": str(path.resolve())}
            for path in prepared.local_image_paths
        )
        turn = await session._jsonrpc_call(
            "turn/start",
            {
                "threadId": session.thread_id,
                "input": inputs,
                "effort": prepared.stage.profile.reasoning_effort or None,
                "summary": str(
                    prepared.stage.profile.options.get("reasoning_summary", "concise")
                ),
            },
        )
        session.turn_id = str(turn.get("result", {}).get("turn", {}).get("id") or "")
        if not session.turn_id:
            raise RuntimeError("Codex app-server did not return a turn id")

    async def steer(self, session: Any, prompt: str) -> bool:
        if not session.thread_id or not session.turn_id:
            return False
        response = await session._jsonrpc_call(
            "turn/steer",
            {
                "threadId": session.thread_id,
                "expectedTurnId": session.turn_id,
                "input": [{"type": "text", "text": prompt}],
            },
        )
        return bool(response.get("result", {}).get("turnId"))

    async def abort(self, session: Any) -> None:
        if session.thread_id and session.turn_id:
            await session._jsonrpc_call(
                "turn/interrupt",
                {"threadId": session.thread_id, "turnId": session.turn_id},
                timeout=2,
            )

    async def parse_event(self, session: Any, event: Mapping[str, Any]) -> None:
        method = str(event.get("method") or "")
        params = event.get("params")
        params = params if isinstance(params, Mapping) else {}
        if method == "item/started":
            item = params.get("item")
            if isinstance(item, Mapping):
                item_id = str(item.get("id") or "")
                if item.get("type") == "agentMessage":
                    state = session._protocol_state
                    state["item_phases"][item_id] = str(item.get("phase") or "")
                elif session._is_escalate_tool(
                    item.get("tool") or item.get("toolName") or item.get("name")
                ):
                    session._capture_escalation()
                name = str(
                    item.get("tool") or item.get("toolName") or item.get("name") or ""
                )
                if name:
                    session._record_tool_start(
                        {"toolName": name, "args": item.get("arguments")}
                    )
        elif method == "item/reasoning/summaryTextDelta":
            item_id = str(params.get("itemId") or "")
            delta = str(params.get("delta") or "")
            state = session._protocol_state
            value = state["reasoning"].get(item_id, "") + delta
            state["reasoning"][item_id] = value
            session._capture_progress(value)
            session._capture_live("think", delta)
        elif method == "item/agentMessage/delta":
            session._capture_live("message", str(params.get("delta") or ""))
        elif method == "item/completed":
            item = params.get("item")
            if isinstance(item, Mapping):
                if item.get("type") == "agentMessage":
                    phase = str(item.get("phase") or "")
                    text = str(item.get("text") or "")
                    if phase == "final_answer":
                        session._capture_body(text, final=True)
                elif item.get("type") == "reasoning":
                    session._capture_progress(
                        "\n".join(
                            str(part.get("text") or "")
                            for part in item.get("summary", [])
                            if isinstance(part, Mapping)
                        ).strip()
                    )
                elif session._is_escalate_tool(
                    item.get("tool") or item.get("toolName") or item.get("name")
                ):
                    session._capture_escalation()
        elif method == "turn/completed":
            turn = params.get("turn")
            if isinstance(turn, Mapping) and (
                not session.turn_id or str(turn.get("id") or "") == session.turn_id
            ):
                if str(turn.get("status") or "") == "completed":
                    session.error = ""
                else:
                    session.error = f"codex_turn_{turn.get('status') or 'failed'}"
                session._finish()
        elif method == "error":
            if not bool(params.get("willRetry")):
                session._fail(
                    session._sanitize(str(params.get("message") or params), 500)
                )


__all__ = ["CodexAdapter", "sys"]
