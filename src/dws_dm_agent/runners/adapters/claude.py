"""claude-stream-json adapter：stdin user 消息 + stdout 事件流。"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Mapping

from .. import claude as claude_command
from .. import mcp_catalog


class ClaudeAdapter:
    protocol = "claude-stream-json"
    images = "embed"  # 无原生图片输入，路径嵌入 prompt
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
        # 模型经 --model 走 command 层；model_override 由 runtime 拼进
        # profile 之前处理（见 runtime._command 调用点），这里不重复。
        return claude_command.command(
            profile, mcp_config_path if mount_escalate else None
        )

    def thread_options(self, profile: Any, workspace_root: Path) -> dict[str, Any]:
        return {}

    def mcp_config(
        self, profile: Any, session_dir: Path, stage_name: str
    ) -> Path | None:
        # claude 的 escalate 走 --mcp-config：front 阶段出 escalate 专用
        # mcp.json，command() 按 mcp_config_path 消费（--strict-mcp-config）。
        if stage_name != "front" or not profile.read_only:
            return None
        script = Path(__file__).parents[2] / "escalate_mcp.py"
        return mcp_catalog.write_session_config(
            session_dir / "mcp.json",
            servers={},
            escalate_script=script,
            auto_enable_codemode=True,
        )

    async def start(self, session: Any) -> None:
        await session._write_json(
            {
                "type": "user",
                "message": {"role": "user", "content": session.prepared.prompt},
            }
        )

    async def steer(self, session: Any, prompt: str) -> bool:
        await session._write_json(
            {
                "type": "user",
                "message": {"role": "user", "content": prompt},
            }
        )
        return True

    async def abort(self, session: Any) -> None:
        # claude stream-json 无中断命令，退化为关 stdin / 杀进程。
        return None

    async def parse_event(self, session: Any, event: Mapping[str, Any]) -> None:
        event_type = str(event.get("type") or "")
        if event_type == "assistant":
            message = event.get("message")
            for tool_name in _assistant_tool_names(message):
                if session._is_escalate_tool(tool_name):
                    session._capture_escalation()
                session._record_tool_start({"toolName": tool_name})
            thinking = _assistant_thinking(message)
            if thinking:
                session._capture_progress(thinking)
                session._capture_live("think", thinking)
            text = session._assistant_text(event)
            session._capture_body(text)
            session._capture_live("message", text)
        elif event_type == "result":
            result = event.get("result")
            if isinstance(result, str):
                session._final_text = result
            if event.get("is_error"):
                session.error = session._sanitize(
                    str(event.get("result") or "Claude error"), 500
                )
            session._finish()


def _assistant_tool_names(message: Any) -> list[str]:
    if not isinstance(message, Mapping):
        return []
    content = message.get("content")
    if not isinstance(content, list):
        return []
    return [
        str(item.get("name") or "")
        for item in content
        if isinstance(item, Mapping) and item.get("type") in {"tool_use", "toolUse"}
    ]


def _assistant_thinking(message: Any) -> str:
    if not isinstance(message, Mapping):
        return ""
    content = message.get("content")
    if not isinstance(content, list):
        return ""
    return "\n".join(
        str(item.get("thinking") or "")
        for item in content
        if isinstance(item, Mapping) and item.get("type") == "thinking"
    ).strip()


__all__ = ["ClaudeAdapter", "sys"]
