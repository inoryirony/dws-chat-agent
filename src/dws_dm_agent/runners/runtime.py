from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import signal
import subprocess
import sys
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import mcp_catalog
from .. import stages as stage_registry
from ..core import MAX_IMAGES, MAX_IMAGE_BYTES, sanitize_text
from ..skills_catalog import resolve_skills
from ..prompts import (
    SHARED_PROMPT_NAME,
    STAGE_PROMPT_VARIABLES,
    SUPPLEMENT_PROMPT_VARIABLES,
    render_prompt as _render_text,
    validate_template_variables,
)
from .adapters import (
    ADAPTERS as _ADAPTERS,
    DRIVER_PROTOCOLS as _DRIVER_PROTOCOLS,
    SERVICE_TOOL_PROTOCOL as _SERVICE_TOOL_PROTOCOL,
    SUPPORTED_PROTOCOLS,
    adapter_for as _adapter_for,
    default_driver as _default_driver,
)


# pipeline 编排空间：stage 名不再固定（front 只是惯例分诊槽位名），
# preset 配置驱动；worker 唯一必填。
# 协议行为（start/steer/abort/事件解析/命令构造）全部查 adapters 注册表：
# runtime 不再持有 protocol 字符串分叉。加新 CLI 接入只动 runners/adapters/。
# 服务工具名单保留在这里做提示词校验；协议锁死名单与 adapters 同源。
_SERVICE_TOOL_NAMES = ("no_reply", "task_record", "task_lookup", "report")
_WORKTREE_TIMEOUT_SECONDS = 600.0
# 事件流单行上限必须容纳"允许下载的整批图片原样回传"：pi RPC 会把带图用户
# 消息（含 base64 ImageContent）在 message/agent 事件里原样发回，单行最大为
# 全部图片的 base64 膨胀（4/3）+ JSON 结构与文本余量。上限从 core 的图片
# 预算推导，与 service 下载侧保持一致，不得各自硬编码或互相砍对方的上限。
_EVENT_STREAM_LIMIT = (
    (MAX_IMAGES * MAX_IMAGE_BYTES * 4 + 2) // 3 + 8 * 1024 * 1024
)
# pi-worker 冷启动（含扩展/MCP 加载）偶发超过默认 15s，首条 prompt 握手放宽到 3 分钟。
_PI_START_HANDSHAKE_TIMEOUT_SECONDS = 180.0
# 面板实时流：一轮会话的 think / 正文 / 工具调用按时间顺序留在内存里，
# 供看板展示"它现在在想什么、调了什么"。上限按行数与单行长度双封顶，
# 长会话不会把内存吃穿；只给面板看，不落库、不外发。
_LIVE_EVENT_LIMIT = 400
_LIVE_EVENT_CHARS = 1200
# pi RPC prompt/steer 命令的 images 字段使用 ImageContent：
# {"type": "image", "data": "<base64>", "mimeType": "..."}（pi docs/rpc.md）。
# service 层按魔数把图片落盘为正确后缀，这里按后缀映射 MIME。
_PI_IMAGE_MIME_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


def _pi_image_payloads(
    paths: Sequence[Path],
) -> tuple[dict[str, str], ...]:
    """把本地图片文件编码成 pi RPC 的 ImageContent 列表。

    不认识的后缀直接报错，而不是发一个 pi 端无法解析的 mimeType。
    """
    payloads: list[dict[str, str]] = []
    for path in paths:
        mime = _PI_IMAGE_MIME_TYPES.get(path.suffix.lower())
        if mime is None:
            raise ValueError(f"unsupported pi image format: {path.suffix}")
        payloads.append(
            {
                "type": "image",
                "data": base64.b64encode(path.read_bytes()).decode("ascii"),
                "mimeType": mime,
            }
        )
    return tuple(payloads)


@dataclass(frozen=True)
class AgentProfile:
    name: str
    driver: str
    protocol: str
    command: tuple[str, ...]
    model: str
    reasoning_effort: str
    read_only: bool
    timeout_seconds: float
    options: Mapping[str, Any]
    environment: Mapping[str, str]
    extra_args: tuple[str, ...]
    # 执行失败时按顺序切换的备用 agent 名（仅对 worker 阶段生效）
    fallbacks: tuple[str, ...] = ()
    # worker 阶段的 worktree 隔离仓（workspace_root 下的目录名；空=关闭）：
    # 每次运行在 worktree_root 下从该仓当前 HEAD 建独立 worktree。
    worktree_repo: str = ""

    @property
    def binary(self) -> str:
        """The executable checked by doctor; retained for old callers."""
        return self.command[0]


@dataclass(frozen=True)
class WorkflowStage:
    name: str
    label: str
    profile: AgentProfile
    prompt_path: Path


@dataclass(frozen=True)
class PreparedAgent:
    stage: WorkflowStage
    protocol: str
    argv: tuple[str, ...]
    cwd: Path
    environment: Mapping[str, str]
    prompt: str
    thread_options: Mapping[str, Any]
    result_path: Path
    events_path: Path
    stderr_path: Path
    prompt_path: Path
    local_image_paths: tuple[Path, ...] = ()
    # pi RPC 专用：随首条 prompt 原生发送的 ImageContent（base64 + mimeType）
    pi_images: tuple[dict[str, str], ...] = ()
    # pi RPC 专用：事项工具上下文文件。服务在 steer 进补充消息后会原子
    # 重写该文件，task_tools.py 每次调用优先读它。文件内的 message_times
    # （本运行已覆盖消息的 id -> 时间戳映射）是证据 ID 的校验集与
    # 时间戳来源，保证事项证据落在被引用消息自身的实际时间上。
    task_context_path: Path | None = None
    # pi RPC 专用：report 工具写 final/blocked 正文的文件；runtime 在
    # tool_execution_end 读取后按正文收口会话（与 silent 同一通路）。
    report_path: Path | None = None
    # worktree 隔离（worker 专用）：源仓与本次运行的 worktree 路径。
    # abort 清理 worktree，成功路径保留现场。
    worktree_source: Path | None = None
    worktree_path: Path | None = None

    @property
    def timeout_seconds(self) -> float:
        return self.stage.profile.timeout_seconds

