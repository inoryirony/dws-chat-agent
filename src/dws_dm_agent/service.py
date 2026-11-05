#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import uuid
import webbrowser
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone as fixed_timezone, tzinfo
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

if os.name == "nt":
    import msvcrt
else:
    import fcntl

from .runners.runtime import AgentRuntime, AgentSession, WorktreeError
from .prompts import (
    SHARED_PROMPT_NAME,
    assemble_stage_prompt,
    render_prompt,
)
from .core import (
    MAX_IMAGES,
    MAX_IMAGE_BYTES,
    AuditStore,
    Contact,
    GateResult,
    HistoryMessage,
    IncomingEvent,
    SecurityGate,
    canonical_message_text,
    human_owns_conversation,
    parse_dws_event,
    parse_local_datetime,
    sanitize_text,
    sha256_text,
)
from .config import (
    DEFAULT_CONFIG,
    DEFAULT_ENV,
    WEB_DIR,
    Settings,
    _load_timezone,
    _read_env_file,
    _resolve_env,
    load_settings,
)
from .dashboard import AgentConfigStore, DashboardServer
from .dws import DwsClient, _find_dws_error_code


LOG = logging.getLogger("dws-chat-agent")
_IMAGE_MEDIA_ID = re.compile(r"\[图片消息\]\(mediaId=([^)\s]+)\)")
# 图片预算统一定义在 core（MAX_IMAGES/MAX_IMAGE_BYTES）；此处仅保留历史别名，
# 事件流读取上限等下游限制必须与 core 常量联动，不允许各自硬编码。
_MAX_IMAGES = MAX_IMAGES
_MAX_IMAGE_BYTES = MAX_IMAGE_BYTES
_STREAM_CARD_BIND_TIMEOUT_SECONDS = 6.0
# 活跃事项快照注入 prompt 的上限；超出部分用 task_lookup 查询，不悄悄丢失。
_TASK_SNAPSHOT_LIMIT = 8
# 事项无实质更新后的归档天数（task_memory.inactive_days 可覆盖）。
_TASK_INACTIVE_DAYS_DEFAULT = 2
# 唤醒循环扫描间隔（wake_interval_seconds 可覆盖）与给人消息的可打扰窗口。
_WAKE_INTERVAL_SECONDS_DEFAULT = 5.0
_WAKE_WINDOW_OPEN = time(9, 0)
_WAKE_WINDOW_CLOSE = time(19, 0)
# 相1 卡片最多渲染的里程碑条数（§18：3~7 行短句）。
_PROGRESS_CARD_LIMIT = 7
# turn-stopping（§18 P3）：模型结束一轮却既无正文也未沉默时，补打一条
# 固定提示再等一轮，仍无结果才走 fallback 兜底；每次运行至多补打一次。
_TURN_NUDGE_PROMPT = (
    "本次运行还没有收尾：请调用 report(final/blocked) 给出正文，"
    "或 no_reply() 表示沉默。"
)
# 合成输入（wake/面板指令）的 message_id 前缀与 prompt 内的系统唤醒块。
_SYNTHETIC_PREFIXES = ("wake-", "panel-")
_SYSTEM_WAKE_DISCLAIMER = "这不是联系人的新消息，不含新授权。"
# 操作者指令出自本人（§16：授权变更只认 human 手打），本身即最高授权。
_OPERATOR_INSTRUCTION_LABEL = "操作者指令"
_OPERATOR_INSTRUCTION_DISCLAIMER = (
    "这是操作者本人下达的指令，按最高授权执行。"
)
_TIME_WAITING_PATTERN = re.compile(r"^@(.+)$")
# claimed wake 超过该时长未收口视为派发进程已死：启动扫尾回置 pending
# 重放（crash 后的合成输入最多迟 30 分钟，绝不丢）。
_STALE_CLAIM_AGE = timedelta(minutes=30)


def _is_synthetic_message_id(message_id: str) -> bool:
    return message_id.startswith(_SYNTHETIC_PREFIXES)


def _system_wake_block(entries: Sequence[tuple[str, str]]) -> str:
    """把合成输入渲染成 prompt 内独立块；标签区分来源与授权语义。

    系统唤醒只是提醒，不带新授权；操作者指令出自本人，本身就是
    最高授权，两种来源各盖各的章，不靠语气猜。
    """
    lines = ["<system_wake>"]
    for label, text in entries:
        lines.append(f"{label}：{text}")
        lines.append(
            _OPERATOR_INSTRUCTION_DISCLAIMER
            if label == _OPERATOR_INSTRUCTION_LABEL
            else _SYSTEM_WAKE_DISCLAIMER
        )
    lines.append("</system_wake>")
    return "\n".join(lines)


def _parse_waiting_moment(value: str, timezone: tzinfo) -> datetime | None:
    """解析 waiting_on 的 "@时刻" 写法；解析失败返回 None（不唤醒）。"""
    try:
        moment = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone)


def _roll_into_wake_window(moment: datetime) -> datetime:
    """把窗外时刻顺延到下一个 09:00（窗口 09:00-19:00，含头不含尾）。"""
    if moment.time() < _WAKE_WINDOW_OPEN:
        return moment.replace(hour=9, minute=0, second=0, microsecond=0)
    if moment.time() >= _WAKE_WINDOW_CLOSE:
        return (moment + timedelta(days=1)).replace(
            hour=9, minute=0, second=0, microsecond=0
        )
    return moment


def _event_consume_command(
    settings: Settings, contact: Contact, *, dry_run: bool = False
) -> list[str]:
    command = [
        str(settings.raw.get("dws", {}).get("binary", "dws")),
        "event",
        "consume",
        "user_im_message_receive_o2o",
    ]
    if contact.user_id:
        command.extend(["--user", contact.user_id])
    command.extend(["--format", "ndjson", "--ephemeral"])
    profile = settings.raw.get("dws", {}).get("profile")
    if profile:
        command.extend(["--profile", str(profile)])
    if dry_run:
        command.append("--dry-run")
    return command


def _image_resources(
    batch: Sequence[IncomingEvent],
) -> list[tuple[IncomingEvent, str]]:
    resources: list[tuple[IncomingEvent, str]] = []
    seen: set[tuple[str, str]] = set()
    for event in batch:
        for media_id in _IMAGE_MEDIA_ID.findall(event.content):
            key = (event.message_id, media_id)
            if key not in seen:
                seen.add(key)
                resources.append((event, media_id))
    return resources


def _image_suffix(path: Path) -> str:
    with path.open("rb") as stream:
        header = stream.read(12)
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if header.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return ".webp"
    raise RuntimeError("media_download_failed: unsupported image format")


def _apply_image_context(
    prompt: str, protocol: str, local_images: Sequence[Path]
) -> tuple[str, tuple[Path, ...]]:
    """按协议把下载好的本地图片接入 prompt，返回 (prompt, local_images)。

    - pi-rpc：图片由 runtime 编码为 ImageContent 随首条 prompt 原生发送，
      prompt 只说明附件已附带、可直接看图；看不到图片时如实说明，请对方
      用文字描述，不再提供单独的看图工具兜底；local_images 原样传给
      open_session。
    - codex-app-server：runtime 以 localImage 路径输入，prompt 不加说明。
    - 其余协议（claude/custom）：无法原生输入图片，把路径嵌入 prompt，
      由 agent 用 read 工具读取，返回空 local_images。
    """
    if not local_images or protocol == "codex-app-server":
        return prompt, tuple(local_images)
    image_paths = "\n".join(f"- {img.resolve()}" for img in local_images)
    if protocol == "pi-rpc":
        return (
            prompt
            + "\n\n## 图片附件\n\n对方发送的图片已作为图片附件随本条消息直接发给你，"
            "请直接查看图片内容并结合上下文理解对方意图，不要为看图调用工具。"
            "若你确实看不到图片内容，如实说明并请对方用文字描述现象。\n",
            tuple(local_images),
        )
    return (
        prompt
        + "\n\n## 图片附件\n\n以下图片文件已下载到本地：\n"
        + image_paths
        + "\n请先用 read 工具逐个读取，确认你是否能直接看到图片内容；"
        "如果 read 得到的不是有效图片内容，如实说明并请对方用文字描述现象。\n",
        (),
    )


async def _reopen_session_for_closing_nudge(session: AgentSession) -> bool:
    """空终稿时重开会话并补打一轮收尾提示（turn-stopping）。

    AgentSession 以 done 拦截 steer，而 pi 会话进程在一个 turn 结束后
    仍存活、可继续对话：这里清掉完成态与错误，让 steer 把固定提示注入
    同一会话。进程已退出等重开失败时恢复原 done/错误，外层按原 error
    路径收口；补打后的收尾判定复用外层现有的 silent/error/正文分支。
    """
    error = session.error
    session.error = ""
    session._complete.clear()
    try:
        steered = await session.steer(_TURN_NUDGE_PROMPT)
    except Exception:  # noqa: BLE001 - 补打是尽力而为，不能炸掉收尾路径
        steered = False
    if steered:
        return True
    if not session.done:
        session.error = error
        session._complete.set()
    return False


@dataclass
class AgentResult:
    session_id: str
    reply: str | None
    exit_code: int | None
    started_at: datetime
    finished_at: datetime
    escalated: bool = False
    manual_takeover: bool = False
    supplements: list[IncomingEvent] | None = None
    consumed_supplements: list[IncomingEvent] | None = None
    error: str = ""
    # 模型显式调用 no_reply 后的成功终态：不发消息、不进 fallback。
    silent: bool = False
    # 模型经 report(blocked, text) 结束：正文照常交付，运行结果记 blocked。
    blocked: bool = False
    # 不可重试的失败（如 worktree 隔离建立失败）：禁止 fallback 链在
    # 未隔离的环境里重试同一段写操作。
    fatal: bool = False

    @property
    def outcome(self) -> str:
        """运行的明确结果：interrupted/blocked/escalate/silent/reply/error。"""
        if self.manual_takeover:
            return "interrupted"
        if self.blocked:
            return "blocked"
        if self.escalated:
            return "escalate"
        if self.silent:
            return "silent"
        if self.reply is not None:
            return "reply"
        return "error"


# 黑板运行的收口 outcome：AgentResult.outcome -> close_run outcome。
_BOARD_CLOSE_OUTCOME = {
    "interrupted": "aborted",
    "blocked": "blocked",
    "escalate": "escalate",
    "silent": "silent",
    "reply": "final",
    "error": "failed",
}


def configure_logging(state_dir: Path, verbose: bool = False) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    file_handler = logging.FileHandler(state_dir / "agent.log", encoding="utf-8")
    handlers.append(file_handler)
    for handler in handlers:
        handler.setFormatter(formatter)
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        handlers=handlers,
        force=True,
    )


