"""dsh-headless-jsonl adapter：dsh headless profile 一次性 NDJSON 运行。

兼容基线：@deepseek-ai/dsh 0.2.1-rc.3（headless 插件同版本硬互锁；npm 线
0.2.1-rc.3 → 0.2.2-rc.1）。事件流是 headless 插件 json-stream 投影
（lib/json-stream-*.js），按 `type` 白名单解析、未知事件跳过：小版本加
事件不炸，字段缺失按空处理。

事件 schema（实测源码+live probe）：
- {"type":"session","sessionId":...,"cwd":...}
- {"type":"status","phase":"turn_start|step_start|step_end|turn_end",...}
  turn_end 带 reason（封闭枚举 completed/aborted/blocked/error/
  max-tokens/interrupted/forked，插件可合并扩展）；step_end 可带 usage。
- {"type":"thinking","text":...}  已提交的 assistant reasoning
- {"type":"text","text":...}      已提交的 assistant 正文
- {"type":"tool_call","callId","tool","input"}  input 是解析后的对象
- {"type":"tool_result","callId","status":"completed|error","result"}
- {"type":"final","text":...}     终稿（豁免 byte cap，进程随后退出）

投影 byte cap：单字符串 8KB / 单事件行 32KB，超限截断并打 truncated 标。

协议局限（headless runner 源码实证，适配层如实声明）：
- 无运行中 steer：runner 是 followup(task) + whenIdle() 单驱动；
  补充消息走 --session-id adopt 新进程 = restart_with_context 等价物。
- 无 abort 命令：退化为杀进程树（runtime 已有 _terminate_process_tree）。
- MCP/扩展不按命令行注入，由 dsh profile 插件层叠决定：节点装配面
  （options.mcp / service_tools_mcp / skills）经 mcp_config 生成
  session patch，模型层 provider 留 options.dsh_patches 静态注入。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Mapping

from .. import mcp_catalog

# turn_end reason.kind → 会话收口动作。error 含结构化 LlmFailure；
# aborted/blocked/max-tokens/interrupted/forked 均按非正常收口记录，
# 其中 blocked 语义对齐 pi 的 report(blocked)。
_ERROR_KINDS = {"error"}
_ABNORMAL_KINDS = {"aborted", "blocked", "max-tokens", "interrupted", "forked"}


class DshAdapter:
    protocol = "dsh-headless-jsonl"
    images = "embed"
    native_steer = False  # 运行中不可注入；补充消息由服务层重启续跑
    # 服务工具经 options.service_tools_mcp → dws-tools patch（stdio）挂载，
    # 与 pi 同名同后端；校验按 options 放行，协议级能力位保持 False。
    service_tools = False

    def command(
        self,
        profile: Any,
        mcp_config_path: Path | None = None,
        model_override: str | None = None,
        *,
        mount_escalate: bool = False,
    ) -> list[str]:
        # dsh 的模型/凭证由 profile 插件层决定（如 dsh-llm 的 provider
        # 配置），命令行不重复传模型；escalate/服务工具同理不挂载。
        # launcher 级 flag（--profile/--patch/--session-id）必须全部在
        # app 级 flag（--json 是 headless app 的）与 task positional 之前：
        # launcher 遇到第一个不认识的 token 就把余下全部交给 app（dsh-cmdline
        # 的 CmdlineArgs 契约），实测顺序错了 app 报 unknown option '--patch'
        # 或 launcher 报 --profile is required。task 不走 argv：prompt 可能
        # 含整批图片附件说明，超长会撞 argv 上限；统一走 stdin（task="-"）。
        result = [*profile.command]
        result.extend(["--profile", str(profile.options.get("dsh_profile") or "headless")])
        for patch in profile.options.get("dsh_patches", ()):
            result.extend(["--patch", str(patch)])
        # 节点装配 patch（MCP/skills）在静态模型层 patch 之后追加，
        # 命名不冲突（静态文件用 id: mcp-* 之外的 id）。
        if mcp_config_path is not None:
            result.extend(["--patch", str(mcp_config_path)])
        if profile.options.get("dsh_session_arg"):
            # 由配置方决定是否 adopt 既有 Session 续跑。
            result.extend(["--session-id", str(profile.options["dsh_session_arg"])])
        result.extend(["--json", "-"])
        result.extend(profile.extra_args)
        return result

    def thread_options(self, profile: Any, workspace_root: Path) -> dict[str, Any]:
        return {}

    def mcp_config(
        self, profile: Any, session_dir: Path, stage_name: str
    ) -> Path | None:
        # 节点装配 patch：options.mcp 目录 server + service_tools_mcp 的
        # dws-tools + skills 目录（skills_dir 由 runtime 在调用前落盘；
        # 这里只接 session_dir/skills 的既定约定）。模型层 provider 不在
        # 此层，仍走 options.dsh_patches 静态文件。
        servers = mcp_catalog.resolve_servers(profile.options.get("mcp"))
        servers = {
            name: mcp_catalog.apply_tool_gate(server)
            for name, server in servers.items()
        }
        if profile.options.get("service_tools_mcp"):
            servers["dws-tools"] = mcp_catalog.service_tools_server()
        skills_dir = session_dir / "skills"
        return mcp_catalog.write_dsh_session_patch(
            session_dir / "dws-session.patch.yml",
            servers=servers,
            skills_dir=skills_dir if skills_dir.is_dir() else None,
        )

    async def start(self, session: Any) -> None:
        # headless runner 见 task="-" 时阻塞读 stdin 到 EOF 作为完整任务
        # 文本（startup.js: program.args → "-" → internals.readStdin()）。
        # 写完立即关 stdin：无握手回合，runner 自驱动到退出。
        assert session.process is not None and session.process.stdin is not None
        session.process.stdin.write(
            (session.prepared.prompt or "").encode("utf-8")
        )
        await session.process.stdin.drain()
        session.process.stdin.close()

    async def steer(self, session: Any, prompt: str) -> bool:
        # 协议无运行中注入；返回 False 让服务层走重启续跑策略。
        return False

    async def abort(self, session: Any) -> None:
        # 无 abort 命令；close() 的 stdin 关闭 + 超时杀进程树即终止路径。
        return None

    async def parse_event(self, session: Any, event: Mapping[str, Any]) -> None:
        event_type = str(event.get("type") or "")
        if event_type == "session":
            # sessionId 是 dsh Session 持久 id：落进协议态留排障，
            # 后续 --session-id adopt 续跑时可用它核对身份。
            session._protocol_state["dsh_session_id"] = str(
                event.get("sessionId") or ""
            )
        elif event_type == "thinking":
            session._capture_progress(str(event.get("text") or ""))
            session._seal_live("message")
            session._capture_live("think", str(event.get("text") or ""))
        elif event_type == "text":
            text = str(event.get("text") or "")
            session._seal_live("think")
            session._capture_stream_delta(text)
            session._capture_live("message", text)
        elif event_type == "tool_call":
            call_id = str(event.get("callId") or "")
            tool_name = str(event.get("tool") or "tool")
            # dsh 流里 call/result 只带 callId 不带工具名：记进协议态，
            # _record_tool_result 按它回填面板条目。
            state = session._protocol_state
            state.setdefault("dsh_tool_names", {})[call_id] = tool_name
            session._seal_live("think", "message")
            session._capture_live(
                "tool",
                session._tool_args_summary(event.get("input")),
                name=tool_name,
            )
            # 服务工具信号在调用时刻捕获（与 pi 的 tool_execution_end
            # 对齐到"模型已决定"语义；dsh 工具事件没有独立的收尾阶段）。
            # report(final/blocked) 的收口延迟到 tool_result——文件在
            # server 侧执行时才落盘。
            if session._is_no_reply_tool(tool_name):
                session._capture_silent()
        elif event_type == "tool_result":
            state = session._protocol_state
            tool_name = state.get("dsh_tool_names", {}).get(
                str(event.get("callId") or ""), ""
            )
            if session._is_report_tool(tool_name):
                # final/blocked 的正文此刻已在 DWS_REPORT_FILE；progress
                # 不写文件，_capture_report 对缺失文件静默忽略。
                session._capture_report()
            session._record_tool_result(
                str(event.get("callId") or ""),
                bool(event.get("status") == "error"),
                str(event.get("result") or ""),
            )
        elif event_type == "status":
            await self._handle_status(session, event)
        elif event_type == "final":
            # final 是投影终稿，在 turn_end 之后才发出（runner 要先
            # summarize 完 durable log）。终稿与收口都在这里做：turn_end
            # 只记 reason；final 打完 runner 即 exit，缺失 final 时由
            # _watch_process 的退出码兜底（非零 → agent_exit_N）。
            text = str(event.get("text") or "")
            if text:
                session._capture_body(text, final=True)
            session._finish()
        # 未知事件类型（含投影 cap 打出的 truncated 变体）一律跳过。

    async def _handle_status(self, session: Any, event: Mapping[str, Any]) -> None:
        phase = str(event.get("phase") or "")
        if phase == "turn_start":
            session._seal_live("think", "message")
        elif phase == "step_end":
            session._seal_live("think", "message")
        elif phase == "turn_end":
            # 只记 reason，不收口：final 在 turn_end 之后才到（runner
            # summarize 完才打终稿并 exit）。error/blocked 语义在这里
            # 落进会话状态，收口统一由 final 事件或进程退出兜底。
            reason = event.get("reason")
            kind = str(reason.get("kind") or "") if isinstance(reason, Mapping) else ""
            if kind == "completed":
                session.error = ""
            elif kind in _ERROR_KINDS:
                failure = reason.get("error") if isinstance(reason, Mapping) else None
                message = (
                    str(failure.get("message") or "dsh turn error")
                    if isinstance(failure, Mapping)
                    else "dsh turn error"
                )
                code = (
                    str(failure.get("code") or "dsh_error")
                    if isinstance(failure, Mapping)
                    else "dsh_error"
                )
                session.error = session._sanitize(f"dsh_{code}: {message}", 500)
            elif kind in _ABNORMAL_KINDS:
                # blocked 对齐 report(blocked) 的展示语义；其余非正常
                # 收口一律按错误记录，让 fallback 链接手。
                if kind == "blocked":
                    session._mark_blocked()
                else:
                    session.error = f"dsh_turn_{kind}"
            else:
                # 插件扩展的新 reason 变体：不猜语义，按异常收口。
                session.error = f"dsh_turn_unknown_reason: {kind or 'missing'}"


__all__ = ["DshAdapter", "sys"]
