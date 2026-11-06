"""pi-rpc adapter：JSON 行 RPC + pi_dws_tools 服务工具扩展。"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Mapping

from .. import mcp_catalog
from .. import pi as pi_command

# pi-worker 冷启动（含扩展/MCP 加载）偶发超过默认 15s，首条 prompt 握手放宽到 3 分钟。
START_HANDSHAKE_TIMEOUT_SECONDS = 180.0


class PiAdapter:
    protocol = "pi-rpc"
    images = "native"  # ImageContent（base64 + mimeType）随首条 prompt
    native_steer = True
    service_tools = True  # pi_dws_tools.ts / pi_escalate.ts 扩展

    def command(
        self,
        profile: Any,
        mcp_config_path: Path | None = None,
        model_override: str | None = None,
        *,
        mount_escalate: bool = False,
    ) -> list[str]:
        return pi_command.command(
            profile,
            mount_escalate,
            model_override=model_override,
            mcp_config=mcp_config_path,
        )

    def thread_options(self, profile: Any, workspace_root: Path) -> dict[str, Any]:
        return {}

    def mcp_config(
        self, profile: Any, session_dir: Path, stage_name: str
    ) -> Path | None:
        servers = mcp_catalog.resolve_servers(profile.options.get("mcp"))
        return mcp_catalog.write_session_config(session_dir / "mcp.json", servers=servers)

    async def start(self, session: Any) -> None:
        prepared = session.prepared
        response = await session._pi_call(
            _pi_prompt_command(prepared),
            timeout=START_HANDSHAKE_TIMEOUT_SECONDS,
        )
        if not response.get("success", False):
            raise RuntimeError(str(response.get("error") or "Pi rejected prompt"))

    async def steer(self, session: Any, prompt: str) -> bool:
        response = await session._pi_call(
            {
                "type": "prompt",
                "message": prompt,
                "streamingBehavior": "steer",
            }
        )
        return bool(response.get("success", False))

    async def abort(self, session: Any) -> None:
        await session._pi_call({"type": "abort"}, timeout=2)

    async def parse_event(self, session: Any, event: Mapping[str, Any]) -> None:
        event_type = str(event.get("type") or "")
        if event_type == "tool_execution_end":
            # 工具收尾先进 live 流，再走下面各分支的终态判定：
            # 面板要看到"调了什么、成没成"，与收口逻辑互不影响。
            session._record_tool_end(event)
        if event_type == "extension_ui_request":
            if event.get("method") in {"select", "confirm", "input", "editor"}:
                await session._write_json(
                    {
                        "type": "extension_ui_response",
                        "id": event.get("id"),
                        "cancelled": True,
                    }
                )
        elif event_type == "message_update":
            update = event.get("assistantMessageEvent")
            update = update if isinstance(update, Mapping) else {}
            update_type = str(update.get("type") or "")
            if update_type == "text_delta":
                delta = str(update.get("delta") or "")
                session._capture_stream_delta(delta)
                session._capture_live("message", delta)
            elif update_type == "thinking_delta":
                delta = str(update.get("delta") or "")
                index = int(update.get("contentIndex") or 0)
                state = session._protocol_state
                value = state["pi_thinking"].get(index, "") + delta
                state["pi_thinking"][index] = value
                # progress 要累积全文（一个思考 = 一条里程碑）；
                # live 只喂 delta，合并交给 _capture_live，否则整段会重复。
                session._capture_progress(value)
                session._capture_live("think", delta)
            elif update_type == "thinking_end":
                session._capture_progress(str(update.get("content") or ""))
                session._seal_live("think")
            elif update_type == "text_end":
                session._seal_live("message")
        elif event_type == "tool_execution_start":
            if session._is_escalate_tool(event.get("toolName")):
                session._capture_escalation()
            session._record_tool_start(event)
        elif (
            event_type == "tool_execution_end"
            and not event.get("isError")
            and session._is_no_reply_tool(event.get("toolName"))
        ):
            # 只在工具真正执行成功后才承认 silent；工具失败时模型继续正常
            # 收尾，不能把一次失败的 no_reply 当成沉默吞掉后续正文。
            session._capture_silent()
        elif (
            event_type == "tool_execution_end"
            and not event.get("isError")
            and session._is_report_tool(event.get("toolName"))
        ):
            # report(final/blocked) 已在扩展侧写好 DWS_REPORT_FILE 并
            # terminate；这里读文件把正文接进终稿通路。工具执行失败时
            # 不终止，模型继续正常收尾。
            session._capture_report()
        elif event_type == "message_end":
            session._seal_live("think", "message")
            text = session._assistant_text(event)
            if text and _is_pi_intermediate_message(event.get("message")):
                # 工具调用间隔中的中间陈述（带 toolCall 或 error/aborted 终止）
                # 只走进度展示，不能作为最终回复交付。
                session._capture_progress(text)
            elif text:
                session._capture_body(text, final=True)
        elif event_type == "agent_end":
            # agent_end 只代表一次底层 run 结束：之后 pi 仍可能自动重试、
            # 溢出/压缩后重试，或处理排队的 steer/follow-up。收口交给
            # agent_settled，这里只把本轮候选正文接进终稿通路。
            messages = event.get("messages")
            if isinstance(messages, list) and not session._report_outcome:
                reply = _pi_final_reply_text(messages)
                if reply:
                    session._capture_body(reply, final=True)
                else:
                    # 本轮没有可交付的终稿：清掉 message_end 期间捕获的
                    # 候选文本，宁可空回复走 fallback 链，也不能把
                    # 中间陈述当正式回复发出。report 终稿除外：终止性
                    # report 的最后一条 assistant 消息带 toolCall，
                    # 终稿在工具事件里，这里不能清掉。
                    session._final_text = ""
        elif event_type == "agent_settled":
            # pi 不再有任何自动续跑（重试 / 压缩恢复 / 排队消息）才收口。
            # 0.84.2 与 1.5 都发本事件；agent_end 不是终局信号。
            session._finish()
        elif event_type == "response" and not event.get("success", True):
            error = session._sanitize(str(event.get("error") or "Pi RPC error"), 500)
            if "already processing" in error.lower():
                # pi 正在处理（或即将开始）后续 turn 时会拒绝新 prompt，
                # 此时会话本身健康；作废整场会连正在处理的补充消息一起丢，
                # 这里只忽略该次被拒请求，等 agent_settled 或服务层超时兜底。
                return
            session._fail(error)


def _pi_prompt_command(prepared: Any) -> dict[str, Any]:
    """构造 pi RPC 首条 prompt 命令；有图片时以 ImageContent 原生附带。

    接受 PreparedAgent，也接受 AgentSession（取 .prepared）——历史测试
    以 session 为参调用，保持两种入口等价。
    """
    if hasattr(prepared, "prepared"):
        prepared = prepared.prepared
    command: dict[str, Any] = {
        "type": "prompt",
        "message": prepared.prompt,
    }
    if prepared.pi_images:
        command["images"] = list(prepared.pi_images)
    return command


def _is_pi_intermediate_message(message: Any) -> bool:
    """判断 pi 的 assistant 消息是否为工具调用间隔中的中间陈述或错误/中断消息。

    pi 助手消息的 content 块类型为 text/thinking/toolCall；带 toolCall 块
    （stopReason=toolUse）说明模型还要继续执行工具，以 error/aborted 终止的
    消息则不是正常收尾。这两类消息的文本都不能作为最终回复交付。
    """
    if not isinstance(message, Mapping):
        return False
    if str(message.get("stopReason") or "") in {"error", "aborted"}:
        return True
    content = message.get("content")
    if not isinstance(content, list):
        return False
    return any(
        isinstance(item, Mapping) and item.get("type") == "toolCall"
        for item in content
    )


def _pi_final_reply_text(messages: list[Any]) -> str:
    """从 pi agent_end.messages 提取可交付的终稿文本。

    只认可最后一条 assistant 消息：必须是无 toolCall 的纯文本且非
    error/aborted 终止，才算终稿。工具调用间隔中的中间陈述、错误消息
    一律返回空串，由上层记 agent_result_missing 走 fallback 链，
    避免把计划独白当正式回复发给联系人。
    """
    for message in reversed(messages):
        if not isinstance(message, Mapping) or message.get("role") != "assistant":
            continue
        if _is_pi_intermediate_message(message):
            return ""
        content = message.get("content")
        if isinstance(content, str):
            return content.strip()
        if not isinstance(content, list):
            return ""
        return "\n".join(
            str(item.get("text") or "")
            for item in content
            if isinstance(item, Mapping) and item.get("type") == "text"
        ).strip()
    return ""


__all__ = ["PiAdapter", "START_HANDSHAKE_TIMEOUT_SECONDS", "sys"]
