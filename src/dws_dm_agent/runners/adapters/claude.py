"""claude-stream-json adapter：stdin user 消息 + stdout 事件流。"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Mapping

from .. import claude as claude_command
from .. import mcp_catalog
from ...stages import stage_spec


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
        # mcp_config_path：front 是 escalate 专用配置，worker 是
        # options.mcp + dws-tools 服务工具，存在即消费。
        return claude_command.command(profile, mcp_config_path)

    def thread_options(self, profile: Any, workspace_root: Path) -> dict[str, Any]:
        return {}

    def mcp_config(
        self, profile: Any, session_dir: Path, stage_name: str
    ) -> Path | None:
        # kind 驱动（不比 stage 名，自定义槽位与 front/worker 同权）：
        # triage 是 escalate 专用 mcp.json，command() 经 --strict-mcp-config
        # 独占消费（只读节点除了升级别无 MCP）。
        # executive：options.mcp 目录 server + dws-tools 服务工具，与 pi
        # 的挂载面一致；空目录 + 无服务工具时维持不写文件。
        kind = stage_spec(stage_name).kind
        if kind == "triage" and profile.read_only:
            return mcp_catalog.write_session_config(
                session_dir / "mcp.json",
                servers={},
                escalate_script=mcp_catalog.escalate_script_path(),
                auto_enable_codemode=True,
            )
        if kind != "executive":
            return None
        servers = mcp_catalog.resolve_servers(profile.options.get("mcp"))
        servers = {
            name: mcp_catalog.apply_tool_gate(server)
            for name, server in servers.items()
        }
        if profile.options.get("service_tools_mcp"):
            servers["dws-tools"] = mcp_catalog.service_tools_server()
        return mcp_catalog.write_session_config(
            session_dir / "mcp.json", servers=servers
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
            state = session._protocol_state
            for tool_id, tool_name in _assistant_tool_uses(message):
                state.setdefault("claude_tool_names", {})[tool_id] = tool_name
                if session._is_escalate_tool(tool_name):
                    session._capture_escalation()
                if session._is_no_reply_tool(tool_name):
                    session._capture_silent()
                session._record_tool_start({"toolName": tool_name})
            thinking = _assistant_thinking(message)
            if thinking:
                session._capture_progress(thinking)
                session._capture_live("think", thinking)
            text = session._assistant_text(event)
            session._capture_body(text)
            session._capture_live("message", text)
        elif event_type == "user":
            # claude 的工具执行结果以 user(tool_result) 事件回流：dws-tools
            # 的 report(final/blocked) 在 server 侧写 DWS_REPORT_FILE，这里
            # 在执行收尾读文件按终稿收口（与 pi 的 tool_execution_end 同通路）。
            state = session._protocol_state
            names = state.get("claude_tool_names", {})
            message = event.get("message")
            content = message.get("content") if isinstance(message, Mapping) else None
            if isinstance(content, list):
                for item in content:
                    if not (isinstance(item, Mapping) and item.get("type") == "tool_result"):
                        continue
                    tool_name = names.get(str(item.get("tool_use_id") or ""))
                    if session._is_report_tool(tool_name):
                        session._capture_report()
        elif event_type == "result":
            result = event.get("result")
            if isinstance(result, str):
                session._final_text = result
            if event.get("is_error"):
                session.error = session._sanitize(
                    str(event.get("result") or "Claude error"), 500
                )
            session._finish()


def _assistant_tool_uses(message: Any) -> list[tuple[str, str]]:
    """(tool_use_id, 工具名) 对：工具名匹配 no_reply/report 时，user 侧
    tool_result 收尾要按 id 回查名字。"""
    if not isinstance(message, Mapping):
        return []
    content = message.get("content")
    if not isinstance(content, list):
        return []
    return [
        (str(item.get("id") or ""), str(item.get("name") or ""))
        for item in content
        if isinstance(item, Mapping) and item.get("type") in {"tool_use", "toolUse"}
    ]


def _assistant_tool_names(message: Any) -> list[str]:
    return [name for _id, name in _assistant_tool_uses(message)]


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