class AgentSession:
    """One live agent process with a provider-neutral steering surface."""

    def __init__(self, prepared: PreparedAgent, session_id: str) -> None:
        self.prepared = prepared
        self.session_id = session_id
        self.process: asyncio.subprocess.Process | None = None
        self.thread_id = ""
        self.turn_id = ""
        self.error = ""
        self._complete = asyncio.Event()
        self._updates: asyncio.Queue[None] = asyncio.Queue(maxsize=1)
        self._closing = False
        self._request_id = 0
        self._pending: dict[int, asyncio.Future[Mapping[str, Any]]] = {}
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._watch_task: asyncio.Task[None] | None = None
        self._write_lock = asyncio.Lock()
        self._final_text = ""
        self._escalated = False
        self._silent = False
        self._report_outcome = ""
        self._progress: list[str] = []
        self._stream_text = ""
        self._live: list[dict[str, Any]] = []
        self._live_seq = 0
        # adapter 是共享单例，协议私有的会话状态一律挂这里；
        # 键名由各 adapter 自行约定（codex/pi/dsh 已在模块 docstring 声明）。
        self._protocol_state: dict[str, Any] = {
            "item_phases": {},
            "reasoning": {},
            "pi_thinking": {},
        }
        # 协议行为入口：按 prepared.protocol 查 adapters 注册表。
        self.adapter = _adapter_for(prepared.protocol)

    @property
    def exit_code(self) -> int | None:
        return self.process.returncode if self.process else None

    @property
    def done(self) -> bool:
        return self._complete.is_set()

    @property
    def latest_progress(self) -> str:
        return self._progress[-1] if self._progress else ""

    @property
    def latest_stream_text(self) -> str:
        return self._stream_text

    @property
    def live_events(self) -> list[dict[str, Any]]:
        """面板实时流：think / 正文 / 工具调用按发生顺序排列。

        返回副本，调用方（看板投影）不能借引用改动会话内部状态。
        """
        return [dict(item) for item in self._live]

    @property
    def escalated(self) -> bool:
        return self._escalated

    @property
    def silent(self) -> bool:
        return self._silent

    @property
    def blocked(self) -> bool:
        """report(blocked) 结束的运行：正文照常交付，结果记为 blocked。"""
        return self._report_outcome == "blocked"

    async def start(self) -> None:
        if self.process is not None:
            raise RuntimeError("agent session already started")
        kwargs: dict[str, Any] = {}
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            kwargs["start_new_session"] = True
        self.process = await asyncio.create_subprocess_exec(
            *self.prepared.argv,
            cwd=self.prepared.cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=_EVENT_STREAM_LIMIT,
            env=dict(self.prepared.environment),
            **kwargs,
        )
        self._reader_task = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._read_stderr())
        self._watch_task = asyncio.create_task(self._watch_process())
        try:
            await self.adapter.start(self)
        except Exception:
            await self.close()
            # 启动失败等同于中止：没有可保留的运行现场，worktree 一并回收。
            await self._remove_worktree()
            raise

    async def wait(self, timeout: float | None = None) -> None:
        if timeout is None:
            await self._complete.wait()
        else:
            await asyncio.wait_for(self._complete.wait(), timeout=timeout)

    async def wait_for_update(self, timeout: float | None = None) -> None:
        if timeout is None:
            await self._updates.get()
        else:
            await asyncio.wait_for(self._updates.get(), timeout=timeout)

    async def steer(self, prompt: str) -> bool:
        if not prompt.strip() or self.done or self.process is None:
            return False
        try:
            return await self.adapter.steer(self, prompt)
        except (BrokenPipeError, ConnectionError, RuntimeError, TimeoutError):
            return False

    def result(self) -> str:
        if self._escalated:
            return ""
        # no_reply 是显式控制动作：捕获后不再有任何路径能把正文当回复交付。
        if self._silent:
            raise ValueError("agent chose silence")
        # 只把真正的最终 assistant 消息当回复；thinking/progress 流文本不参与回退，
        # 避免把内部独白当正式回复发给联系人。无终稿时抛错交由服务层走 fallback 链。
        reply = self._final_text.strip()
        if not reply:
            raise ValueError("agent produced no reply or escalation")
        return reply

    async def abort(self) -> None:
        if self.process is not None and self.process.returncode is None:
            with suppress(Exception):
                await self.adapter.abort(self)
        await self.close()
        # abort 清理 worktree：被中止的运行没有可保留的现场；成功路径的
        # close() 不走这里，现场留给操作者检查。清理失败只记日志，不掩盖
        # abort 本身的结果。
        await self._remove_worktree()

    async def _remove_worktree(self) -> None:
        prepared = self.prepared
        if (
            prepared.worktree_path is None
            or prepared.worktree_source is None
            or not prepared.worktree_path.exists()
        ):
            return
        try:
            completed = await asyncio.to_thread(
                subprocess.run,
                [
                    "git", "-C", str(prepared.worktree_source),
                    "worktree", "remove", "--force", str(prepared.worktree_path),
                ],
                capture_output=True,
                text=True,
                timeout=_WORKTREE_TIMEOUT_SECONDS,
            )
            if completed.returncode != 0:
                logging.getLogger(__name__).warning(
                    "worktree remove failed path=%s detail=%s",
                    prepared.worktree_path,
                    completed.stderr.strip()[:300],
                )
        except (OSError, subprocess.SubprocessError) as exc:
            logging.getLogger(__name__).warning(
                "worktree remove failed path=%s error=%s",
                prepared.worktree_path,
                exc,
            )

    async def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        process = self.process
        if process and process.returncode is None:
            if process.stdin:
                process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), timeout=2)
            except TimeoutError:
                await _terminate_process_tree(process)
        tasks = [
            task
            for task in (self._reader_task, self._stderr_task, self._watch_task)
            if task is not None and task is not asyncio.current_task()
        ]
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for future in self._pending.values():
            if not future.done():
                future.cancel()
        self._pending.clear()

    async def _jsonrpc_call(
        self, method: str, params: Mapping[str, Any], *, timeout: float = 15
    ) -> Mapping[str, Any]:
        self._request_id += 1
        request_id = self._request_id
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        await self._write_json(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": _without_none(params),
            }
        )
        try:
            response = await asyncio.wait_for(future, timeout=timeout)
        finally:
            self._pending.pop(request_id, None)
        if response.get("error"):
            raise RuntimeError(str(response["error"]))
        return response

    async def _pi_call(
        self, command: Mapping[str, Any], *, timeout: float = 15
    ) -> Mapping[str, Any]:
        self._request_id += 1
        request_id = self._request_id
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        await self._write_json({"id": request_id, **command})
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            self._pending.pop(request_id, None)

    async def _custom_call(
        self, command: Mapping[str, Any], *, timeout: float = 15
    ) -> Mapping[str, Any]:
        self._request_id += 1
        request_id = self._request_id
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        await self._write_json({"id": request_id, **command})
        try:
            response = await asyncio.wait_for(future, timeout=timeout)
        finally:
            self._pending.pop(request_id, None)
        if response.get("type") != "response":
            raise RuntimeError("custom agent response must use type=response")
        return response

    async def _write_json(self, value: Mapping[str, Any]) -> None:
        if not self.process or not self.process.stdin or self.process.returncode is not None:
            raise BrokenPipeError("agent process is not writable")
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        async with self._write_lock:
            self.process.stdin.write((payload + "\n").encode("utf-8"))
            await self.process.stdin.drain()

    async def _read_stdout(self) -> None:
        assert self.process and self.process.stdout
        try:
            while line := await self.process.stdout.readline():
                self.prepared.events_path.parent.mkdir(parents=True, exist_ok=True)
                with self.prepared.events_path.open("ab") as stream:
                    stream.write(line)
                try:
                    event = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if not isinstance(event, Mapping):
                    continue
                request_id = event.get("id")
                if isinstance(request_id, int) and request_id in self._pending:
                    future = self._pending[request_id]
                    if not future.done():
                        future.set_result(event)
                    continue
                await self._handle_event(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self._closing:
                self._fail(f"event_stream_error: {exc}")

    async def _read_stderr(self) -> None:
        assert self.process and self.process.stderr
        try:
            while line := await self.process.stderr.readline():
                self.prepared.stderr_path.parent.mkdir(parents=True, exist_ok=True)
                with self.prepared.stderr_path.open("ab") as stream:
                    stream.write(line)
        except asyncio.CancelledError:
            raise

    async def _watch_process(self) -> None:
        assert self.process
        code = await self.process.wait()
        if self._reader_task is not None and not self._reader_task.done():
            await self._reader_task
        if not self._closing and not self.done:
            self._fail(f"agent_exit_{code}")

    async def _handle_event(self, event: Mapping[str, Any]) -> None:
        """协议事件统一入口：解析逻辑在 adapter，会话只保留通用管线。

        adapter 无状态、单例共享；会话私有状态挂 _protocol_state。
        """
        try:
            await self.adapter.parse_event(self, event)
        except Exception as exc:
            if not self._closing:
                self._fail(f"event_stream_error: {exc}")

    def _capture_body(self, text: str, *, final: bool = False) -> None:
        clean = text.strip()
        if not clean:
            return
        if final:
            self._final_text = clean
            return
        self._stream_text = clean
        self._signal_update()

    def _capture_progress(self, text: str) -> None:
        progress = sanitize_text(text, 500).strip()
        if progress and (not self._progress or self._progress[-1] != progress):
            self._progress.append(progress)
            self._signal_update()

    def _capture_live(self, kind: str, text: str, *, name: str = "") -> None:
        """记一条面板实时流事件（think / message / tool / tool_result）。

        与 _capture_progress 的区别：progress 是给联系人卡片的稀疏里程碑
        （走 report 工具），这里是给操作者看的原始流水——同一段文字可以
        既是 think 又有工具调用，两条都留。同 kind 且同 name 的连续
        delta 合并成一条，避免 token 级写入把缓冲刷满。
        """
        body = str(text or "")
        if not body.strip():
            return
        # 流式 delta 保留首尾空白：token 拼接靠它，strip 会把单词粘在一起。
        if kind not in {"think", "message"}:
            body = body.strip()
        if len(body) > _LIVE_EVENT_CHARS:
            body = body[:_LIVE_EVENT_CHARS] + "…"
        last = self._live[-1] if self._live else None
        if (
            last is not None
            and last["kind"] == kind
            and last.get("name", "") == name
            and last.get("streaming")
        ):
            merged = f"{last['text']}{body}"
            last["text"] = merged[-_LIVE_EVENT_CHARS:]
            return
        self._live_seq += 1
        self._live.append(
            {
                "seq": self._live_seq,
                "kind": kind,
                "name": name,
                "text": body,
                "streaming": kind in {"think", "message"},
                "at": datetime.now().astimezone().isoformat(),
            }
        )
        if len(self._live) > _LIVE_EVENT_LIMIT:
            del self._live[: len(self._live) - _LIVE_EVENT_LIMIT]
        self._signal_update()

    def _record_tool_start(self, event: Mapping[str, Any]) -> None:
        name = str(event.get("toolName") or event.get("name") or "tool")
        # 工具开始意味着一轮陈述结束：把 think/message 定稿，后续另起一条。
        self._seal_live("think", "message")
        self._capture_live(
            "tool", _tool_args_summary(event.get("args")), name=name
        )

    def _record_tool_end(self, event: Mapping[str, Any]) -> None:
        name = str(event.get("toolName") or event.get("name") or "tool")
        failed = bool(event.get("isError"))
        # 结果并回对应的开始条目：面板上一行就是一个工具调用，不再多一行。
        for entry in reversed(self._live):
            if (
                entry["kind"] == "tool"
                and entry.get("name") == name
                and not entry.get("finished")
            ):
                entry["finished"] = True
                entry["failed"] = failed
                return
        self._capture_live("tool", "完成" if not failed else "失败", name=name)

    def _record_tool_result(self, call_id: str, failed: bool, result: str) -> None:
        """按 callId 并回工具结果（dsh 流形态：call/result 分离，无名字）。"""
        state = self._protocol_state
        name = state.get("dsh_tool_names", {}).get(call_id, "tool")
        for entry in reversed(self._live):
            if (
                entry["kind"] == "tool"
                and not entry.get("finished")
                and entry.get("name") == name
                and entry.get("call_id", call_id) == call_id
            ):
                entry["finished"] = True
                entry["failed"] = failed
                return
        self._capture_live("tool", "完成" if not failed else "失败", name=name)

    def _mark_blocked(self) -> None:
        """非 pi 协议的 blocked 终态（如 dsh turn_end blocked）：结果记
        blocked，正文照常交付——语义对齐 pi 的 report(blocked)。"""
        self._report_outcome = "blocked"

    def _seal_live(self, *kinds: str) -> None:
        """结束指定类型的流式条目：之后同 kind 的 delta 另起一条。"""
        wanted = set(kinds) if kinds else None
        for entry in self._live:
            if wanted is None or entry["kind"] in wanted:
                entry["streaming"] = False

    # —— adapter 门面：协议解析层（runners/adapters/*）经这些方法读写会话，
    # 不直接摸内部字段。全部是既有内部实现的薄委托，行为不变。

    @staticmethod
    def _sanitize(text: str, limit: int) -> str:
        return sanitize_text(text, limit)

    @staticmethod
    def _is_escalate_tool(value: Any) -> bool:
        return _is_escalate_tool(value)

    @staticmethod
    def _is_no_reply_tool(value: Any) -> bool:
        return _is_no_reply_tool(value)

    @staticmethod
    def _is_report_tool(value: Any) -> bool:
        return _is_report_tool(value)

    @staticmethod
    def _assistant_text(event: Mapping[str, Any]) -> str:
        return _assistant_text(event)

    @staticmethod
    def _tool_args_summary(args: Any) -> str:
        return _tool_args_summary(args)

    def _close_live_streams(self) -> None:
        """一轮结束：所有流式条目定稿。"""
        self._seal_live()

    def _capture_escalation(self) -> None:
        self._escalated = True
        self._complete.set()
        self._signal_update()

    def _capture_silent(self) -> None:
        self._silent = True
        self._complete.set()
        self._signal_update()

    def _capture_report(self) -> None:
        """读取 report 工具落盘的 {kind, text} 并按终稿收口会话。

        progress 调用不写文件也不终止（文件缺失直接忽略，模型继续跑）；
        final/blocked 的文件存在但不可解析说明扩展与 runtime 的交接断了：
        按失败收口（错误可见、可走 fallback），不能静默当无终稿处理。
        """
        if self.prepared.report_path is None:
            return
        try:
            raw = self.prepared.report_path.read_text(encoding="utf-8")
        except OSError:
            return
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            self._fail(f"report_result_invalid: {exc}")
            return
        kind = str(payload.get("kind") or "")
        text = str(payload.get("text") or "").strip()
        if kind not in {"final", "blocked"} or not text:
            self._fail("report_result_invalid: bad payload")
            return
        self._report_outcome = kind
        self._capture_body(text, final=True)
        self._complete.set()
        self._signal_update()

    def _capture_stream_delta(self, delta: str) -> None:
        if not delta:
            return
        self._stream_text += delta
        self._signal_update()

    def _signal_update(self) -> None:
        if self._updates.empty():
            self._updates.put_nowait(None)

    def _finish(self) -> None:
        # result.txt 仅作排障留档：无终稿时保留流文本便于回溯，但不作为交付。
        self._close_live_streams()
        payload = self._final_text or self._stream_text
        if payload:
            self.prepared.result_path.write_text(payload, encoding="utf-8")
        if (
            not self._final_text
            and not self.error
            and not self._escalated
            and not self._silent
        ):
            self.error = "agent_result_missing"
        self._complete.set()
        self._signal_update()

    def _fail(self, error: str) -> None:
        self.error = error
        failure = RuntimeError(error)
        for future in self._pending.values():
            if not future.done():
                future.set_exception(failure)
        self._complete.set()
        self._signal_update()


class WorktreeError(RuntimeError):
    """worktree 隔离失败：运行按 failed 收口，不允许 fallback 链在
    未隔离的主仓上重试同一段写操作。"""


class AgentRuntime:
    """Loads one two-stage workflow and hides provider-specific protocols."""

    def __init__(
        self,
        name: str,
        stages: Mapping[str, WorkflowStage],
        profiles: Mapping[str, AgentProfile],
        config_dir: Path,
        workspace_root: Path,
        supplement_strategy: str,
        supplement_prompt_path: Path,
        auto_messages: Mapping[str, Any],
        worktree_root: Path | None = None,
        skill_pool: Mapping[str, Any] | None = None,
        pipeline: Sequence[str] = (),
    ) -> None:
        self.name = name
        self._stages = dict(stages)
        self._profiles = dict(profiles)
        self._config_dir = config_dir
        self._workspace_root = workspace_root
        self._worktree_root = worktree_root or workspace_root / "worktrees"
        self._skill_pool = dict(skill_pool) if skill_pool else {}
        self.skill_pool: Mapping[str, Any] = self._skill_pool
        # 编排空间：有序 step 列表（gate/deliver 内置名 + stage 槽位名），
        # service 主循环按序遍历。缺省推导保持"front→worker"现行为。
        self.pipeline: tuple[str, ...] = tuple(pipeline) or (
            "gate", "worker", "deliver"
        )
        self.supplement_strategy = supplement_strategy
        self._supplement_prompt_path = supplement_prompt_path
        self._auto_messages = dict(auto_messages)
        self.progress_enabled = bool(auto_messages.get("progress_enabled", True))
        self.max_progress_updates = int(auto_messages.get("max_progress_updates", 60))

    @classmethod
    def from_config(
        cls,
        raw: Mapping[str, Any],
        config_path: Path,
        workspace_root: Path,
    ) -> AgentRuntime:
        profile_values = raw.get("agents")
        workflow_values = raw.get("workflows")
        if not isinstance(profile_values, Mapping) or not isinstance(workflow_values, Mapping):
            raise ValueError("agents and workflows configuration are required")
        profiles: dict[str, AgentProfile] = {}
        for name, value in profile_values.items():
            if not isinstance(value, Mapping):
                raise ValueError(f"agent profile must be an object: {name}")
            driver = str(value.get("driver") or "").lower().replace("_", "-")
            if driver == "claude-code":
                driver = "claude"
            protocol = str(value.get("protocol") or "").lower().replace("_", "-")
            if not protocol:
                protocol = _DRIVER_PROTOCOLS.get(driver, "")
            if protocol not in _ADAPTERS:
                raise ValueError(
                    f"unsupported agent protocol: {protocol or driver or name}"
                )
            if not driver:
                driver = _default_driver(protocol)
            timeout = float(value.get("timeout_seconds", 60))
            if timeout < 0:
                raise ValueError(f"agent timeout must be non-negative: {name}")
            environment = value.get("environment", {})
            options = value.get("options", {})
            extra_args = value.get("extra_args", [])
            if not isinstance(environment, Mapping) or not isinstance(options, Mapping):
                raise ValueError(f"agent environment/options must be objects: {name}")
            if not isinstance(extra_args, list):
                raise ValueError(f"agent extra_args must be an array: {name}")
            command_value = value.get("command")
            if command_value is None:
                command = (str(value.get("binary") or driver),)
            else:
                if not isinstance(command_value, list) or not command_value:
                    raise ValueError(f"agent command must be a non-empty array: {name}")
                if any(
                    not isinstance(item, str) or not item.strip()
                    for item in command_value
                ):
                    raise ValueError(
                        f"agent command entries must be non-empty strings: {name}"
                    )
                command = tuple(command_value)
            fallbacks_raw = value.get("fallbacks", [])
            if not isinstance(fallbacks_raw, list) or any(
                not isinstance(item, str) or not item.strip()
                for item in fallbacks_raw
            ):
                raise ValueError(
                    f"agent fallbacks must be an array of agent names: {name}"
                )
            profiles[str(name)] = AgentProfile(
                name=str(name),
                driver=driver,
                protocol=protocol,
                command=command,
                model=str(value.get("model") or ""),
                reasoning_effort=str(value.get("reasoning_effort") or ""),
                read_only=bool(value.get("read_only", False)),
                timeout_seconds=timeout,
                options=dict(options),
                environment={str(key): str(item) for key, item in environment.items()},
                extra_args=tuple(str(item) for item in extra_args),
                fallbacks=tuple(str(item).strip() for item in fallbacks_raw),
                worktree_repo=str(value.get("worktree_repo") or ""),
            )
        active = str(workflow_values.get("active") or "")
        presets = workflow_values.get("presets")
        preset = presets.get(active) if isinstance(presets, Mapping) else None
        if not active or not isinstance(preset, Mapping):
            raise ValueError(f"active workflow preset does not exist: {active or '(empty)'}")
        supplement_strategy = str(preset.get("supplement_strategy") or "steer")
        if supplement_strategy not in {"steer", "restart_with_context"}:
            raise ValueError(f"unsupported supplement strategy: {supplement_strategy}")
        auto_messages = preset.get("auto_messages", {})
        if not isinstance(auto_messages, Mapping):
            raise ValueError("workflow auto_messages must be an object")
        if int(auto_messages.get("max_progress_updates", 60)) < 0:
            raise ValueError("max progress updates must not be negative")
        config_dir = config_path.resolve().parent
        skill_pool = raw.get("skills")
        skill_pool = dict(skill_pool) if isinstance(skill_pool, Mapping) else {}
        worktree_value = raw.get("worktree_root")
        worktree_root = (
            _resolve_path(config_dir, worktree_value) if worktree_value else None
        )
        supplement_prompt_path = _resolve_path(
            config_dir, preset.get("supplement_prompt") or "prompts/supplement.md"
        )
        if not supplement_prompt_path.is_file():
            raise ValueError(
                f"workflow supplement prompt does not exist: {supplement_prompt_path}"
            )
        # pipeline 编排空间：至少一个 executive kind 的 stage（惯例名
        # worker），其余 stage 名自由——preset 里写什么键就有什么 step。
        # service 侧经 stage_of_kind(<kind>) 探测，无配置即无该 step。
        stages: dict[str, WorkflowStage] = {}
        stage_names = [name for name in preset if isinstance(preset.get(name), Mapping)]
        if not any(
            stage_registry.stage_spec(str(name)).executive for name in stage_names
        ):
            raise ValueError(f"workflow {active} requires an executive stage")
        for stage_name in stage_names:
            value = preset.get(stage_name)
            if stage_name in {"supplement_prompt", "supplement_strategy", "auto_messages"}:
                continue
            profile_name = str(value.get("agent") or "")
            profile = profiles.get(profile_name)
            if profile is None:
                raise ValueError(f"workflow stage references unknown agent: {profile_name}")
            prompt_path = _resolve_path(config_dir, value.get("prompt"))
            if not prompt_path.is_file():
                raise ValueError(f"workflow prompt does not exist: {prompt_path}")
            stages[stage_name] = WorkflowStage(
                name=stage_name,
                label=str(value.get("label") or ("前置模型" if stage_name == "front" else "模型")),
                profile=profile,
                prompt_path=prompt_path,
            )
        for name, stage in stages.items():
            spec = stage_registry.stage_spec(name)
            if spec.kind == "triage" and not stage.profile.read_only:
                raise ValueError(
                    f"workflow triage stage {name} must set read_only=true"
                )
            if spec.executive and stage.profile.read_only:
                raise ValueError(
                    f"workflow executive stage {name} must set read_only=false"
                )
        pipeline = _resolve_pipeline(preset, stages)
        for profile in profiles.values():
            unknown = [
                fallback
                for fallback in profile.fallbacks
                if fallback not in profiles
            ]
            if unknown:
                raise ValueError(
                    f"agent {profile.name} references unknown fallback agents: "
                    f"{', '.join(unknown)}"
                )
        # 服务工具校验按阶段进行：只有该阶段实际看到的提示词（executive
        # 阶段的 shared.md 公共段 + 阶段提示词，加上会 steer 进任一在跑
        # 阶段会话的补充提示词）引用了服务工具，该阶段实际执行的 agent
        # （阶段 agent 及其备用链）才要求全部能挂载；提示词不引用服务
        # 工具的阶段不受影响。全局合并所有提示词校验会把没有工具指令
        # 的阶段一并锁死。挂载通路有三条：pi 原生扩展、service_tools_mcp
        # （dws-tools stdio server）、task_cli（bash 拉起 task_tools.py，
        # 覆盖 task_record/task_lookup 子集）。
        supplement_referenced = {
            name
            for name in _SERVICE_TOOL_NAMES
            if name in supplement_prompt_path.read_text(encoding="utf-8")
        }
        for stage in stages.values():
            referenced = set(supplement_referenced)
            referenced.update(
                name
                for name in _SERVICE_TOOL_NAMES
                if name in stage.prompt_path.read_text(encoding="utf-8")
            )
            if stage_registry.stage_spec(stage.name).executive:
                shared_path = stage.prompt_path.parent / SHARED_PROMPT_NAME
                if shared_path.is_file():
                    referenced.update(
                        name
                        for name in _SERVICE_TOOL_NAMES
                        if name in shared_path.read_text(encoding="utf-8")
                    )
            if not referenced:
                continue
            for agent in [
                stage.profile,
                *(profiles[name] for name in stage.profile.fallbacks),
            ]:
                # task_cli 运行时（如 dsh）由 prompt 指教模型经 bash 拉起
                # task_tools.py，task_record/task_lookup 与 pi 同通路可用；
                # service_tools_mcp 挂载 dws-tools stdio server，四个服务
                # 工具全部与 pi 同名同后端（report/no_reply 经 DWS_REPORT_FILE
                # 与工具事件收口）。
                cli_capable = bool(agent.options.get("task_cli"))
                tools_mounted = bool(agent.options.get("service_tools_mcp"))
                unsupported = {
                    name
                    for name in referenced
                    if agent.protocol != _SERVICE_TOOL_PROTOCOL
                    and not tools_mounted
                    and not (cli_capable and name in {"task_record", "task_lookup"})
                }
            if unsupported:
                raise ValueError(
                    f"workflow {active} stage {stage.name} prompts reference "
                    f"service tools ({', '.join(sorted(unsupported))}) but "
                    f"agent {agent.name} (protocol {agent.protocol}) cannot "
                    "mount them; use a pi agent for stages whose prompts "
                    "use these tools, enable options.service_tools_mcp "
                    "(dws-tools MCP server) or options.task_cli for the "
                    "task_record/task_lookup subset, or remove the "
                    "service-tool instructions from the prompts"
                    )
        return cls(
            active,
            stages,
            profiles,
            config_dir,
            workspace_root.resolve(),
            supplement_strategy,
            supplement_prompt_path,
            auto_messages,
            worktree_root=worktree_root,
            skill_pool=skill_pool,
            pipeline=pipeline,
        )

    def render(self, stage_name: str, variables: Mapping[str, Any]) -> str:
        template = self._stage(stage_name).prompt_path.read_text(encoding="utf-8")
        return _render_text(template, variables)

    def stage_of_kind(self, kind: str) -> str | None:
        """第一个属于该 kind 的 stage 槽位名（preset 键序），无则 None。

        kind 语义见 stages.py：triage（惯例名 front）/ executive（惯例名
        worker）。service 派发只认 kind，不认具体槽位名。
        """
        for name in self._stages:
            if stage_registry.stage_spec(name).kind == kind:
                return name
        return None

    def executive_stage(self) -> str:
        stage = self.stage_of_kind("executive")
        if stage is None:
            raise ValueError(f"workflow {self.name} has no executive stage")
        return stage

    def render_supplement(self, variables: Mapping[str, Any]) -> str:
        return _render_text(
            self._supplement_prompt_path.read_text(encoding="utf-8"), variables
        )

    def render_auto_message(self, name: str, variables: Mapping[str, Any]) -> str:
        if name == "ack" and not bool(self._auto_messages.get("ack_enabled", True)):
            return ""
        return _render_text(str(self._auto_messages.get(name) or ""), variables)

    @property
    def ack_message(self) -> str:
        if not bool(self._auto_messages.get("ack_enabled", True)):
            return ""
        return str(self._auto_messages.get("ack") or "")

    def prepare(
        self,
        stage_name: str,
        session_id: str,
        prompt: str,
        session_dir: Path,
        local_images: Sequence[Path] = (),
        model_override: str | None = None,
        agent_override: str | None = None,
        task_context: Mapping[str, Any] | None = None,
    ) -> PreparedAgent:
        stage = self.resolve_stage(stage_name, agent_override)
        spec = stage_registry.stage_spec(stage_name)
        session_dir.mkdir(parents=True, exist_ok=True)
        prompt_path = session_dir / "prompt.md"
        result_path = session_dir / "result.txt"
        events_path = session_dir / "events.jsonl"
        stderr_path = session_dir / "stderr.log"
        prompt_path.write_text(prompt, encoding="utf-8")
        protocol = stage.profile.protocol
        pi_images: tuple[dict[str, str], ...] = ()
        if local_images and protocol == "pi-rpc":
            # pi RPC 原生支持 prompt.images（ImageContent：base64 + mimeType），
            # 在此一次性编码，首条 prompt 由 AgentSession 直接携带图片。
            pi_images = _pi_image_payloads(local_images)
            local_images = ()
        elif local_images and protocol != "codex-app-server":
            # 其余协议不支持原生图片输入，应在上游 _run_agent 中将图片路径
            # 嵌入 prompt；这里只保留防御性日志，不再硬阻断。
            logging.getLogger(__name__).warning(
                "local_images passed to non-codex protocol %s; images=%d",
                protocol,
                len(local_images),
            )
        skills = resolve_skills(self._skill_pool, stage.profile.options.get("skills"))
        # skill 通道是运行时装配（session 目录 + argv flag），协议 adapter
        # 不感知：pi 原生 --skill 指向池内目录；其余协议先落盘再由各自
        # 原生机制发现（dsh patch customSkillDirs / codex CODEX_HOME /
        # claude --plugin-dir），argv 片段在这里拼。落盘必须先于
        # mcp_config：dsh 的 session patch 要引用 skills 目录。
        skill_args = _protocol_skill_args(protocol, skills, session_dir)
        _prepare_protocol_skills(protocol, skills, session_dir)
        mcp_config_path = _prepare_mcp_config(
            stage.profile, protocol, session_dir, stage_name
        )
        argv = [
            *_command(
                stage.profile,
                mcp_config_path,
                model_override,
                mount_escalate=spec.mount_escalate,
            ),
            *skill_args,
        ]
        # codex 全 stage 走 per-session CODEX_HOME：front 隔离运行环境，
        # worker 顺带获得 skills/ symlink 挂载位，不再依赖全局 ~/.codex。
        environment = self._environment(
            stage.profile,
            session_dir / "codex-home" if protocol == "codex-app-server" else None,
        )
        if mcp_config_path is not None:
            # pi 走 adapter：exclusive 让 --mcp-config 成为唯一来源，不串全局 server。
            environment["PI_MCP_CONFIG_MODE"] = "exclusive"
            environment["DWS_ESCALATE_MCP_CONFIG"] = str(mcp_config_path)
            environment["DWS_ESCALATE_PYTHON"] = sys.executable
            environment["DWS_ESCALATE_MCP_SCRIPT"] = str(
                Path(__file__).with_name("escalate_mcp.py").resolve()
            )
        task_context_path: Path | None = None
        report_path: Path | None = None
        if (
            protocol == "pi-rpc"
            or stage.profile.options.get("task_cli")
            or stage.profile.options.get("service_tools_mcp")
        ) and task_context is not None:
            # 事项工具的作用域与运行信息只经环境变量注入执行面（pi 走
            # pi_dws_tools.ts 扩展；task_cli 运行时由 prompt 指教模型用 bash
            # 拉起 task_tools.py；service_tools_mcp 走 dws-tools stdio server），
            # 不进入 prompt，模型无法伪造其他作用域。
            # DWS_TASK_CONTEXT 是会话启动时的初始快照；DWS_TASK_CONTEXT_FILE
            # 指向的文件由服务在 steer 后原子重写，task_tools.py 每次调用
            # 优先读它，证据消息集合随本运行覆盖的消息扩展。
            environment["DWS_TASK_PYTHON"] = sys.executable
            environment["DWS_TASK_SCRIPT"] = str(
                Path(__file__).with_name("task_tools.py").resolve()
            )
            environment["DWS_TASK_CONTEXT"] = json.dumps(
                dict(task_context), ensure_ascii=False
            )
            task_context_path = session_dir / "task-context.json"
            task_context_path.write_text(
                environment["DWS_TASK_CONTEXT"], encoding="utf-8"
            )
            environment["DWS_TASK_CONTEXT_FILE"] = str(task_context_path)
            # report(final/blocked) 的正文经该文件传回 runtime（terminate
            # 之后的最后一条 assistant 消息带 toolCall，不能当终稿）。
            report_path = session_dir / "report.json"
            environment["DWS_REPORT_FILE"] = str(report_path)
            if stage.profile.options.get("service_tools_mcp"):
                # dsh 的 dws-tools 经 patch yml 拉起 stdio server，
                # 路径/解释器只能经 env 传（!!js 表达式读取）；
                # claude/codex 走 session mcp.json，不消费这两个键。
                environment["DWS_TOOLS_PYTHON"] = sys.executable
                environment["DWS_TOOLS_SCRIPT"] = str(
                    mcp_catalog.tools_script_path()
                )
        cwd = self._workspace_root
        worktree_source: Path | None = None
        worktree_path: Path | None = None
        if spec.use_worktree and stage.profile.worktree_repo:
            worktree_source, worktree_path = self._add_worktree(
                stage.profile.worktree_repo, session_id
            )
            cwd = worktree_path
            environment["DWS_WORKTREE"] = str(worktree_path)
        return PreparedAgent(
            stage=stage,
            protocol=protocol,
            argv=tuple(argv),
            cwd=cwd,
            environment=environment,
            prompt=prompt,
            thread_options=_thread_options(stage.profile, self._workspace_root),
            result_path=result_path,
            events_path=events_path,
            stderr_path=stderr_path,
            prompt_path=prompt_path,
            local_image_paths=tuple(local_images),
            pi_images=pi_images,
            task_context_path=task_context_path,
            report_path=report_path,
            worktree_source=worktree_source,
            worktree_path=worktree_path,
        )

    def _add_worktree(self, repo: str, session_id: str) -> tuple[Path, Path]:
        """从 workspace_root/<repo> 当前 HEAD 建本次运行专属的 worktree。

        失败抛 WorktreeError：隔离不可用时宁可整场 failed，也不能让模型
        在共享主仓上执行写操作（fallback 链同样被拦下）。
        """
        source = self._workspace_root / repo
        if not source.is_dir() or not (source / ".git").exists():
            raise WorktreeError(f"worktree_repo is not a git repository: {repo}")
        # 会话 id 尾段是每次运行唯一的 uuid 随机部：同日同仓的两次运行
        # 绝不共用 worktree（日期前缀在一天内必然相同，等于必撞）。
        target = self._worktree_root / f"{repo}-{session_id[-12:]}"
        try:
            self._worktree_root.mkdir(parents=True, exist_ok=True)
            completed = subprocess.run(
                [
                    "git", "-C", str(source),
                    "worktree", "add", "--detach", str(target), "HEAD",
                ],
                capture_output=True,
                text=True,
                timeout=_WORKTREE_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise WorktreeError(f"git worktree add failed: {exc}") from exc
        if completed.returncode != 0:
            raise WorktreeError(
                f"git worktree add failed: {completed.stderr.strip()[:300]}"
            )
        return source, target

    def open_session(
        self,
        stage_name: str,
        session_id: str,
        prompt: str,
        session_dir: Path,
        local_images: Sequence[Path] = (),
        model_override: str | None = None,
        agent_override: str | None = None,
        task_context: Mapping[str, Any] | None = None,
    ) -> AgentSession:
        return AgentSession(
            self.prepare(
                stage_name,
                session_id,
                prompt,
                session_dir,
                local_images,
                model_override=model_override,
                agent_override=agent_override,
                task_context=task_context,
            ),
            session_id,
        )

    def profile_name(self, stage_name: str) -> str:
        return self._stage(stage_name).profile.name

    def has_stage(self, stage_name: str) -> bool:
        return stage_name in self._stages

    def required_binaries(self) -> dict[str, str]:
        return {
            stage.profile.name: stage.profile.command[0]
            for stage in self._stages.values()
        }

    def describe(self) -> dict[str, Any]:
        active_profiles = {stage.profile.name for stage in self._stages.values()}
        return {
            "name": self.name,
            "supplementStrategy": self.supplement_strategy,
            "supplementPromptPath": os.path.relpath(
                self._supplement_prompt_path, self._config_dir
            ),
            "supplementPrompt": self._supplement_prompt_path.read_text(encoding="utf-8"),
            "autoMessages": {
                "ack": self.ack_message,
                "progressEnabled": self.progress_enabled,
                "maxProgressUpdates": self.max_progress_updates,
            },
            "stages": [
                {
                    "id": name,
                    "label": self._stages[name].label,
                    "agent": self._stages[name].profile.name,
                    "driver": self._stages[name].profile.driver,
                    "protocol": self._stages[name].profile.protocol,
                    "launchCommand": list(self._stages[name].profile.command),
                    "nativeSteer": True,
                    "model": self._stages[name].profile.model,
                    "reasoningEffort": self._stages[name].profile.reasoning_effort,
                    "readOnly": self._stages[name].profile.read_only,
                    "promptPath": os.path.relpath(
                        self._stages[name].prompt_path, self._config_dir
                    ),
                    "prompt": self._stages[name].prompt_path.read_text(encoding="utf-8"),
                }
                for name in self._stages
                if name in self._stages
            ],
            "availableAgents": [
                {
                    "name": profile.name,
                    "driver": profile.driver,
                    "protocol": profile.protocol,
                    "launchCommand": list(profile.command),
                    "model": profile.model,
                    "reasoningEffort": profile.reasoning_effort,
                    "readOnly": profile.read_only,
                    "fallbacks": list(profile.fallbacks),
                    "active": profile.name in active_profiles,
                }
                for profile in self._profiles.values()
            ],
        }

    def _stage(self, name: str) -> WorkflowStage:
        try:
            return self._stages[name]
        except KeyError as exc:
            raise ValueError(f"unknown workflow stage: {name}") from exc

    def resolve_stage(
        self, name: str, agent_override: str | None = None
    ) -> WorkflowStage:
        """返回 stage 定义；agent_override 用指定 agent 替换该阶段的执行 profile。"""
        stage = self._stage(name)
        if not agent_override or agent_override == stage.profile.name:
            return stage
        profile = self._profiles.get(agent_override)
        if profile is None:
            raise ValueError(f"unknown agent override: {agent_override}")
        return WorkflowStage(
            name=stage.name,
            label=stage.label,
            profile=profile,
            prompt_path=stage.prompt_path,
        )

    def _environment(
        self, profile: AgentProfile, session_home: Path | None = None
    ) -> dict[str, str]:
        environment = {**os.environ, **profile.environment, "PYTHONUNBUFFERED": "1"}
        if profile.protocol != "codex-app-server":
            return environment
        home_value: Any = session_home or profile.options.get("isolated_home")
        if not home_value:
            return environment
        home = Path(str(home_value)).expanduser()
        if not home.is_absolute():
            home = self._config_dir / home
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        auth_value = profile.options.get("auth_file")
        auth_source = (
            Path(str(auth_value)).expanduser().resolve()
            if auth_value
            else Path(environment.get("CODEX_HOME") or Path.home() / ".codex")
            / "auth.json"
        )
        auth_target = home / "auth.json"
        if (
            auth_source.is_file()
            and not auth_target.exists()
            and not auth_target.is_symlink()
        ):
            auth_target.symlink_to(auth_source)
        environment["CODEX_HOME"] = str(home.resolve())
        return environment


def _resolve_path(base: Path, value: Any) -> Path:
    if not value:
        raise ValueError("workflow path must not be empty")
    path = Path(str(value)).expanduser()
    return path.resolve() if path.is_absolute() else (base / path).resolve()


def _protocol_skill_args(
    protocol: str,
    skills: Sequence[tuple[str, Path]],
    session_dir: Path,
) -> list[str]:
    """skill 的 argv 片段：pi 直接 --skill 池内目录；claude 用
    --plugin-dir 指 session 落盘目录；其余协议靠原生发现位，无 flag。"""
    if not skills:
        return []
    if protocol == "pi-rpc":
        # -ns 关掉全局/项目发现，显式 --skill 仍加载（pi cli 契约）：
        # 节点只暴露声明的 skills，与 claude/codex 的 session 装配对等。
        args: list[str] = ["-ns"]
        for _name, path in skills:
            skill_dir = path.parent if path.name == "SKILL.md" else path
            args.extend(["--skill", str(skill_dir)])
        return args
    if protocol == "claude-stream-json":
        return ["--plugin-dir", str(session_dir / "claude-plugin")]
    return []


def _prepare_protocol_skills(
    protocol: str,
    skills: Sequence[tuple[str, Path]],
    session_dir: Path,
) -> None:
    """把 skill 目录落进各协议的原生发现位。

    - dsh：session/skills/<name> symlink，由动态 patch 的 customSkillDirs
      指向（dsh-skill-filesystem 运行时发现，watch 增删实时生效）。
    - codex：codex-home/skills/<name> symlink，CODEX_HOME 原生发现。
    - claude：claude-plugin/ 插件目录（skills/ 子目录 + plugin 清单），
      argv 侧 --plugin-dir 挂载；claude 无 per-invocation skill flag，
      plugin-dir 是唯一按次通道。
    - pi：不需要落盘，argv 直接 --skill <dir> 指向池内原目录。
    """
    if not skills:
        return
    if protocol == "dsh-headless-jsonl":
        root = session_dir / "skills"
    elif protocol == "codex-app-server":
        root = session_dir / "codex-home" / "skills"
    elif protocol == "claude-stream-json":
        root = session_dir / "claude-plugin" / "skills"
        manifest = session_dir / "claude-plugin" / ".claude-plugin" / "plugin.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        if not manifest.exists():
            manifest.write_text(
                json.dumps({"name": "dws-session-skills", "version": "0.1.0"}),
                encoding="utf-8",
            )
    else:
        return
    root.mkdir(parents=True, exist_ok=True)
    for name, path in skills:
        target = root / name
        source = path.parent if path.name == "SKILL.md" else path
        if target.exists() or target.is_symlink():
            continue
        target.symlink_to(source.resolve())


def _command(
    profile: AgentProfile,
    mcp_config_path: Path | None,
    model_override: str | None = None,
    *,
    mount_escalate: bool = False,
) -> list[str]:
    return _adapter_for(profile.protocol).command(
        profile, mcp_config_path, model_override, mount_escalate=mount_escalate
    )



_BUILTIN_STEPS = ("gate", "deliver")


def _resolve_pipeline(
    preset: Mapping[str, Any], stages: Mapping[str, WorkflowStage]
) -> tuple[str, ...]:
    """preset 的 pipeline 字段 → 有序 step 列表（无环无并行，线性序贯）。

    项 = 内置 step（gate 规则闸 / deliver 收口投递）或 preset 里的 stage
    槽位名（按 kind 路由到 triage/executive 处理器）。缺省推导 = 现行为
    gate → triage 槽位（如有）→ executive → deliver，老 preset 零改动。
    deliver 不允许缺席（删了它结果无处去），声明里漏掉时自动补尾。
    """
    declared = preset.get("pipeline")
    # 空列表视同未声明（面板快照的 round-trip 会带空数组），走缺省推导。
    if declared is None or (
        isinstance(declared, (list, tuple)) and len(declared) == 0
    ):
        steps = ["gate"]
        for name in stages:
            if stage_registry.stage_spec(name).kind == "triage":
                steps.append(name)
        for name in stages:
            if stage_registry.stage_spec(name).executive:
                steps.append(name)
        steps.append("deliver")
        return tuple(steps)
    if not isinstance(declared, (list, tuple)) or not declared:
        raise ValueError("workflow pipeline must be a non-empty list of step names")
    if not all(isinstance(item, str) and item for item in declared):
        raise ValueError("workflow pipeline steps must be non-empty strings")
    steps = [str(item) for item in declared]
    if len(set(steps)) != len(steps):
        raise ValueError(f"workflow pipeline has duplicate steps: {steps}")
    for item in steps:
        if item not in _BUILTIN_STEPS and item not in stages:
            raise ValueError(f"workflow pipeline references unknown step: {item!r}")
    if "deliver" not in steps:
        steps.append("deliver")
    if not any(
        item not in _BUILTIN_STEPS and stage_registry.stage_spec(item).executive
        for item in steps
    ):
        raise ValueError("workflow pipeline requires an executive stage")
    return tuple(steps)


def _prepare_mcp_config(
    profile: AgentProfile, protocol: str, session_dir: Path, stage_name: str
) -> Path | None:
    """session 级 mcp.json 生成：具体形状由 adapter 决定。

    pi 走 --mcp-config（exclusive 不串全局 server）；codex/claude 只有
    front 需要 escalate；其余协议（custom/dsh）不消费 MCP 配置文件。
    """
    return _adapter_for(protocol).mcp_config(profile, session_dir, stage_name)


def _thread_options(profile: AgentProfile, workspace_root: Path) -> dict[str, Any]:
    return _adapter_for(profile.protocol).thread_options(profile, workspace_root)


async def _terminate_process_tree(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    if os.name == "nt":
        killer = await asyncio.create_subprocess_exec(
            "taskkill",
            "/PID",
            str(process.pid),
            "/T",
            "/F",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await killer.wait()
    else:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
            return
        except TimeoutError:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
    await process.wait()


def _without_none(value: Mapping[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item is not None}


def _assistant_text(event: Mapping[str, Any]) -> str:
    message = event.get("message")
    if not isinstance(message, Mapping) or message.get("role") not in {None, "assistant"}:
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        str(item.get("text") or "")
        for item in content
        if isinstance(item, Mapping) and item.get("type") == "text"
    ).strip()


def _assistant_thinking(event: Mapping[str, Any]) -> str:
    message = event.get("message")
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


def _assistant_tool_names(event: Mapping[str, Any]) -> list[str]:
    message = event.get("message")
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


def _is_escalate_tool(value: Any) -> bool:
    name = str(value or "").lower()
    return name == "escalate" or name.endswith(
        ("__escalate", "/escalate", ":escalate")
    )


def _tool_args_summary(args: Any) -> str:
    """把工具参数压成一行可读摘要，给面板的 toolcall 条目用。

    完整 JSON 在面板上没人看得懂也塞不下：优先取最能表达意图的字段，
    其余按 key=value 拼接并限长。凭据类字段一律打码——工具参数里出现
    令牌时不能因为"面板是本地的"就原样落进浏览器的 DOM。
    """
    if not isinstance(args, Mapping):
        return ""
    for key in ("command", "path", "file_path", "query", "url", "pattern", "goal", "text"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return _redact_secrets(value.strip())[:_LIVE_EVENT_CHARS]
    parts = []
    for key, value in args.items():
        if _is_secret_key(key):
            parts.append(f"{key}=***")
        elif isinstance(value, (str, int, float, bool)):
            parts.append(f"{key}={_redact_secrets(str(value))}")
        elif isinstance(value, (list, tuple)):
            parts.append(f"{key}[{len(value)}]")
        elif isinstance(value, Mapping):
            parts.append(f"{key}{{{len(value)}}}")
    return " ".join(parts)[:_LIVE_EVENT_CHARS]


_SECRET_KEY_HINTS = ("token", "secret", "password", "passwd", "api_key", "apikey", "credential", "cookie", "authorization")
# 值可能是 "Bearer sk-..." 这种带方案前缀的形式：方案名保留，后面的凭据整段打码，
# 否则只吃到第一个空格，令牌就漏在面板上。
_SECRET_VALUE = r"(\"[^\"]*\"|'[^']*'|(?:(?:Bearer|Basic|Token)\s+)?\S+)"
_SECRET_FLAG = re.compile(
    r"(?i)(--?(?:token|secret|password|passwd|api[-_]?key|authorization|cookie)[=\s]+)" + _SECRET_VALUE
)
_SECRET_PAIR = re.compile(
    r"(?i)\b((?:token|secret|password|passwd|api[-_]?key|authorization|cookie)\s*[:=]\s*)" + _SECRET_VALUE
)
_SECRET_SCHEME = re.compile(r"(?i)^((?:Bearer|Basic|Token)\s+).*$")


def _is_secret_key(key: Any) -> bool:
    return any(hint in str(key).lower() for hint in _SECRET_KEY_HINTS)


def _redact_secrets(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        value = match.group(2)
        scheme = _SECRET_SCHEME.match(value.strip("\"'"))
        # 保留 "Bearer " 之类的方案前缀，只吞掉凭据本体。
        return f"{match.group(1)}{scheme.group(1) if scheme else ''}***"

    return _SECRET_PAIR.sub(replace, _SECRET_FLAG.sub(replace, text))


def _is_no_reply_tool(value: Any) -> bool:
    name = str(value or "").lower()
    return name == "no_reply" or name.endswith(
        ("__no_reply", "/no_reply", ":no_reply")
    )


def _is_report_tool(value: Any) -> bool:
    name = str(value or "").lower()
    return name == "report" or name.endswith(("__report", "/report", ":report"))
