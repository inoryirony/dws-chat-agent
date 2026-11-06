"""custom-jsonl-v1 adapter：dws 自定义 JSON 行协议，外部 agent 的通用逃生口。"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Mapping

from .. import custom as custom_command


class CustomAdapter:
    protocol = "custom-jsonl-v1"
    images = "embed"
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
        return custom_command.command(profile)

    def thread_options(self, profile: Any, workspace_root: Path) -> dict[str, Any]:
        return {}

    def mcp_config(
        self, profile: Any, session_dir: Path, stage_name: str
    ) -> Path | None:
        return None

    async def start(self, session: Any) -> None:
        prepared = session.prepared
        response = await session._custom_call(
            {
                "type": "start",
                "protocolVersion": 1,
                "sessionId": session.session_id,
                "stage": prepared.stage.name,
                "cwd": str(prepared.cwd),
                "prompt": prepared.prompt,
                "readOnly": prepared.stage.profile.read_only,
                "model": prepared.stage.profile.model,
                "reasoningEffort": prepared.stage.profile.reasoning_effort,
            }
        )
        if not response.get("success", False):
            raise RuntimeError(
                str(response.get("error") or "custom agent rejected start")
            )

    async def steer(self, session: Any, prompt: str) -> bool:
        response = await session._custom_call(
            {
                "type": "steer",
                "sessionId": session.session_id,
                "message": prompt,
            }
        )
        return bool(response.get("success", False))

    async def abort(self, session: Any) -> None:
        await session._custom_call(
            {"type": "abort", "sessionId": session.session_id}, timeout=2
        )

    async def parse_event(self, session: Any, event: Mapping[str, Any]) -> None:
        event_type = str(event.get("type") or "")
        if event_type in {"progress", "thinking"}:
            text = str(event.get("text") or "")
            session._capture_progress(text)
            session._capture_live(
                "think" if event_type == "thinking" else "message", text
            )
        elif event_type == "assistant":
            text = session._assistant_text(event)
            session._capture_body(text)
            session._capture_live("message", text)
        elif event_type == "tool_call":
            name = str(event.get("name") or event.get("toolName") or "tool")
            if session._is_escalate_tool(name):
                session._capture_escalation()
            session._record_tool_start({"toolName": name, "args": event.get("args")})
        elif event_type == "result":
            result = event.get("result", event.get("value"))
            if isinstance(result, str):
                session._final_text = result
            else:
                session.error = "custom_agent_result_missing"
            session._finish()
        elif event_type == "error":
            session._fail(
                session._sanitize(
                    str(event.get("message") or "custom agent error"), 500
                )
            )


__all__ = ["CustomAdapter", "sys"]