class AgentService:
    def __init__(
        self,
        settings: Settings,
        mode_override: str | None = None,
        *,
        open_dashboard: bool = False,
    ) -> None:
        self.settings = settings
        self.mode = mode_override or settings.mode
        if self.mode not in {"shadow", "live"}:
            raise ValueError("mode must be shadow or live")
        self.agent_runtime = AgentRuntime.from_config(
            settings.raw, settings.config_path, settings.workspace_root
        )
        self.open_dashboard = open_dashboard
        self.store = AuditStore(settings.state_dir / "audit.sqlite3")
        security = settings.raw.get("security", {})
        self.gate = SecurityGate(
            max_chars=int(security.get("max_message_chars", 12000))
        )
        self.dws = DwsClient(settings)
        self.queues: dict[str, asyncio.Queue[IncomingEvent]] = {}
        self.workers: dict[str, asyncio.Task[None]] = {}
        self.listeners: list[asyncio.Task[None]] = []
        self.runtime_heartbeat: asyncio.Task[None] | None = None
        self.history_reconciler: asyncio.Task[None] | None = None
        self.outbox_dispatcher: asyncio.Task[None] | None = None
        self.wake_dispatcher: asyncio.Task[None] | None = None
        self.outbox_wakeup = asyncio.Event()
        self.listener_processes: set[asyncio.subprocess.Process] = set()
        self.stop_event = asyncio.Event()
        self.capacity = int(settings.raw.get("max_parallel_conversations", 2))
        self.global_slots = asyncio.Semaphore(self.capacity)
        self.runtime_path = settings.state_dir / "runtime.json"
        self.runtime_states: dict[str, dict[str, Any]] = {}
        self.streaming_card_send_uuids: dict[str, str] = {}
        # 在跑会话注册表（steer_run 用）与相1卡片的里程碑水位。
        self.live_sessions: dict[str, AgentSession] = {}
        self.progress_card_seqs: dict[str, int] = {}
        # Ponytail ceiling: this lock only covers idle-check/config-swap/enqueue;
        # move config application onto the event loop if those operations grow.
        self.configuration_lock = threading.RLock()
        self.dashboard = DashboardServer(
            settings.state_dir / "audit.sqlite3",
            self.runtime_path,
            WEB_DIR / "dashboard.html",
            {item.identity: item.display_name for item in settings.contacts},
            settings.timezone,
            config_store=AgentConfigStore(
                settings.config_path,
                settings.workspace_root,
                on_apply=self._apply_agent_configuration,
                is_idle=self._configuration_is_idle,
                mutation_lock=self.configuration_lock,
                contacts=[
                    {
                        "alias": item.alias,
                        "display_name": item.display_name,
                        "user_id": item.user_id,
                        "open_dingtalk_id": item.open_dingtalk_id,
                    }
                    for item in settings.contacts
                ],
            ),
            contact_search=self._search_dingtalk_contacts,
            service=self,
        )
    def close(self) -> None:
        self.dashboard.stop()
        self.runtime_path.unlink(missing_ok=True)
        self.store.close()

    async def run(self) -> None:
        self.settings.state_dir.mkdir(parents=True, exist_ok=True)
        self.settings.worktree_root.mkdir(parents=True, exist_ok=True)
        LOG.info("starting mode=%s contacts=%d", self.mode, len(self.settings.contacts))
        now = datetime.now(self.settings.timezone)
        aborted_runs = self.store.abort_stale_runs(now)
        if aborted_runs:
            LOG.warning("startup aborted stale board runs count=%d", len(aborted_runs))
            self._reclaim_orphan_worktrees(aborted_runs)
        replayed = self.store.recover_stale_wakes(
            now, max_claim_age=_STALE_CLAIM_AGE
        )
        if replayed:
            LOG.warning("startup replayed claimed wakes count=%d", replayed)
        days = self._task_memory_inactive_days()
        if days > 0:
            archived = self.store.archive_inactive_tasks(
                datetime.now(self.settings.timezone), days
            )
            if archived:
                LOG.info("startup archived inactive tasks count=%d", archived)
        self._write_runtime()
        try:
            self.dashboard.start()
            self._write_panel_access_files()
            LOG.info(
                "dashboard ready url=%s panel token: %s…",
                self.dashboard.url,
                self.dashboard.operator_token[:4],
            )
            if self.open_dashboard:
                await asyncio.to_thread(
                    webbrowser.open, self.dashboard.tokened_url
                )
        except OSError as exc:
            LOG.error("dashboard failed error=%s", exc)
        await self._recover_pending_events()
        await self._reconcile_missing_events()
        self.listeners = [
            asyncio.create_task(self._listen_contact(contact), name=f"listen:{contact.alias}")
            for contact in self.settings.contacts
        ]
        self.history_reconciler = asyncio.create_task(
            self._reconcile_history_loop(), name="history-reconciler"
        )
        self.outbox_dispatcher = asyncio.create_task(
            self._dispatch_outbox(), name="outbox-dispatcher"
        )
        self.wake_dispatcher = asyncio.create_task(
            self._consume_wakes(), name="wake-dispatcher"
        )
        self.runtime_heartbeat = asyncio.create_task(
            self._keep_runtime_alive(), name="runtime-heartbeat"
        )
        await self.stop_event.wait()
        LOG.info("stopping listeners gracefully")
        for task in self.listeners:
            task.cancel()
        for task in self.workers.values():
            task.cancel()
        self.history_reconciler.cancel()
        self.outbox_dispatcher.cancel()
        self.wake_dispatcher.cancel()
        self.runtime_heartbeat.cancel()
        await asyncio.gather(
            *self.listeners,
            *self.workers.values(),
            self.history_reconciler,
            self.outbox_dispatcher,
            self.wake_dispatcher,
            self.runtime_heartbeat,
            return_exceptions=True,
        )

    def request_stop(self) -> None:
        self.stop_event.set()

    def _write_panel_access_files(self) -> None:
        """面板凭据落盘：token 与带 token 的完整 URL，均 0600。

        日志只打印 token 前 4 位；完整值仅进这两个文件（本机操作者可读）。
        token 从不写入环境变量，也就从不可能进任何子进程。
        """
        token_path = self.settings.state_dir / "operator_token"
        url_path = self.settings.state_dir / "panel.url"
        for path, content in (
            (token_path, f"{self.dashboard.operator_token}\n"),
            (url_path, f"{self.dashboard.tokened_url}\n"),
        ):
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(content)
            os.chmod(path, 0o600)

    def _reclaim_orphan_worktrees(self, aborted_runs: Sequence[Mapping[str, Any]]) -> None:
        """回收崩溃运行的 worktree：路径在 worktree_root 下且 git 仍登记。

        判据缺一即跳过（只记日志）：路径越界可能是配置迁移后的旧根，
        git 记录不存在说明 worktree 已被手动清理，强删目录会丢未合并
        的现场之外的东西。
        """
        root = self.settings.worktree_root.resolve()
        reclaimed = 0
        for run in aborted_runs:
            raw_path = str(run.get("worktree_path") or "")
            if not raw_path:
                continue
            path = Path(raw_path).resolve()
            try:
                path.relative_to(root)
            except ValueError:
                LOG.warning(
                    "skip worktree reclaim outside root path=%s", raw_path
                )
                continue
            if path == root or not path.exists():
                continue
            source_repo = self._worktree_main_repo(path)
            if source_repo is None:
                LOG.warning(
                    "skip worktree reclaim, not a registered worktree path=%s",
                    raw_path,
                )
                continue
            completed = subprocess.run(
                [
                    "git", "-C", str(source_repo),
                    "worktree", "remove", "--force", str(path),
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if completed.returncode == 0:
                reclaimed += 1
            else:
                LOG.warning(
                    "worktree reclaim failed path=%s detail=%s",
                    raw_path,
                    completed.stderr.strip()[:200],
                )
        if reclaimed:
            LOG.info("startup reclaimed orphan worktrees count=%d", reclaimed)

    @staticmethod
    def _worktree_main_repo(worktree_path: Path) -> Path | None:
        """从 worktree 的 .git 文件反查主仓路径；不是 worktree 返回 None。"""
        git_marker = worktree_path / ".git"
        try:
            content = git_marker.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if not content.startswith("gitdir:"):
            return None
        gitdir = Path(content[len("gitdir:"):].strip())
        if not gitdir.is_absolute():
            gitdir = (worktree_path / gitdir).resolve()
        # gitdir 指向 <main>/.git/worktrees/<name>；主仓是其上两级。
        main_git = gitdir.parent.parent
        return main_git if (main_git / "HEAD").exists() else None

    async def _recover_pending_events(self) -> None:
        async def recover(contact: Contact) -> None:
            pending = self.store.pending_event_ids(contact.identity)
            if not pending:
                return
            try:
                history = await self.dws.history(contact)
            except Exception as exc:
                LOG.warning("queued event recovery failed contact=%s error=%s", contact.alias, exc)
                return
            for message in history:
                if (
                    message.message_id not in pending
                    or message.sender_open_dingtalk_id != contact.open_dingtalk_id
                ):
                    continue
                self._enqueue(
                    IncomingEvent(
                        event_id=f"recovery:{message.message_id}",
                        message_id=message.message_id,
                        conversation_id=message.conversation_id,
                        contact=contact,
                        content=message.content,
                        created_at=message.created_at,
                    )
                )
                pending.remove(message.message_id)
            if pending:
                LOG.info(
                    "queued events remain pending after limited history recovery "
                    "contact=%s count=%d",
                    contact.alias,
                    len(pending),
                )

        await asyncio.gather(*(recover(contact) for contact in self.settings.contacts))

    async def _reconcile_missing_events(
        self, contacts: Sequence[Contact] | None = None
    ) -> None:
        lookback = float(
            self.settings.raw.get("event_reconcile_lookback_seconds", 7200)
        )
        not_before = datetime.now(self.settings.timezone) - timedelta(seconds=lookback)

        async def reconcile(contact: Contact) -> None:
            try:
                history = await self.dws.history(contact)
            except Exception as exc:
                LOG.warning(
                    "event history reconciliation failed contact=%s error=%s",
                    contact.alias,
                    exc,
                )
                return
            latest_self = max(
                (
                    message.created_at
                    for message in history
                    if message.sender_open_dingtalk_id == self.settings.self_open_id
                ),
                default=not_before,
            )
            recover_after = max(not_before, latest_self)
            recovered = 0
            for message in history:
                if (
                    message.sender_open_dingtalk_id != contact.open_dingtalk_id
                    or message.created_at <= recover_after
                ):
                    continue
                event = IncomingEvent(
                    event_id=f"reconcile:{message.message_id}",
                    message_id=message.message_id,
                    conversation_id=message.conversation_id,
                    contact=contact,
                    content=message.content,
                    created_at=message.created_at,
                )
                if self.store.claim_event(event):
                    self._enqueue(event)
                    recovered += 1
            if recovered:
                LOG.warning(
                    "recovered events missed by listener contact=%s count=%d",
                    contact.alias,
                    recovered,
                )

        targets = contacts if contacts is not None else self.settings.contacts
        await asyncio.gather(*(reconcile(contact) for contact in targets))

    async def _reconcile_history_loop(self) -> None:
        interval = float(
            self.settings.raw.get("event_reconcile_interval_seconds", 60)
        )
        while not self.stop_event.is_set():
            await asyncio.sleep(interval)
            await self._reconcile_missing_events()

    async def _keep_runtime_alive(self) -> None:
        while True:
            await asyncio.sleep(5)
            self._write_runtime()

    def _configuration_is_idle(self) -> bool:
        with self.configuration_lock:
            return not self.runtime_states and all(
                queue.empty() for queue in self.queues.values()
            )

    def _search_dingtalk_contacts(self, query: str) -> list[dict[str, Any]]:
        command = [
            str(self.settings.raw.get("dws", {}).get("binary", "dws")),
            "contact",
            "user",
            "search",
            "--query",
            query,
            "--format",
            "json",
            "--timeout",
            "15",
        ]
        profile = self.settings.raw.get("dws", {}).get("profile")
        if profile:
            command.extend(["--profile", str(profile)])
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("搜索超时") from exc
        if completed.returncode:
            code = _find_dws_error_code(completed.stderr) or f"exit {completed.returncode}"
            raise RuntimeError(code)
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("DWS 返回了无效 JSON") from exc
        raw_results = payload.get("result", []) if isinstance(payload, Mapping) else []
        if not isinstance(raw_results, list):
            raise RuntimeError("DWS 返回了无效搜索结果")
        results = []
        for item in raw_results[:30]:
            if not isinstance(item, Mapping):
                continue
            results.append(
                {
                    "name": sanitize_text(item.get("name"), 120),
                    "nick": sanitize_text(item.get("nick"), 120),
                    "flowerName": sanitize_text(item.get("flowerName"), 120),
                    "title": sanitize_text(item.get("title"), 120),
                    "userId": str(item.get("userId") or "")[:512],
                    "openDingTalkId": str(item.get("openDingTalkId") or "")[:512],
                }
            )
        return results

    def _apply_agent_configuration(self, raw: Mapping[str, Any]) -> None:
        env = _read_env_file(self.settings.env_path)
        env.update(os.environ)
        contacts = raw.get("contacts", [])
        if isinstance(contacts, list) and any(
            str(item.get("open_dingtalk_id") or "") == self.settings.self_open_id
            for item in contacts
            if isinstance(item, Mapping)
        ):
            raise ValueError("self identity cannot be added to the contact whitelist")
        candidate = dict(self.settings.raw)
        candidate["agents"] = _resolve_env(raw.get("agents", {}), env)
        candidate["workflows"] = _resolve_env(raw.get("workflows", {}), env)
        candidate["dws"] = _resolve_env(raw.get("dws", {}), env)
        runtime = AgentRuntime.from_config(
            candidate, self.settings.config_path, self.settings.workspace_root
        )
        missing = [
            f"{name}: {binary}"
            for name, binary in runtime.required_binaries().items()
            if shutil.which(binary) is None
        ]
        if missing:
            raise ValueError(f"active agent launcher not found: {', '.join(missing)}")
        previous_agents = self.settings.raw.get("agents")
        previous_workflows = self.settings.raw.get("workflows")
        previous_dws = self.settings.raw.get("dws")
        previous_ai_tag = self.dws.ai_tag
        previous_runtime = self.agent_runtime
        try:
            self.settings.raw["agents"] = candidate["agents"]
            self.settings.raw["workflows"] = candidate["workflows"]
            self.settings.raw["dws"] = candidate["dws"]
            self.dws.ai_tag = bool(candidate["dws"].get("ai_tag", False))
            self.agent_runtime = runtime
            self._write_runtime()
        except Exception:
            self.settings.raw["agents"] = previous_agents
            self.settings.raw["workflows"] = previous_workflows
            self.settings.raw["dws"] = previous_dws
            self.dws.ai_tag = previous_ai_tag
            self.agent_runtime = previous_runtime
            raise

    def _write_runtime(self) -> None:
        payload = {
            "pid": os.getpid(),
            "mode": self.mode,
            "contacts": len(self.settings.contacts),
            "capacity": self.capacity,
            "workflow": self.agent_runtime.describe(),
            "heartbeatAt": datetime.now(self.settings.timezone).isoformat(),
            "active": sorted(
                self.runtime_states.values(), key=lambda item: item["startedAt"]
            ),
        }
        temporary = self.runtime_path.with_suffix(".tmp")
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            os.replace(temporary, self.runtime_path)
        except OSError as exc:
            LOG.warning("runtime snapshot failed error=%s", exc)

    def _set_runtime(
        self,
        batch: Sequence[IncomingEvent],
        phase: str,
        *,
        attempt: int | None = None,
        session_id: str | None = None,
    ) -> None:
        conversation_id = batch[0].conversation_id
        now = datetime.now(self.settings.timezone).isoformat()
        state = self.runtime_states.setdefault(
            conversation_id,
            {
                "conversationId": conversation_id,
                "contactName": batch[0].contact.display_name,
                "contactAlias": batch[0].contact.alias,
                "startedAt": min(item.created_at for item in batch).isoformat(),
                "attempt": 1,
                "sessionId": "",
            },
        )
        labels = {
            "stabilizing": "正在合并消息",
            "screening": "正在判断是否处理",
            "acknowledged": "已确认，准备执行",
            "routing": "前置 Agent 正在只读处理",
            "worker": "后置 Agent 正在执行",
            "steering": "已收到补充消息，正在调整处理",
            "verifying": "正在核验结果",
            "freshness": "正在检查补充消息",
            "sending": "正在发送结果",
        }
        state.update(
            {
                "phase": phase,
                "phaseLabel": labels.get(phase, phase),
                "updatedAt": now,
                "batchSize": len(batch),
                "requestPreview": self._request_preview(batch),
            }
        )
        if attempt is not None:
            state["attempt"] = attempt
        if session_id is not None:
            state["sessionId"] = session_id
        self._write_runtime()

    @staticmethod
    def _request_preview(batch: Sequence[IncomingEvent]) -> str:
        return sanitize_text("\n".join(item.content for item in batch), 500)

    def _touch_runtime(self, conversation_id: str) -> None:
        if conversation_id in self.runtime_states:
            self.runtime_states[conversation_id]["updatedAt"] = datetime.now(
                self.settings.timezone
            ).isoformat()
            self._write_runtime()

    def _clear_runtime(self, conversation_id: str) -> None:
        self.runtime_states.pop(conversation_id, None)
        self._write_runtime()

    async def _listen_contact(self, contact: Contact) -> None:
        if not contact.user_id:
            await self._poll_open_id_contact(contact)
            return
        delay = 1.0
        while not self.stop_event.is_set():
            command = _event_consume_command(self.settings, contact)
            process: asyncio.subprocess.Process | None = None
            stderr_task: asyncio.Task[None] | None = None
            try:
                process = await asyncio.create_subprocess_exec(
                    *command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                self.listener_processes.add(process)
                stderr_task = asyncio.create_task(self._drain_listener_stderr(process, contact))
                assert process.stdout is not None
                async for raw_line in process.stdout:
                    event = parse_dws_event(
                        raw_line.decode("utf-8", errors="replace"), contact, self.settings.timezone
                    )
                    if event is None:
                        continue
                    if not self.store.claim_event(event) and not self.store.event_is_pending(
                        event.message_id
                    ):
                        continue
                    self._enqueue(event)
                return_code = await process.wait()
                if not self.stop_event.is_set():
                    LOG.warning("listener exited contact=%s code=%s", contact.alias, return_code)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOG.error("listener error contact=%s error=%s", contact.alias, exc)
            finally:
                if stderr_task:
                    stderr_task.cancel()
                    await asyncio.gather(stderr_task, return_exceptions=True)
                if process is not None:
                    self.listener_processes.discard(process)
                    if process.returncode is None:
                        process.terminate()
                    try:
                        await asyncio.wait_for(process.communicate(), timeout=10)
                    except TimeoutError:
                        LOG.error("listener did not stop after SIGTERM contact=%s", contact.alias)
            if not self.stop_event.is_set():
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)

    async def _poll_open_id_contact(self, contact: Contact) -> None:
        # ponytail: open-id-only contacts poll until DWS event consume accepts
        # openDingTalkId; replace this with a native subscription when available.
        interval = float(
            self.settings.raw.get("open_id_poll_interval_seconds", 5)
        )
        LOG.info(
            "listener ready contact=%s transport=history-poll interval=%ss",
            contact.alias,
            interval,
        )
        while not self.stop_event.is_set():
            await asyncio.sleep(interval)
            await self._reconcile_missing_events((contact,))

    async def _drain_listener_stderr(
        self, process: asyncio.subprocess.Process, contact: Contact
    ) -> None:
        assert process.stderr is not None
        async for raw_line in process.stderr:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if "ready" in line.lower():
                LOG.info("listener ready contact=%s", contact.alias)
            elif line:
                LOG.debug(
                    "listener status contact=%s detail_hash=%s detail_length=%d",
                    contact.alias,
                    sha256_text(line)[:12],
                    len(line),
                )

    def _enqueue(self, event: IncomingEvent) -> None:
        with self.configuration_lock:
            queue = self.queues.setdefault(event.conversation_id, asyncio.Queue())
            queue.put_nowait(event)
            if (
                event.conversation_id not in self.workers
                or self.workers[event.conversation_id].done()
            ):
                self.workers[event.conversation_id] = asyncio.create_task(
                    self._conversation_worker(event.conversation_id),
                    name=f"conversation:{sha256_text(event.conversation_id)[:8]}",
                )

    async def _conversation_worker(self, conversation_id: str) -> None:
        queue = self.queues[conversation_id]
        while not self.stop_event.is_set():
            first = await queue.get()
            batch = [first]
            self._set_runtime(batch, "stabilizing")
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=self.settings.quiet_window)
                    batch.append(event)
                    self._set_runtime(batch, "stabilizing")
                except TimeoutError:
                    break
            try:
                async with self.global_slots:
                    pending = [
                        item
                        for item in self._deduplicate(batch)
                        if self.store.event_is_pending(item.message_id)
                    ]
                    if pending:
                        await self._set_processing_reactions(pending, active=True)
                        try:
                            await self._handle_batch(pending)
                        finally:
                            await self._set_processing_reactions(
                                pending, active=False
                            )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOG.exception(
                    "conversation failure conversation=%s error=%s",
                    sha256_text(conversation_id)[:10],
                    exc,
                )
                reason = sanitize_text(str(exc), 500) or type(exc).__name__
                try:
                    self._record_without_agent(
                        batch,
                        status="error",
                        action="handoff",
                        handled="处理过程中发生异常，未完成回复",
                        reason=reason,
                    )
                except Exception:
                    LOG.exception(
                        "failed to record conversation error conversation=%s",
                        sha256_text(conversation_id)[:10],
                    )
                self.store.update_event_status(
                    (item.message_id for item in batch), "error", reason
                )
            finally:
                self._clear_runtime(conversation_id)
                for _ in batch:
                    queue.task_done()

    async def _set_processing_reactions(
        self, batch: Sequence[IncomingEvent], *, active: bool
    ) -> None:
        dws = getattr(self, "dws", None)
        operation = getattr(
            dws, "add_emoji" if active else "remove_emoji", None
        )
        if not callable(operation):
            return
        for item in batch:
            if _is_synthetic_message_id(item.message_id):
                # 合成输入没有对应的钉钉消息，表情回应无从挂载。
                continue
            try:
                await operation(item.conversation_id, item.message_id, "专注")
            except Exception as exc:
                LOG.warning(
                    "processing reaction failed active=%s message=%s error=%s",
                    active,
                    sha256_text(item.message_id)[:12],
                    sanitize_text(str(exc), 300),
                )

    @staticmethod
    def _deduplicate(events: Sequence[IncomingEvent]) -> list[IncomingEvent]:
        by_id: dict[str, IncomingEvent] = {}
        for event in events:
            by_id[event.message_id] = event
        return sorted(by_id.values(), key=lambda item: item.created_at)

    def _register_manual_messages(self, history: Sequence[HistoryMessage]) -> datetime | None:
        for message in history:
            if message.sender_open_dingtalk_id != self.settings.self_open_id:
                continue
            if self.store.is_agent_outgoing(message):
                # A send response may omit message_id. Reconcile it from history
                # and undo any earlier false manual-takeover classification.
                self.store.remove_manual(message.message_id)
            else:
                self.store.record_manual(message)
        if not history:
            return None
        return self.store.latest_manual(history[-1].conversation_id)

    def _human_owns_conversation(
        self, latest_manual: datetime | None, earliest_incoming: datetime
    ) -> bool:
        return human_owns_conversation(latest_manual, earliest_incoming, self.settings.cooldown)

    def _history_events(
        self,
        contact: Contact,
        conversation_id: str,
        history: Sequence[HistoryMessage],
        not_before: datetime,
    ) -> list[IncomingEvent]:
        result: list[IncomingEvent] = []
        for message in history:
            if message.sender_open_dingtalk_id != contact.open_dingtalk_id:
                continue
            if message.created_at < not_before - timedelta(seconds=2):
                continue
            result.append(
                IncomingEvent(
                    event_id=f"history:{message.message_id}",
                    message_id=message.message_id,
                    conversation_id=conversation_id,
                    contact=contact,
                    content=message.content,
                    created_at=message.created_at,
                )
            )
        return result

    def _recent_context(
        self,
        history: Sequence[HistoryMessage],
        batch: Sequence[IncomingEvent],
        *,
        limit: int | None = None,
    ) -> tuple[list[dict[str, str]], bool]:
        current_ids = {item.message_id for item in batch}
        latest_current = max(item.created_at for item in batch)
        contact = batch[0].contact
        conversation_id = batch[0].conversation_id
        messages: list[dict[str, str]] = []
        for message in sorted(history, key=lambda item: item.created_at):
            if message.message_id in current_ids or message.created_at > latest_current:
                continue
            if message.conversation_id != conversation_id:
                continue
            if message.sender_open_dingtalk_id == contact.open_dingtalk_id:
                role = "contact"
            elif message.sender_open_dingtalk_id == self.settings.self_open_id:
                role = "agent" if self.store.is_agent_outgoing(message) else "operator"
            else:
                continue
            execution = ""
            if role == "agent":
                execution = self.store.outgoing_session_id(message.message_id).partition(":")[0]
            messages.append(
                {
                    "role": role,
                    "sender": contact.display_name if role == "contact" else getattr(self.settings, "self_name", ""),
                    "execution": execution,
                    "time": message.created_at.isoformat(),
                    "message_id": message.message_id,
                    "conversation_id": message.conversation_id,
                    "text": message.content,
                }
            )
        context_limit = int(
            limit
            if limit is not None
            else self.settings.raw.get("recent_context_messages", 100)
        )
        # 条数上限封顶 100（§15 锚定扩窗的旧上限）；真正的预算约束是
        # 字符窗：从最旧条目开始丢弃直到总字符不超过 recent_context_chars。
        context_limit = min(max(context_limit, 0), 100)
        recent = messages[-context_limit:] if context_limit > 0 else []
        budget = int(self.settings.raw.get("recent_context_chars", 12000))
        if budget > 0:
            total = sum(len(item["text"]) for item in recent)
            drop = 0
            while drop < len(recent) and total > budget:
                total -= len(recent[drop]["text"])
                drop += 1
            if drop:
                recent = recent[drop:]
        # 锚只看预算裁剪后剩下的窗口：窗口头被丢就丢，不回溯重建。
        follows_plan = bool(
            recent
            and recent[-1]["role"] == "agent"
            and recent[-1]["execution"] == "plan_large_change"
        )
        return recent, follows_plan

    async def _stabilize(
        self, batch: list[IncomingEvent]
    ) -> tuple[list[IncomingEvent], list[HistoryMessage], datetime | None]:
        contact = batch[0].contact
        earliest = min(item.created_at for item in batch)
        known = {item.message_id for item in batch}
        history: list[HistoryMessage] = []
        for _ in range(4):
            try:
                history = await self.dws.history(contact)
            except Exception as exc:
                LOG.warning(
                    "history unavailable; continuing without remote context "
                    "contact=%s error=%s",
                    contact.alias,
                    sanitize_text(str(exc), 300),
                )
                return self._deduplicate(batch), [], self.store.latest_manual(
                    batch[0].conversation_id
                )
            latest_manual = self._register_manual_messages(history)
            if self._human_owns_conversation(latest_manual, earliest):
                return batch, history, latest_manual
            discovered = [
                item
                for item in self._history_events(
                    contact, batch[0].conversation_id, history, earliest
                )
                if item.message_id not in known
            ]
            if not discovered:
                return self._deduplicate(batch), history, latest_manual
            for item in discovered:
                self.store.claim_event(item)
                known.add(item.message_id)
                batch.append(item)
            await asyncio.sleep(self.settings.quiet_window)
        return self._deduplicate(batch), history, self.store.latest_manual(batch[0].conversation_id)

    async def _handle_batch(self, initial_batch: list[IncomingEvent]) -> None:
        batch = initial_batch
        prior_attempt = ""
        max_replans = int(self.settings.raw.get("max_replans", 5))
        for attempt in range(max_replans):
            batch, history, latest_manual = await self._stabilize(batch)
            recent_context, _ = self._recent_context(history, batch)
            self._set_runtime(batch, "screening", attempt=attempt + 1)
            earliest = min(item.created_at for item in batch)
            if self._human_owns_conversation(latest_manual, earliest):
                await self._close_active_streaming_card(
                    batch[0].conversation_id, "已停止自动处理，已转人工。"
                )
                self._record_without_agent(
                    batch,
                    status="human_cooldown",
                    action="no_reply",
                    handled="人工会话仍在接管期，未自动回复",
                    reason="manual_message_within_cooldown",
                )
                self.store.update_event_status(
                    (item.message_id for item in batch), "skipped", "human_cooldown"
                )
                return

            gate = self.gate.inspect([item.content for item in batch])
            if gate.action == "drop":
                await self._close_active_streaming_card(
                    batch[0].conversation_id, "处理已结束。"
                )
                self._record_without_agent(
                    batch,
                    status="no_reply",
                    action="no_reply",
                    handled="无需回复",
                    reason=gate.reason,
                )
                self.store.update_event_status(
                    (item.message_id for item in batch), "processed", gate.reason
                )
                return
            if gate.action == "refuse":
                refusal_session = f"code-{uuid.uuid4().hex[:12]}"
                outcome = await self._send_after_freshness_check(
                    batch,
                    gate.reply,
                    session_id=refusal_session,
                )
                if outcome == "supplement":
                    batch = await self._merge_latest(batch)
                    continue
                self._record_without_agent(
                    batch,
                    status="refused" if outcome in {"sent", "shadow"} else outcome,
                    action="refuse",
                    handled="拒绝高风险请求",
                    reason=gate.reason,
                    session_id=refusal_session,
                )
                if outcome != "delivery_pending":
                    self.store.update_event_status(
                        (item.message_id for item in batch), "processed", gate.reason
                    )
                return

            if self._agent_rate_limited(batch[0].contact):
                await self._close_active_streaming_card(
                    batch[0].conversation_id, "处理已结束。"
                )
                self._record_without_agent(
                    batch,
                    status="no_reply",
                    action="no_reply",
                    handled="触发本地 Agent 频率保护，未自动回复",
                    reason="agent_rate_limit",
                )
                self.store.update_event_status(
                    (item.message_id for item in batch), "skipped", "agent_rate_limit"
                )
                return

            result: AgentResult | None = None
            if (
                self.agent_runtime.has_stage("front")
                and not _image_resources(batch)
            ):
                self._set_runtime(batch, "routing", attempt=attempt + 1)
                front_result = await self._run_agent(
                    batch,
                    prior_attempt,
                    front=True,
                    recent_context=recent_context,
                )
                if front_result.consumed_supplements:
                    batch = self._deduplicate(
                        [*batch, *front_result.consumed_supplements]
                    )
                if front_result.manual_takeover:
                    # 人工接管后不再新建对外卡片通知；接管事实由人工消息本身
                    # 传达，但已存在的处理中卡片必须安全收口，不能悬在会话里。
                    await self._close_active_streaming_card(
                        batch[0].conversation_id, "已停止自动处理，已转人工。"
                    )
                    self._record_agent_result(
                        batch, front_result, status="human_cooldown"
                    )
                    self.store.update_event_status(
                        (item.message_id for item in batch),
                        "skipped",
                        "manual_takeover",
                    )
                    return
                if front_result.supplements:
                    batch = self._deduplicate([*batch, *front_result.supplements])
                    prior_attempt = "前台收到补充消息，已按完整上下文重新处理。"
                    await asyncio.sleep(self.settings.quiet_window)
                    continue
                if front_result.silent:
                    # 前台判定无需回复：成功终态，不进 worker、不发送、不通知。
                    await self._finish_silent(batch, front_result)
                    return
                if front_result.reply is not None and not front_result.escalated:
                    result = front_result
                else:
                    prior_attempt = (
                        "前台已升级，由执行 Agent 处理完整请求。"
                        if front_result.escalated
                        else f"前台失败：{front_result.error}"
                    )

            if result is None:
                self._set_runtime(batch, "worker", attempt=attempt + 1)
                result = await self._run_worker_with_fallback(
                    batch,
                    prior_attempt,
                    recent_context=recent_context,
                )
                if result.consumed_supplements:
                    batch = self._deduplicate(
                        [*batch, *result.consumed_supplements]
                    )

            if result.manual_takeover:
                await self._close_active_streaming_card(
                    batch[0].conversation_id, "已停止自动处理，已转人工。"
                )
                self._record_agent_result(batch, result, status="human_cooldown")
                self.store.update_event_status(
                    (item.message_id for item in batch), "skipped", "manual_takeover"
                )
                return
            if result.supplements:
                self._record_agent_result(batch, result, status="stale_supplement")
                batch = self._deduplicate([*batch, *result.supplements])
                prior_attempt = "对方补充了消息，按新的完整上下文继续。"
                await asyncio.sleep(self.settings.quiet_window)
                continue
            if result.silent:
                # 模型显式选择沉默：成功终态，不触发兜底回复、不写发送队列。
                await self._finish_silent(batch, result)
                return
            if result.reply is None:
                # 失败要通知联系人：卡片在此时点才创建（见 _ensure_streaming_card）。
                stream_card_id = await self._ensure_streaming_card(
                    batch, result.session_id
                )
                await self._safe_update_streaming_card(
                    stream_card_id, "处理失败，请稍后重试或转人工处理。", 5
                )
                self._record_agent_result(batch, result, status="error")
                self.store.update_event_status(
                    (item.message_id for item in batch), "error", result.error[:200]
                )
                return

            self._set_runtime(batch, "freshness", attempt=attempt + 1)
            # 回复路径不在决策点建卡：最终新鲜度检查在投递派发时进行，
            # 卡片只能在检查通过后创建（见 _dispatch_due_delivery），否则
            # 补充消息或人工接管取消投递时会把处理中卡片留在会话里。
            try:
                outcome = await self._send_after_freshness_check(
                    batch,
                    result.reply,
                    session_id=result.session_id,
                )
            except Exception as exc:
                await self._close_active_streaming_card(
                    batch[0].conversation_id, "结果发送失败，请稍后重试。"
                )
                result.error = f"send_failed: {sanitize_text(str(exc), 500)}"
                self._record_agent_result(batch, result, status="error")
                self.store.update_event_status(
                    (item.message_id for item in batch), "error", result.error
                )
                return
            self._record_agent_result(batch, result, status=outcome)
            if outcome != "delivery_pending":
                self.store.update_event_status(
                    (item.message_id for item in batch), "processed", outcome
                )
            return

        stream_card_id = await self._ensure_streaming_card(
            batch, f"code-{uuid.uuid4().hex[:12]}"
        )
        await self._safe_update_streaming_card(
            stream_card_id, "补充消息过多，本轮已停止自动处理。", 5
        )
        self._record_without_agent(
            batch,
            status="no_reply",
            action="no_reply",
            handled="对方持续补充消息，本轮未抢答",
            reason="max_replans_reached",
        )
        self.store.update_event_status(
            (item.message_id for item in batch), "skipped", "max_replans_reached"
        )


    def _agent_rate_limited(self, contact: Contact) -> bool:
        limits = self.settings.raw.get("rate_limit", {})
        contact_limit = int(limits.get("per_contact_per_hour", 12))
        global_limit = int(limits.get("global_per_hour", 40))
        since = datetime.now(self.settings.timezone) - timedelta(hours=1)
        contact_count, global_count = self.store.agent_run_counts_since(
            since, contact.identity
        )
        return contact_count >= contact_limit or global_count >= global_limit

    async def _merge_latest(self, batch: list[IncomingEvent]) -> list[IncomingEvent]:
        contact = batch[0].contact
        history = await self.dws.history(contact)
        self._register_manual_messages(history)
        earliest = min(item.created_at for item in batch)
        discovered = self._history_events(
            contact, batch[0].conversation_id, history, earliest
        )
        for item in discovered:
            self.store.claim_event(item)
        return self._deduplicate([*batch, *discovered])

    async def _freshness_state(
        self, batch: Sequence[IncomingEvent]
    ) -> tuple[str, list[IncomingEvent]]:
        history = await self.dws.history(batch[0].contact)
        latest_manual = self._register_manual_messages(history)
        earliest = min(item.created_at for item in batch)
        if self._human_owns_conversation(latest_manual, earliest):
            return "manual", []
        known = {item.message_id for item in batch}
        supplements = [
            item
            for item in self._history_events(
                batch[0].contact, batch[0].conversation_id, history, earliest
            )
            if item.message_id not in known
        ]
        return ("supplement", supplements) if supplements else ("fresh", [])

    async def _ensure_streaming_card(
        self, batch: Sequence[IncomingEvent], session_id: str
    ) -> str:
        """失败/过多补充等"需要通知联系人"的终态点创建流式卡片。

        回复路径不再走这里：回复的卡片由 _dispatch_due_delivery 在最终
        新鲜度检查通过后创建，否则补充消息或人工接管取消投递时会留下
        处理中卡片（silent 更是要求决策前零对外输出）。ack 文案只作为
        卡片初始内容，随即被最终内容覆盖。运行内的进度/思考不对外展示
        （begin_response 展示控制是后续独立工作）。创建失败返回空串，
        交付回退纯文本。
        """
        if not getattr(getattr(self, "dws", None), "streaming_cards", False):
            return ""
        if batch[0].conversation_id.startswith("panel:"):
            # 面板内部轮没有对外会话，卡片创建只可能失败，直接回退。
            return ""
        content = self.agent_runtime.render_auto_message(
            "ack",
            {
                "request_count": len(batch),
                "contact_name": batch[0].contact.display_name,
                "contact_alias": batch[0].contact.alias,
                "self_name": self.settings.self_name,
            },
        ) or "正在处理。"
        try:
            return await self._start_streaming_card(
                batch[0].contact,
                batch[0].conversation_id,
                content,
                session_id,
            )
        except Exception as exc:
            LOG.warning(
                "streaming card start failed contact=%s error=%s",
                batch[0].contact.alias,
                sanitize_text(str(exc), 300),
            )
            return ""

    async def _close_active_streaming_card(
        self, conversation_id: str, content: str
    ) -> None:
        """把会话中仍处于处理态的卡片安全收口成终止态；无卡时是空操作。

        卡片正常只在投递派发（最终新鲜度检查之后）或失败通知点创建并随即
        收口；仍会残留处理中卡片的路径是投递重试被补充/人工接管取消。这些
        终态（silent、人工接管、无 agent 处理的跳过路径）必须把残留卡片
        关闭，不能让联系人看到永远"处理中"的卡片。
        """
        active = self.store.active_streaming_card(conversation_id)
        if not active:
            return
        biz_id, send_uuid = active
        # 进程重启或直写数据库时内存映射里没有这张卡；先补上映射，保证
        # DingTalk 卡片关闭后 store 里的卡片状态也收敛到终止态。
        card_send_uuids = getattr(self, "streaming_card_send_uuids", None)
        if card_send_uuids is None:
            card_send_uuids = self.streaming_card_send_uuids = {}
        card_send_uuids.setdefault(biz_id, send_uuid)
        await self._safe_update_streaming_card(biz_id, content, 5)

    async def _sync_progress_card(
        self, batch: Sequence[IncomingEvent], session_id: str
    ) -> None:
        """相1：运行中卡片正文 = 里程碑全量幂等渲染（"- " 前缀纯文本）。

        只在里程碑水位上涨时动作：首个里程碑创建卡片（silent 前仍零输出），
        后续里程碑整表重发。无里程碑不建卡，模型 delta 永不进正文。
        面板内部轮没有对外会话：progress 只进面板事件流，零 DWS 调用。
        """
        if batch[0].conversation_id.startswith("panel:"):
            return
        if not getattr(getattr(self, "dws", None), "streaming_cards", False):
            return
        entries = self.store.recent_progress(
            session_id, limit=_PROGRESS_CARD_LIMIT
        )
        if not entries:
            return
        top_seq = entries[-1]["seq"]
        if top_seq <= self.progress_card_seqs.get(session_id, 0):
            return
        body = "\n".join(f"- {entry['text']}" for entry in entries)
        conversation_id = batch[0].conversation_id
        try:
            active = self.store.active_streaming_card(conversation_id)
            if active:
                biz_id, send_uuid = active
                self.streaming_card_send_uuids.setdefault(biz_id, send_uuid)
                await self._safe_update_streaming_card(biz_id, body, 2)
            else:
                await self._start_streaming_card(
                    batch[0].contact, conversation_id, body, session_id
                )
            self.progress_card_seqs[session_id] = top_seq
        except Exception as exc:
            LOG.warning(
                "progress card sync failed session=%s error=%s",
                session_id,
                sanitize_text(str(exc), 300),
            )

    def _record_without_agent(
        self,
        batch: Sequence[IncomingEvent],
        *,
        status: str,
        action: str,
        handled: str,
        reason: str,
        session_id: str = "",
    ) -> None:
        now = datetime.now(self.settings.timezone)
        self.store.record_run(
            session_id=session_id or f"code-{uuid.uuid4().hex[:12]}",
            conversation_id=batch[0].conversation_id,
            contact=batch[0].contact,
            started_at=now,
            finished_at=now,
            action=action,
            status=status,
            handled=handled,
            reason=reason,
            codex_exit_code=None,
            request_preview=self._request_preview(batch),
        )

    async def _finish_silent(
        self,
        batch: Sequence[IncomingEvent],
        result: AgentResult,
    ) -> None:
        """落地 silent 终态：消费输入、审计留痕，不发消息、不进 outbox。

        回复路径的卡片在投递派发时才创建，silent 决策前正常不存在卡片；
        若会话仍残留处理中卡片（上次投递重试被补充取消），这里把它安全
        收口，除此之外对联系人零输出。
        """
        await self._close_active_streaming_card(
            batch[0].conversation_id, "处理已结束。"
        )
        self._record_agent_result(batch, result, status="silent")
        self.store.update_event_status(
            (item.message_id for item in batch), "processed", "silent"
        )

    def _record_agent_result(
        self,
        batch: Sequence[IncomingEvent],
        result: AgentResult,
        *,
        status: str,
    ) -> None:
        self.store.record_run(
            session_id=result.session_id,
            conversation_id=batch[0].conversation_id,
            contact=batch[0].contact,
            started_at=result.started_at,
            finished_at=result.finished_at,
            action=(
                "reply"
                if result.reply
                else ("no_reply" if result.silent else "handoff")
            ),
            status=status,
            handled=sanitize_text(result.reply, 1000),
            reason=result.error,
            codex_exit_code=result.exit_code,
            request_preview=self._request_preview(batch),
        )
        # 黑板运行在此收口：_record_agent_result 覆盖全部 agent 会话终态
        # （回复/silent/失败/接管/补充重排），close_run 幂等，首次收口生效。
        self.store.close_run(
            result.session_id,
            _BOARD_CLOSE_OUTCOME.get(result.outcome, "failed"),
            now=datetime.now(self.settings.timezone),
        )

    def _task_memory_inactive_days(self) -> int:
        value = int(
            self.settings.raw.get("task_memory", {}).get(
                "inactive_days", _TASK_INACTIVE_DAYS_DEFAULT
            )
        )
        # <=0 视为关闭自动归档（事项继续留在活跃快照里）。
        return value

    def _active_tasks_snapshot(self, contact: Contact) -> str:
        """构建活跃事项快照；加载前先做本地幂等归档。

        与面板 board_tasks 同源转换：每条带最近 3 条 facts（含 source），
        模型在 prompt 里可见跨轮事实，不必先 task_lookup。
        """
        days = self._task_memory_inactive_days()
        if days > 0:
            archived = self.store.archive_inactive_tasks(
                datetime.now(self.settings.timezone), days
            )
            if archived:
                LOG.info("archived inactive tasks count=%d days=%d", archived, days)
        tasks = self.store.board_tasks(
            contact_id=contact.identity, limit=_TASK_SNAPSHOT_LIMIT
        )
        return json.dumps(tasks, ensure_ascii=False, indent=2)

    def _task_context_for(
        self,
        batch: Sequence[IncomingEvent],
        consumed: Iterable[IncomingEvent],
        run_id: str,
    ) -> dict[str, Any]:
        """构建事项工具上下文：message_times 是本运行已覆盖消息的
        id -> 时间戳映射。

        consumed 是已 steer 进当前会话的补充消息，并入映射后模型可以
        引用它们作证据。映射是证据 ID 的唯一校验集与时间戳来源：
        未覆盖的 ID 会被拒绝，引用旧消息的时间就取旧消息的时间。
        """
        covered = [*batch, *consumed]
        return {
            "db_path": str(self.store.path),
            "self_id": self.settings.self_open_id,
            "contact_id": batch[0].contact.identity,
            "conversation_id": batch[0].conversation_id,
            "run_id": run_id,
            # 合成输入（wake/面板指令）不是联系人消息，不得进入证据覆盖集。
            "message_times": {
                item.message_id: item.created_at.isoformat()
                for item in covered
                if not _is_synthetic_message_id(item.message_id)
            },
        }

    def _refresh_task_context(
        self,
        session: "AgentSession",
        batch: Sequence[IncomingEvent],
        consumed: Iterable[IncomingEvent],
    ) -> None:
        """steer 成功后原子重写上下文文件，事项更新改用最新实际证据。

        pi 进程的环境变量无法在启动后修改，所以 prepare() 把上下文落到
        session 目录的 task-context.json，task_tools.py 每次调用优先读它。
        """
        path = getattr(
            getattr(session, "prepared", None), "task_context_path", None
        )
        if path is None:
            return
        context = self._task_context_for(batch, consumed, session.session_id)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(context, ensure_ascii=False), encoding="utf-8"
        )
        os.replace(temporary, path)

    def _prompt_variables(
        self,
        stage: str,
        session_id: str,
        batch: Sequence[IncomingEvent],
        prior_attempt: str,
        recent_context: Sequence[Mapping[str, str]],
    ) -> dict[str, Any]:
        contact = batch[0].contact
        messages = []
        for item in batch:
            # 合成输入不进"联系人消息"清单：它在 prompt 的 <system_wake> 块里。
            if _is_synthetic_message_id(item.message_id):
                continue
            messages.append(
                {
                    "time": item.created_at.isoformat(),
                    "sender": item.contact.display_name,
                    "role": "contact",
                    "conversation_id": item.conversation_id,
                    "message_id": item.message_id,
                    "text": item.content,
                }
            )
        skills = self.settings.raw.get("skills")
        skills = skills if isinstance(skills, Mapping) else {}

        def _skill_path(key: str) -> str:
            # 提示词里引用的技能/脚本路径全部来自 config（skills 段），
            # 支持 ~ 前缀；缺省回退空串，模板文案对空值保持可用。
            value = str(skills.get(key) or "").strip()
            return str(Path(value).expanduser()) if value else ""

        return {
            "self_name": self.settings.self_name,
            "agent_name": self.agent_runtime.profile_name(stage),
            "worker_name": self.agent_runtime.profile_name("worker"),
            "contact_name": contact.display_name,
            "contact_alias": contact.alias,
            "contact_user_id": contact.user_id,
            "contact_open_dingtalk_id": contact.open_dingtalk_id,
            "session_id": session_id,
            "prior_attempt": prior_attempt or "无",
            "workspace_root": self.settings.workspace_root,
            "worktree_root": self.settings.worktree_root,
            "recent_context_json": json.dumps(
                list(recent_context), ensure_ascii=False, indent=2
            ),
            "current_messages_json": json.dumps(
                messages, ensure_ascii=False, indent=2
            ),
            "active_tasks_json": self._active_tasks_snapshot(contact),
            "now": datetime.now(self.settings.timezone).strftime("%Y-%m-%d %H:%M:%S"),
            "skill_humanizer": _skill_path("humanizer_zh"),
            "skill_answer_html": _skill_path("answer_me_with_html"),
            "script_am": _skill_path("answer_me_script"),
            "deliveries_dir": str(self.settings.state_dir / "deliveries"),
        }

    def _render_stage_prompt(
        self, stage: str, variables: Mapping[str, Any]
    ) -> str:
        """渲染阶段提示词；worker = shared（身份/信任/表达契约）+ 阶段模板。

        shared 与阶段模板同目录，两个模板共用同一份变量；缺失或为空时
        退回纯阶段模板，提示词文件可独立部署。front 未启用，维持单模板。
        """
        rendered = self.agent_runtime.render(stage, variables)
        if stage != "worker":
            return rendered
        shared_path = (
            self.agent_runtime._stage(stage).prompt_path.parent
            / SHARED_PROMPT_NAME
        )
        try:
            shared = shared_path.read_text(encoding="utf-8")
        except OSError:
            return rendered
        return assemble_stage_prompt(render_prompt(shared, variables), rendered)

    async def _run_worker_with_fallback(
        self,
        batch: Sequence[IncomingEvent],
        prior_attempt: str,
        *,
        recent_context: Sequence[Mapping[str, str]] = (),
    ) -> AgentResult:
        """按主 worker + fallbacks 链执行；无回复视为该 agent 失败，切换链上下一个。"""
        result = await self._run_agent(
            batch,
            prior_attempt,
            recent_context=recent_context,
        )
        fallbacks = self.agent_runtime._stage("worker").profile.fallbacks
        for index, agent_name in enumerate(fallbacks, start=1):
            if (
                result.reply is not None
                or result.silent
                or result.manual_takeover
                or result.supplements
                or result.fatal
            ):
                # silent 与正常回复同为成功终态，不能触发 fallback 兜底发送；
                # fatal（隔离建立失败等）不允许在未隔离环境重试同一段写操作。
                return result
            LOG.warning(
                "worker fallback engaged agent=%s attempt=%d/%d error=%s",
                agent_name,
                index,
                len(fallbacks),
                result.error[:200],
            )
            result = await self._run_agent(
                batch,
                prior_attempt,
                recent_context=recent_context,
                agent_override=agent_name,
            )
        return result

    async def _run_agent(
        self,
        batch: Sequence[IncomingEvent],
        prior_attempt: str,
        *,
        front: bool = False,
        recent_context: Sequence[Mapping[str, str]] = (),
        agent_override: str | None = None,
    ) -> AgentResult:
        stage = "front" if front else "worker"
        profile = re.sub(
            r"[^a-z0-9]+",
            "-",
            (
                agent_override
                or self.agent_runtime.profile_name(stage)
            ).lower(),
        ).strip("-")
        session_id = (
            f"dm-{datetime.now(self.settings.timezone):%Y%m%d}-{profile}-"
            f"{uuid.uuid4().hex[:10]}"
        )
        self._set_runtime(
            batch, "routing" if front else "worker", session_id=session_id
        )
        started_at = datetime.now(self.settings.timezone)
        temp_root = self.settings.state_dir / "sessions"
        temp_root.mkdir(parents=True, exist_ok=True)
        session_dir = Path(tempfile.mkdtemp(prefix=f"{session_id}-", dir=temp_root))
        prompt = self._render_stage_prompt(
            stage,
            self._prompt_variables(
                stage,
                session_id,
                batch,
                prior_attempt,
                recent_context,
            ),
        )
        synthetic_inputs = [
            (
                "系统唤醒" if item.message_id.startswith("wake-") else "操作者指令",
                item.content,
            )
            for item in batch
            if _is_synthetic_message_id(item.message_id)
        ]
        if synthetic_inputs:
            prompt += "\n\n" + _system_wake_block(synthetic_inputs)
        try:
            stage_profile = self.agent_runtime.resolve_stage(stage, agent_override)
            local_images = (
                () if front else await self._download_images(batch, session_dir)
            )
            has_images = bool(local_images)
            # pi-rpc 原生附带图片（返回的 local_images 保持非空，由 runtime
            # 编码为 ImageContent）；其余非 codex 协议只嵌路径进 prompt。
            prompt, local_images = _apply_image_context(
                prompt, stage_profile.profile.protocol, local_images
            )
            # 有图片时使用多模态模型
            model_override: str | None = None
            if has_images and stage_profile.profile.protocol == "pi-rpc":
                multimodal_model = stage_profile.profile.options.get("multimodal_model")
                if multimodal_model:
                    model_override = str(multimodal_model)
            # 事项工具上下文只注入 pi-rpc：作用域隔离由后端强制，重放按
            # (run_id, tool_call_id) 幂等；其他协议不提供事项工具。
            task_context = (
                self._task_context_for(batch, (), session_id)
                if stage_profile.profile.protocol == "pi-rpc"
                else None
            )
            # open_session 内含 worktree 建立 subprocess（最长 600s）：
            # 放入线程执行，事件循环在慢仓克隆期间仍可调度其他会话。
            # WorktreeError 跨线程原样抛出，失败清理（session_dir 删除）
            # 仍由本协程完成。
            session = await asyncio.to_thread(
                self.agent_runtime.open_session,
                stage, session_id, prompt, session_dir, local_images,
                model_override=model_override,
                agent_override=agent_override,
                task_context=task_context,
            )
            if task_context is not None:
                # 写入租约先于会话启动登记：task_tools.py 直连审计库的
                # 事务在拿到 SQLite 写锁后仍会校验租约，接管/取消后排队
                # 等锁的过期子进程写入会被拒绝，而不是继续落地变更。
                self.store.register_task_run(
                    session_id, datetime.now(self.settings.timezone)
                )
            # 黑板运行随会话开启登记（front/worker 都登）；headline 先以
            # 请求摘要占位，report(progress) 会换成模型写的一句话进度。
            # origin 标记输入来源：wake 合成轮记 synthetic，终态不反向
            # 触发新 wake（回声切断）；面板指令轮按 live 记，其结果照常
            # 传播给等待方。
            self.store.register_run(
                session_id,
                contact_id=batch[0].contact.identity,
                stage=stage,
                headline=self._request_preview(batch)[:80],
                now=datetime.now(self.settings.timezone),
                origin=(
                    "synthetic"
                    if any(
                        item.message_id.startswith("wake-") for item in batch
                    )
                    else "live"
                ),
            )
            worktree_path = getattr(
                getattr(session, "prepared", None), "worktree_path", None
            )
            if worktree_path is not None:
                # 崩溃回收的定位依据：worktree 属于本次运行，进程死后
                # 只有这行记录知道它在哪。
                self.store.set_run_worktree(session_id, worktree_path)
            self.live_sessions[session_id] = session
        except WorktreeError as exc:
            shutil.rmtree(session_dir, ignore_errors=True)
            return AgentResult(
                session_id,
                None,
                None,
                started_at,
                datetime.now(self.settings.timezone),
                error=f"{type(exc).__name__}: {exc}",
                fatal=True,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            shutil.rmtree(session_dir, ignore_errors=True)
            return AgentResult(
                session_id,
                None,
                None,
                started_at,
                datetime.now(self.settings.timezone),
                error=f"{type(exc).__name__}: {exc}",
            )
        supplements: dict[str, IncomingEvent] = {}
        consumed: dict[str, IncomingEvent] = {}

        def revoke_run_lease(reason: str) -> None:
            # 幂等：首次吊销的原因与时间保留，后续调用是 no-op。
            if task_context is not None:
                self.store.revoke_task_run(
                    session_id, reason, datetime.now(self.settings.timezone)
                )

        # 失败保留 session 现场（prompt.md/result.txt/stderr.log 供排障），仅成功路径清理
        session_dir_keep = True
        try:
            await session.start()
            interval = float(self.settings.raw.get("monitor_interval_seconds", 5))
            timeout = session.prepared.timeout_seconds
            now_monotonic = asyncio.get_running_loop().time()
            deadline = now_monotonic + timeout if timeout > 0 else None
            next_history_at = now_monotonic + interval
            known_ids = {item.message_id for item in batch}
            earliest = min(item.created_at for item in batch)
            # 运行内不流式模型 delta：卡片只承载 report(progress) 的里程碑
            # 全量列表（相1）；最终正文在投递时点整体覆盖（相2）。
            # turn_nudged：空终稿补打（turn-stopping）每运行至多一次。
            turn_nudged = False
            while not session.done:
                self._touch_runtime(batch[0].conversation_id)
                self.store.touch_run(
                    session_id, now=datetime.now(self.settings.timezone)
                )
                await self._sync_progress_card(batch, session_id)
                remaining = (
                    deadline - asyncio.get_running_loop().time()
                    if deadline is not None
                    else None
                )
                if remaining is not None and remaining <= 0:
                    revoke_run_lease("agent_timeout")
                    await session.abort()
                    return AgentResult(
                        session_id,
                        None,
                        session.exit_code,
                        started_at,
                        datetime.now(self.settings.timezone),
                        consumed_supplements=sorted(
                            consumed.values(), key=lambda item: item.created_at
                        ),
                        error="agent_timeout",
                    )
                try:
                    wait_timeout = max(
                        0.0, next_history_at - asyncio.get_running_loop().time()
                    )
                    if remaining is not None:
                        wait_timeout = min(wait_timeout, remaining)
                    await session.wait_for_update(timeout=wait_timeout)
                except TimeoutError:
                    pass
                if session.done:
                    if (
                        not turn_nudged
                        and not session.silent
                        and not session.escalated
                        and session.error == "agent_result_missing"
                    ):
                        # 一轮结束却既无正文也未沉默：先补打一条固定收尾
                        # 提示再等一轮（复用本监控循环），仍无结果才按原
                        # error 路径走 fallback 兜底。
                        turn_nudged = True
                        LOG.info("turn nudge engaged session=%s", session_id)
                        if await _reopen_session_for_closing_nudge(session):
                            continue
                    break
                now_monotonic = asyncio.get_running_loop().time()
                if now_monotonic < next_history_at:
                    continue
                next_history_at = now_monotonic + interval
                try:
                    history = await self.dws.history(batch[0].contact)
                except Exception as exc:
                    LOG.warning(
                        "freshness poll failed session=%s error=%s", session_id, exc
                    )
                    continue
                latest_manual = self._register_manual_messages(history)
                if self._human_owns_conversation(latest_manual, started_at):
                    # 人工接管：先吊销写入租约再终止会话，被接管运行的
                    # 子进程（含排队等锁的）不能再落地事项变更，也不批量
                    # 改写事项状态。
                    revoke_run_lease("manual_takeover")
                    await session.abort()
                    return AgentResult(
                        session_id,
                        None,
                        session.exit_code,
                        started_at,
                        datetime.now(self.settings.timezone),
                        manual_takeover=True,
                        consumed_supplements=sorted(
                            consumed.values(), key=lambda item: item.created_at
                        ),
                        error="manual_takeover",
                    )
                new_supplements: list[IncomingEvent] = []
                for item in self._history_events(
                    batch[0].contact, batch[0].conversation_id, history, earliest
                ):
                    if item.message_id in known_ids:
                        continue
                    known_ids.add(item.message_id)
                    supplements[item.message_id] = item
                    new_supplements.append(item)
                    self.store.claim_event(item)
                if new_supplements:
                    steer_prompt = self.agent_runtime.render_supplement(
                        {
                            "supplement_messages_json": json.dumps(
                                [
                                    {
                                        "time": item.created_at.isoformat(),
                                        "message_id": item.message_id,
                                        "text": item.content,
                                    }
                                    for item in new_supplements
                                ],
                                ensure_ascii=False,
                                indent=2,
                            ),
                            "session_id": session_id,
                            "contact_name": batch[0].contact.display_name,
                            "contact_alias": batch[0].contact.alias,
                            "self_name": self.settings.self_name,
                        }
                    )
                    steered = (
                        not _image_resources(new_supplements)
                        and self.agent_runtime.supplement_strategy == "steer"
                        and await session.steer(steer_prompt)
                    )
                    if steered:
                        for item in new_supplements:
                            consumed[item.message_id] = item
                            supplements.pop(item.message_id, None)
                        # 补充消息已进入会话：事项工具上下文改用最新实际
                        # 证据（含补充消息的 ID 与时间），而不是启动快照。
                        self._refresh_task_context(
                            session, batch, consumed.values()
                        )
                        self._set_runtime(
                            [*batch, *consumed.values()],
                            "steering",
                            session_id=session_id,
                        )
                    else:
                        await session.abort()
                        return AgentResult(
                            session_id,
                            None,
                            session.exit_code,
                            started_at,
                            datetime.now(self.settings.timezone),
                            supplements=sorted(
                                supplements.values(), key=lambda item: item.created_at
                            ),
                            consumed_supplements=sorted(
                                consumed.values(), key=lambda item: item.created_at
                            ),
                            error="supplement_restart",
                        )
            if session.silent:
                # no_reply 是模型的显式控制动作且已成功执行：即使其后出现
                # 提供方噪声错误，也不能让错误路径触发兜底发送。
                session_dir_keep = False
                return AgentResult(
                    session_id,
                    None,
                    session.exit_code if session.exit_code is not None else 0,
                    started_at,
                    datetime.now(self.settings.timezone),
                    silent=True,
                    consumed_supplements=sorted(
                        consumed.values(), key=lambda item: item.created_at
                    ),
                )
            if session.error:
                return AgentResult(
                    session_id,
                    None,
                    session.exit_code,
                    started_at,
                    datetime.now(self.settings.timezone),
                    consumed_supplements=sorted(
                        consumed.values(), key=lambda item: item.created_at
                    ),
                    error=session.error,
                )
            session_dir_keep = False
            return AgentResult(
                session_id,
                session.result(),
                session.exit_code if session.exit_code is not None else 0,
                started_at,
                datetime.now(self.settings.timezone),
                escalated=session.escalated,
                blocked=session.blocked,
                consumed_supplements=sorted(
                    consumed.values(), key=lambda item: item.created_at
                ),
            )
        except asyncio.CancelledError:
            # 取消（含服务关停）：先吊销租约再终止会话，防止孤儿子进程
            # 在会话取消后仍落地事项写入。
            revoke_run_lease("run_cancelled")
            await session.abort()
            raise
        except (OSError, RuntimeError, ValueError) as exc:
            return AgentResult(
                session_id,
                None,
                session.exit_code,
                started_at,
                datetime.now(self.settings.timezone),
                supplements=sorted(supplements.values(), key=lambda item: item.created_at),
                consumed_supplements=sorted(
                    consumed.values(), key=lambda item: item.created_at
                ),
                error=f"{type(exc).__name__}: {exc}",
            )
        finally:
            # 会话收尾兜底吊销租约（幂等）：成功/失败/静默路径同样不允许
            # 迟到的子进程写入；此前已按具体原因吊销时这里是 no-op。
            revoke_run_lease("run_finished")
            self.live_sessions.pop(session_id, None)
            self.progress_card_seqs.pop(session_id, None)
            await session.close()
            if session_dir_keep:
                LOG.warning(
                    "agent run failed, kept session dir for debugging session=%s dir=%s",
                    session_id,
                    session_dir,
                )
            else:
                shutil.rmtree(session_dir, ignore_errors=True)

    async def _download_images(
        self, batch: Sequence[IncomingEvent], session_dir: Path
    ) -> tuple[Path, ...]:
        resources = _image_resources(batch)
        if len(resources) > _MAX_IMAGES:
            raise RuntimeError(
                f"media_download_failed: at most {_MAX_IMAGES} images are supported"
            )
        images: list[Path] = []
        for index, (event, media_id) in enumerate(resources, 1):
            downloaded = session_dir / f"image-{index}.download"
            await self.dws.download_media(
                media_id, event.message_id, event.conversation_id, downloaded
            )
            if downloaded.stat().st_size > _MAX_IMAGE_BYTES:
                raise RuntimeError(
                    f"media_download_failed: image exceeds {_MAX_IMAGE_BYTES} bytes"
                )
            image = downloaded.with_suffix(_image_suffix(downloaded))
            downloaded.replace(image)
            images.append(image)
        return tuple(images)

    async def _send_supervisor_text(
        self,
        contact: Contact,
        conversation_id: str,
        content: str,
        session_id: str,
        *,
        retry_network: bool = False,
    ) -> None:
        if self.mode == "shadow":
            LOG.info(
                "shadow supervisor message contact=%s chars=%d",
                contact.alias,
                len(content),
            )
            return
        send_uuid = str(uuid.uuid4())
        sent_at = datetime.now(self.settings.timezone)
        self.store.record_outgoing(
            send_uuid,
            None,
            conversation_id,
            contact.identity,
            sent_at,
            content,
            session_id,
        )
        try:
            message_id, _ = await self.dws.send(
                contact, content, send_uuid, retry_network=retry_network
            )
        except Exception:
            with suppress(Exception):
                self.store.remove_outgoing(send_uuid)
            raise
        if message_id:
            try:
                self.store.record_outgoing(
                    send_uuid,
                    message_id,
                    conversation_id,
                    contact.identity,
                    sent_at,
                    content,
                    session_id,
                )
            except Exception:
                LOG.exception(
                    "outgoing message-id update failed session=%s", session_id
                )

    async def _start_streaming_card(
        self,
        contact: Contact,
        conversation_id: str,
        content: str,
        session_id: str,
    ) -> str:
        if self.mode == "shadow":
            LOG.info(
                "shadow streaming card contact=%s chars=%d",
                contact.alias,
                len(content),
            )
            return ""
        active_card = self.store.active_streaming_card(conversation_id)
        if active_card:
            biz_id, send_uuid = active_card
            card_send_uuids = getattr(self, "streaming_card_send_uuids", None)
            if card_send_uuids is None:
                card_send_uuids = self.streaming_card_send_uuids = {}
            card_send_uuids[biz_id] = send_uuid
            await self._update_streaming_card(biz_id, content, 2)
            return biz_id
        try:
            async with asyncio.timeout(_STREAM_CARD_BIND_TIMEOUT_SECONDS):
                baseline = {
                    message.message_id
                    for message in await self.dws.history(contact)
                    if message.conversation_id == conversation_id
                    and message.sender_open_dingtalk_id
                    == self.settings.self_open_id
                }
        except Exception as exc:
            LOG.warning(
                "streaming card baseline failed contact=%s error=%s",
                contact.alias,
                sanitize_text(str(exc), 300),
            )
            return ""
        sent_at = datetime.now(self.settings.timezone)
        send_uuid = str(uuid.uuid4())
        card_marker = f"处理编号：{send_uuid[:8]}"
        marked_content = f"{content}\n\n{card_marker}"
        biz_id, message_id, _ = await self.dws.send_card(contact)
        self.store.record_outgoing(
            send_uuid,
            message_id,
            conversation_id,
            contact.identity,
            sent_at,
            marked_content,
            session_id,
            card_biz_id=biz_id,
            card_flow_status=2,
        )
        card_send_uuids = getattr(self, "streaming_card_send_uuids", None)
        if card_send_uuids is None:
            card_send_uuids = self.streaming_card_send_uuids = {}
        card_send_uuids[biz_id] = send_uuid
        try:
            await self._update_streaming_card(biz_id, marked_content, 2)
        except Exception:
            await self._safe_update_streaming_card(
                biz_id, "卡片初始化失败，请等待普通文本回复。", 5
            )
            raise
        if message_id:
            await self._safe_update_streaming_card(biz_id, content, 2)
            return biz_id
        new_messages: dict[str, HistoryMessage] = {}
        try:
            async with asyncio.timeout(_STREAM_CARD_BIND_TIMEOUT_SECONDS):
                for delay in (0.25, 0.5, 1.0, 2.0):
                    await asyncio.sleep(delay)
                    history = await self.dws.history(contact)
                    new_messages = {
                        message.message_id: message
                        for message in history
                        if message.message_id not in baseline
                        and message.conversation_id == conversation_id
                        and message.sender_open_dingtalk_id
                        == self.settings.self_open_id
                    }
                    # 只认处理编号，不比对全文：钉钉历史里卡片正文会被改写
                    # （行内代码 `x` 变成 **x**、列表项前的换行被吞掉），全文
                    # 相等在带 Markdown 的回复上几乎必失败，卡片随即被标成
                    # "卡片识别失败"并退回纯文本。
                    marked_candidates = [
                        message
                        for message in new_messages.values()
                        if card_marker in message.content
                    ]
                    if (
                        len(marked_candidates) == 1
                        and self.store.bind_outgoing_id(
                            send_uuid, marked_candidates[0].message_id
                        )
                    ):
                        self.store.remove_manual(marked_candidates[0].message_id)
                        await self._safe_update_streaming_card(
                            biz_id, content, 2
                        )
                        return biz_id
        except Exception as exc:
            LOG.warning(
                "streaming card message-id reconciliation failed session=%s error=%s",
                session_id,
                sanitize_text(str(exc), 300),
            )
        await self._safe_update_streaming_card(
            biz_id, "卡片识别失败，请等待普通文本回复。", 5
        )
        LOG.warning(
            "streaming card message-id ambiguous session=%s new_self_messages=%d",
            session_id,
            len(new_messages),
        )
        return ""

    async def _start_delivery_card(
        self,
        contact: Contact,
        conversation_id: str,
        content: str,
        session_id: str,
    ) -> str:
        """投递时点创建（或复用）承载最终回复的流式卡片。

        只在 _dispatch_due_delivery 的最终新鲜度检查通过后调用；初始内容
        直接用最终回复（含消息编号标记以便回绑 message_id）。创建失败返回
        空串，投递回退纯文本。
        """
        try:
            return await self._start_streaming_card(
                contact, conversation_id, content, session_id
            )
        except Exception as exc:
            LOG.warning(
                "streaming card start failed contact=%s error=%s",
                contact.alias,
                sanitize_text(str(exc), 300),
            )
            return ""

    async def _update_streaming_card(
        self, biz_id: str, content: str, flow_status: int
    ) -> None:
        card_send_uuids = getattr(self, "streaming_card_send_uuids", {})
        try:
            await self.dws.update_card(biz_id, content, flow_status)
            send_uuid = card_send_uuids.get(biz_id)
            if send_uuid:
                self.store.update_streaming_card(send_uuid, content, flow_status)
        finally:
            if flow_status in {3, 5}:
                card_send_uuids.pop(biz_id, None)

    async def _safe_update_streaming_card(
        self, biz_id: str, content: str, flow_status: int
    ) -> None:
        if not biz_id or getattr(self, "mode", "live") == "shadow":
            return
        try:
            await self._update_streaming_card(biz_id, content, flow_status)
        except Exception as exc:
            LOG.warning(
                "streaming card update failed status=%d error=%s",
                flow_status,
                sanitize_text(str(exc), 300),
            )

    async def _send_after_freshness_check(
        self,
        batch: Sequence[IncomingEvent],
        reply: str,
        *,
        session_id: str = "",
    ) -> str:
        """把回复写入发送队列；真正的最终新鲜度检查在投递派发时进行。

        这里不创建流式卡片：卡片由 _dispatch_due_delivery 在人工接管与
        补充消息检查都通过之后创建，保证任何取消路径都不留处理中卡片。
        """
        content = sanitize_text(
            reply,
            int(self.settings.raw.get("max_reply_chars", 16000)),
        )
        if not content:
            raise ValueError("refusing to send an empty reply")
        if batch[0].conversation_id.startswith("panel:"):
            # 面板内部轮的回复只进 panel_replies，不进发送队列、不外发。
            self.store.record_panel_reply(
                session_id or batch[0].message_id,
                content,
                now=datetime.now(self.settings.timezone),
            )
            return "panel"
        self._set_runtime(batch, "sending")
        if self.mode == "shadow":
            LOG.info(
                "shadow reply contact=%s chars=%d",
                batch[0].contact.alias,
                len(content),
            )
            return "shadow"
        outgoing_session = session_id or str(uuid.uuid4())
        self.store.enqueue_delivery(
            str(uuid.uuid4()),
            min(batch, key=lambda item: item.created_at),
            content,
            outgoing_session,
            event_ids=[item.message_id for item in batch],
        )
        self.outbox_wakeup.set()
        LOG.info("reply queued contact=%s", batch[0].contact.alias)
        return "delivery_pending"

    async def _dispatch_outbox(self) -> None:
        LOG.info("outbox dispatcher ready")
        while not self.stop_event.is_set():
            if await self._dispatch_due_delivery():
                continue
            self.outbox_wakeup.clear()
            try:
                await asyncio.wait_for(self.outbox_wakeup.wait(), timeout=2)
            except TimeoutError:
                pass

    async def _dispatch_due_delivery(self) -> bool:
        now = datetime.now(self.settings.timezone)
        row = self.store.claim_due_delivery(now)
        if row is None:
            return False
        send_uuid = str(row["send_uuid"])
        contact = next(
            (
                item
                for item in self.settings.contacts
                if item.identity == row["contact_user_id"]
            ),
            None,
        )
        if contact is None:
            await self._safe_update_streaming_card(
                str(row["card_biz_id"] or ""), "处理已结束。", 5
            )
            self.store.mark_delivery_cancelled(
                send_uuid, "error", "delivery_contact_not_configured"
            )
            return True
        try:
            history = await self.dws.history(contact)
            latest_manual = self._register_manual_messages(history)
            source_received_at = datetime.fromisoformat(row["source_received_at"])
            if self._human_owns_conversation(latest_manual, source_received_at):
                await self._safe_update_streaming_card(
                    str(row["card_biz_id"] or ""),
                    "已停止自动处理，已转人工。",
                    5,
                )
                self.store.mark_delivery_cancelled(
                    send_uuid, "human_cooldown", "manual_takeover"
                )
                return True
            known = set(json.loads(row["event_ids_json"] or "[]"))
            supplements = [
                item
                for item in self._history_events(
                    contact,
                    str(row["conversation_id"]),
                    history,
                    source_received_at,
                )
                if item.message_id not in known
            ]
            if supplements:
                self.store.mark_delivery_cancelled(
                    send_uuid, "stale_supplement", "new_message_before_delivery"
                )
                # 已创建的卡片（如上次网络重试后留下的）保持处理态，交给
                # 下一轮处理复用或在其终态收口；这里不新建也不重复关闭。
                for item in supplements:
                    if self.store.claim_event(item) or self.store.event_is_pending(
                        item.message_id
                    ):
                        self._enqueue(item)
                return True

            content = str(row["payload_text"])
            message_id = ""
            card_flow_status: int | None = None
            card_biz_id = str(row["card_biz_id"] or "")
            if not card_biz_id and getattr(self.dws, "streaming_cards", False):
                # 卡片是投递的一部分：人工接管与补充消息的最终检查都已
                # 通过，此刻才创建；创建后绑定投递行，网络重试复用同卡。
                card_biz_id = await self._start_delivery_card(
                    contact,
                    str(row["conversation_id"]),
                    content,
                    str(row["session_id"]),
                )
                if card_biz_id:
                    self.store.bind_delivery_card(send_uuid, card_biz_id)
            if card_biz_id:
                try:
                    await self._update_streaming_card(card_biz_id, content, 3)
                    card_flow_status = 3
                except Exception as exc:
                    LOG.warning(
                        "streaming card final update failed; falling back to text "
                        "session=%s error=%s",
                        row["session_id"],
                        sanitize_text(str(exc), 300),
                    )
                    message_id, _ = await self.dws.send(
                        contact, content, send_uuid, retry_network=True
                    )
                    card_flow_status = 5
            else:
                message_id, _ = await self.dws.send(
                    contact, content, send_uuid, retry_network=True
                )
            self.store.mark_delivery_sent(
                send_uuid,
                message_id,
                card_flow_status=card_flow_status,
            )
            LOG.info("reply sent contact=%s", contact.alias)
        except Exception as exc:
            delay = min(2 ** int(row["attempt_count"]), 60)
            self.store.mark_delivery_retry(
                send_uuid,
                f"{type(exc).__name__}: {exc}",
                now + timedelta(seconds=delay),
            )
            LOG.warning(
                "delivery deferred contact=%s error=%s",
                contact.alias,
                sanitize_text(str(exc), 300),
            )
        return True

    # ------------------------------------------------------------------
    # 唤醒（§18 唤醒 2 条）：任务终态唤醒等待方 + waiting_on=@时刻到点唤醒。
    # 循环只做三件事：扫时刻、排空终态事件、把到点 wake 合成输入进联系人
    # 队列；合成轮复用 _handle_batch 全链，message_id 带 wake- 前缀标记。
    # ------------------------------------------------------------------

    async def _consume_wakes(self) -> None:
        interval = float(
            self.settings.raw.get(
                "wake_interval_seconds", _WAKE_INTERVAL_SECONDS_DEFAULT
            )
        )
        LOG.info("wake dispatcher ready interval=%ss", interval)
        while not self.stop_event.is_set():
            await asyncio.sleep(interval)
            try:
                now = datetime.now(self.settings.timezone)
                self._scan_time_wakes(now)
                self._drain_terminal_events(now)
                for wake in self.store.due_wakes(now):
                    await self._dispatch_wake(wake, now)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("wake dispatcher iteration failed")

    def _scan_time_wakes(self, now: datetime) -> None:
        """扫 waiting_on=@时刻 的事项：到点（顺延入窗）唤醒主联系人。

        去重键是 (task_id, waiting_on 当前值) 且只看未派发的 wake：
        改约时间（waiting_on 变化）后到点会再次唤醒，同一时刻的
        pending/claimed 不会重复入队。专用查询不截断，board_tasks 的
        50 条上限不再吃掉排位靠后的等待事项。
        """
        for task in self.store.time_waiting_tasks():
            match = _TIME_WAITING_PATTERN.fullmatch(task["waiting_on"])
            if not match:
                continue
            moment = _parse_waiting_moment(match.group(1), self.settings.timezone)
            if moment is None:
                continue
            effective = _roll_into_wake_window(moment)
            if now < effective:
                continue
            if self.store.wake_exists(task["id"], task["waiting_on"]):
                continue
            contact_ids = task["contact_ids"] or [task["contact_id"]]
            self.store.enqueue_wake(
                contact_ids[0],
                f"到点检查：{task['goal']}",
                origin_task_id=task["id"],
                waiting_on=task["waiting_on"],
                due_at=now,
                now=now,
            )

    def _drain_terminal_events(self, now: datetime) -> None:
        """把任务终态事件换成等待方的 wake；synthetic 轮终态不再传（回声切断）。

        单事件单事务（store.fan_out_terminal_event）：wakes 生成与事件
        标记 handled 同生共死，处理中崩溃只会整体重试，不会半提交。
        """
        for event in self.store.pending_terminal_events():
            self.store.fan_out_terminal_event(int(event["id"]), now=now)

    async def _steer_live_session(self, contact_id: str, text: str) -> bool:
        """把唤醒文本 steer 进该联系人正在跑的会话；成功返回 True。

        在跑会话比批队列快一个量级：它下一轮 digest 就能看到系统唤醒块。
        任一会话 steer 失败按无活跃处理，走合成输入兜底。
        """
        for run in self.store.active_runs():
            if str(run["contact_id"]) != contact_id:
                continue
            session = self.live_sessions.get(str(run["session_id"]))
            if session is None or getattr(session, "done", True):
                continue
            try:
                steered = await session.steer(
                    _system_wake_block([("系统唤醒", text)])
                )
            except Exception as exc:
                LOG.warning(
                    "wake steer failed session=%s error=%s",
                    run["session_id"],
                    sanitize_text(str(exc), 300),
                )
                continue
            if steered:
                return True
        return False

    async def _dispatch_wake(self, wake: Mapping[str, Any], now: datetime) -> None:
        """把到点 wake 送达：在跑会话直接 steer，否则合成输入进批队列。

        生命周期 pending -> claimed -> done：claim 之后崩溃的 wake 由
        启动扫尾回置 pending 重放；派发失败也置 done 并记 error，绝不
        无限重试同一条。
        """
        wake_id = int(wake["id"])
        if not self.store.claim_wake(wake_id, now=now):
            return
        text = str(wake["text"])
        contact_id = str(wake["contact_id"])
        contact = next(
            (
                item
                for item in self.settings.contacts
                if item.identity == contact_id
            ),
            None,
        )
        if contact is None:
            self.store.finish_wake(
                wake_id, now=now, error=f"contact_not_whitelisted:{contact_id}"
            )
            LOG.warning(
                "wake done with error: contact not whitelisted contact=%s",
                contact_id,
            )
            return
        if await self._steer_live_session(contact_id, text):
            self.store.finish_wake(wake_id, now=now)
            LOG.info(
                "wake steered to live session contact=%s origin_task=%s",
                contact.alias,
                wake.get("origin_task_id") or "-",
            )
            return
        event = IncomingEvent(
            event_id=f"wake-{wake_id}",
            message_id=f"wake-{wake_id}",
            conversation_id=(
                self.store.latest_conversation_id(contact.identity)
                or f"dm:{contact.identity}"
            ),
            contact=contact,
            content=text,
            created_at=now,
        )
        self.store.claim_event(event)
        self._enqueue(event)
        self.store.finish_wake(wake_id, now=now)
        LOG.info(
            "wake dispatched contact=%s origin_task=%s",
            contact.alias,
            wake.get("origin_task_id") or "-",
        )

    # ------------------------------------------------------------------
    # 操作者面板服务契约（§18 面板写2）：面板直调，钉死签名。
    # 面板 steer = 操作者指令：不终止会话、不写 manual 记录、不冷却。
    # ------------------------------------------------------------------

    async def open_operator_session(
        self, text: str, contact_alias: str | None
    ) -> str:
        """以操作者身份合成一轮输入，返回面板消息 id（panel-<uuid>）。

        无 contact：contact_id 用 'operator' 的内部轮，回复只落
        panel_replies 不外发；有 contact：进该联系人真实会话队列走全链路。
        动作本身落 wakes 审计行（contact_id='operator'，status='done'）。
        """
        now = datetime.now(self.settings.timezone)
        panel_id = f"panel-{uuid.uuid4().hex[:12]}"
        contact = None
        if contact_alias:
            contact = next(
                (
                    item
                    for item in self.settings.contacts
                    if item.alias == contact_alias
                ),
                None,
            )
            if contact is None:
                raise ValueError(f"unknown contact alias: {contact_alias}")
        if contact is None:
            contact = Contact(
                alias="operator",
                display_name=self.settings.self_name,
                user_id="",
                open_dingtalk_id=self.settings.self_open_id,
            )
            conversation_id = f"panel:{panel_id}"
        else:
            conversation_id = (
                self.store.latest_conversation_id(contact.identity)
                or f"dm:{contact.identity}"
            )
        event = IncomingEvent(
            event_id=panel_id,
            message_id=panel_id,
            conversation_id=conversation_id,
            contact=contact,
            content=text,
            created_at=now,
        )
        self.store.claim_event(event)
        self.store.record_operator_action(
            text, run_session_id=panel_id, now=now
        )
        self._enqueue(event)
        return panel_id

    async def steer_run(self, session_id: str, text: str) -> bool:
        """给在跑会话注入操作者指令；会话不在跑返回 False。绝不写 manual。

        成功的注入落 wakes 审计行（同 open_operator_session 的结构），
        操作者对在跑会话的每次插话都有留痕可查。
        """
        session = self.live_sessions.get(session_id)
        if session is None or getattr(session, "done", True):
            return False
        steered = bool(
            await session.steer(_system_wake_block([("操作者指令", text)]))
        )
        if steered:
            self.store.record_operator_action(
                text, run_session_id=session_id, now=datetime.now(
                    self.settings.timezone
                )
            )
        return steered

    async def answer_task(self, task_id: int | str, answer: str) -> bool:
        """给 waiting_on=本人名字 的任务作答：落 human 事实、恢复 open、唤醒联系人。

        答复与其触发的 wakes 由 store 在同一事务内落地，无半提交窗口。
        """
        answer = sanitize_text(answer, 2000).strip()
        if not answer:
            return False
        resolved = self.store.answer_waiting_task(
            str(task_id),
            self.settings.self_name,
            answer,
            now=datetime.now(self.settings.timezone),
        )
        return bool(resolved.get("ok"))

    def board_snapshot(self) -> dict[str, Any]:
        """黑板投影快照：在跑会话（含里程碑）、任务、等我裁决、近 24h 完成、
        面板内部轮回复。"""
        now = datetime.now(self.settings.timezone)
        runs = []
        for run in self.store.active_runs():
            entry = dict(run)
            entry["recent_progress"] = self.store.recent_progress(
                entry["session_id"], limit=5
            )
            runs.append(entry)
        day_ago = now - timedelta(hours=24)
        recent_done = []
        for task in self.store.board_tasks(status="completed", limit=50):
            with suppress(ValueError):
                if datetime.fromisoformat(task["updated_at"]) >= day_ago:
                    recent_done.append(task)
        return {
            "runs": runs,
            "tasks": self.store.board_tasks(include_done=True),
            "recent_failed": self.store.recent_failed_runs(since=day_ago),
            "waiting_operator": [
                task
                for task in self.store.board_tasks(status="waiting")
                if task["waiting_on"] == self.settings.self_name
            ],
            "recent_done": recent_done,
            "panel_replies": self.store.recent_panel_replies(limit=20),
        }


def _validate_configuration(settings: Settings) -> list[str]:
    errors: list[str] = []
    aliases = [item.alias for item in settings.contacts]
    user_ids = [item.user_id for item in settings.contacts if item.user_id]
    open_ids = [item.open_dingtalk_id for item in settings.contacts]
    if not settings.contacts:
        errors.append("contacts must not be empty")
    if len(set(aliases)) != len(aliases):
        errors.append("duplicate contact alias")
    if len(set(user_ids)) != len(user_ids) or len(set(open_ids)) != len(open_ids):
        errors.append("duplicate DingTalk identity")
    if settings.self_open_id in open_ids:
        errors.append("self identity appears in listener whitelist")
    if settings.quiet_window <= 0 or settings.cooldown < 0:
        errors.append("quiet window must be positive and cooldown must be non-negative")
    try:
        AgentRuntime.from_config(
            settings.raw, settings.config_path, settings.workspace_root
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        errors.append(f"agent workflow is invalid: {exc}")
    if not settings.workspace_root.exists():
        errors.append(f"workspace_root does not exist: {settings.workspace_root}")
    return errors


async def _doctor(settings: Settings) -> int:
    checks: list[tuple[str, bool, str]] = []
    errors = _validate_configuration(settings)
    checks.append(("config", not errors, "; ".join(errors) if errors else "ok"))
    binaries = {
        "dws": str(settings.raw.get("dws", {}).get("binary", "dws")),
    }
    if not errors:
        runtime = AgentRuntime.from_config(
            settings.raw, settings.config_path, settings.workspace_root
        )
        binaries.update(runtime.required_binaries())
    for binary_name, binary in binaries.items():
        resolved = shutil.which(binary)
        checks.append((binary_name, bool(resolved), resolved or "not found"))
    if not errors and shutil.which(str(settings.raw.get("dws", {}).get("binary", "dws"))):
        contact = settings.contacts[0]
        command = _event_consume_command(settings, contact, dry_run=True)
        process = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        checks.append(
            (
                "dws event dry-run",
                process.returncode == 0,
                (stdout or stderr).decode("utf-8", errors="replace").splitlines()[0][:200],
            )
        )
    for name, ok, detail in checks:
        print(f"{'PASS' if ok else 'FAIL'} {name}: {detail}")
    return 0 if all(item[1] for item in checks) else 1


def _acquire_single_instance(state_dir: Path):
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = state_dir / "agent.lock"
    handle = lock_path.open("a+")
    try:
        if os.name == "nt":
            if lock_path.stat().st_size == 0:
                handle.write("0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        handle.close()
        raise RuntimeError("another dws-chat-agent instance is already running")
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


async def _run_service(
    settings: Settings, mode: str | None, *, open_dashboard: bool = False
) -> int:
    lock_handle = _acquire_single_instance(settings.state_dir)
    service = AgentService(settings, mode, open_dashboard=open_dashboard)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, service.request_stop)
    try:
        await service.run()
    finally:
        service.close()
        lock_handle.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="DingTalk chat agent")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV)
    parser.add_argument("--verbose", action="store_true")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser("run", help="consume DWS events")
    run_parser.add_argument("--mode", choices=("shadow", "live"))
    run_parser.add_argument(
        "--open-dashboard", action="store_true", help="open the local dashboard"
    )
    subparsers.add_parser("doctor", help="validate local runtime without sending")
    gate_parser = subparsers.add_parser("probe-gate", help="run the zero-token gate")
    gate_parser.add_argument("text")
    return parser


async def async_main(arguments: argparse.Namespace) -> int:
    try:
        settings = load_settings(arguments.config, arguments.env_file)
    except (OSError, ValueError, KeyError) as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    configure_logging(settings.state_dir, arguments.verbose)
    if arguments.command == "doctor":
        return await _doctor(settings)
    if arguments.command == "probe-gate":
        security = settings.raw.get("security", {})
        gate = SecurityGate(
            int(security.get("max_message_chars", 12000))
        )
        result: GateResult = gate.inspect([arguments.text])
        print(json.dumps(result.__dict__, ensure_ascii=False))
        return 0
    if arguments.command == "run":
        errors = _validate_configuration(settings)
        if errors:
            for error in errors:
                print(f"configuration error: {error}", file=sys.stderr)
            return 2
        return await _run_service(
            settings, arguments.mode, open_dashboard=arguments.open_dashboard
        )
    return 2


def main() -> int:
    return asyncio.run(async_main(build_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
