"""task_record/task_lookup/report(progress) 的 CLI 后端，由 pi 扩展
pi_dws_tools.ts 按次拉起。

输入：环境变量 DWS_TASK_CONTEXT_FILE（优先，服务在 steer 进补充消息后
会原子重写）或 DWS_TASK_CONTEXT（会话启动时的初始快照），内容为 JSON：
db_path/self_id/contact_id/conversation_id/run_id/message_times（本运行
已覆盖消息的 id -> 时间戳映射）；
stdin 一行 JSON 请求
{"tool": "task_record"|"task_lookup"|"report", "tool_call_id": str, "args": {...}}。
输出：stdout 一行 JSON 结果，始终退出码 0，错误以 {"ok": false, "error"} 返回，
让模型读到失败原因而不是让工具调用挂掉。

report 的 final/blocked 不走本后端：终止与正文传递由 pi 扩展写
DWS_REPORT_FILE、runtime 在 tool_execution_end 捕获（同 no_reply 通路），
收口由服务在终态 close_run 完成。

同一 (run_id, tool_call_id) 的重复事件直接返回已落地的响应，不重复创建事项。
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

# 本脚本由 pi_dws_tools.ts 以 `python <script>` 直接拉起，没有包上下文，
# 相对导入会直接 ImportError。把 src/ 加入 sys.path 后改用包内绝对导入，
# 不依赖安装方式或调用方 cwd（escalate_mcp.py 不导入本包，无此问题）。
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from dws_dm_agent.core import AuditStore, TaskScope, sanitize_text

_MAX_MESSAGE_IDS = 12
_MAX_RESOURCES = 12
_MAX_FACTS = 8
_REPORT_KINDS = {"progress", "final", "blocked"}


def _context_from_env() -> dict[str, Any]:
    # 优先读服务刷新过的上下文文件：steer 进补充消息后，可引用的证据
    # 消息集合应扩展为本运行已覆盖的最新消息，而不是会话启动时的快照。
    # 文件缺失或损坏时回退到环境变量里的初始快照，不让工具挂掉。
    file_path = os.environ.get("DWS_TASK_CONTEXT_FILE", "")
    if file_path:
        try:
            value = json.loads(Path(file_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            value = None
        if isinstance(value, Mapping):
            return dict(value)
    raw = os.environ.get("DWS_TASK_CONTEXT", "")
    if not raw:
        raise ValueError("DWS_TASK_CONTEXT is not configured")
    value = json.loads(raw)
    if not isinstance(value, Mapping):
        raise ValueError("DWS_TASK_CONTEXT must be a JSON object")
    return dict(value)


def _message_time_map(context: Mapping[str, Any]) -> dict[str, datetime] | str:
    """解析上下文的 message_times（消息 id -> 时间戳）。

    返回错误字符串表示上下文不可用；映射可以为空（纯 wake/面板轮没有
    覆盖任何联系人消息，调用方按空覆盖集放宽锚定）。时间戳必须可解析
    且带时区，后续 max 比较才有意义。
    """
    raw = context.get("message_times")
    if not isinstance(raw, Mapping):
        return "task_context_missing_message_times"
    mapping: dict[str, datetime] = {}
    for key, value in raw.items():
        try:
            parsed = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return "task_context_invalid_message_time"
        if not parsed.tzinfo:
            return "task_context_invalid_message_time"
        mapping[str(key)] = parsed
    return mapping


def _clean_str(value: Any, max_chars: int) -> str:
    return sanitize_text(value, max_chars).strip()


def _optional_str(
    args: Mapping[str, Any], key: str, max_chars: int
) -> str | None:
    """读取可选字段：缺省或 null 返回 None（本次不提交该字段）。

    显式传空串按提交处理，用于 waiting_on 这类“传空即清空”的字段；
    goal/status/evidence_summary 的空串在后端按未提交处理。
    """
    value = args.get(key)
    if value is None:
        return None
    return _clean_str(value, max_chars)


def _clean_message_ids(value: Any) -> list[str]:
    """清洗模型提交的证据消息 ID；不做任何自动补齐。

    自动附加本运行全部消息 ID 会让无关确认也构成“新证据”而续期事项，
    因此这里只保留模型显式引用的 ID，再由调用方对照上下文校验。
    """
    ids: list[str] = []
    if isinstance(value, (list, tuple)):
        ids.extend(_clean_str(item, 120) for item in value)
    seen: set[str] = set()
    result: list[str] = []
    for item in ids:
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result[:_MAX_MESSAGE_IDS]


def _clean_resources(value: Any) -> list[str]:
    """清洗资源标签（字符串数组，如 "repo:lucid"）；后端按整组替换语义使用。"""
    items: list[str] = []
    if isinstance(value, (list, tuple)):
        items.extend(_clean_str(item, 200) for item in value)
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result[:_MAX_RESOURCES]


def _clean_facts(value: Any) -> list[dict[str, str]] | None:
    """清洗 facts 数组；返回 None 表示本次不提交事实。

    返回错误字符串的职责在调用方：锚定 ID 的覆盖集校验必须对照
    message_times，这里只做形状与文本清洗。
    """
    if value is None:
        return None
    facts: list[dict[str, str]] = []
    if isinstance(value, (list, tuple)):
        for item in value:
            if not isinstance(item, Mapping):
                continue
            text = _clean_str(item.get("text"), 2000)
            anchor = _clean_str(item.get("anchor_message_id"), 120)
            if text:
                facts.append({"text": text, "anchor_message_id": anchor})
    return facts[:_MAX_FACTS]


def execute_request(request: Mapping[str, Any], context: Mapping[str, Any]) -> dict[str, Any]:
    """执行一次 task_record/task_lookup/report 请求；所有边界校验都在这里完成。"""
    tool = str(request.get("tool") or "")
    if tool not in {"task_record", "task_lookup", "report"}:
        return {"ok": False, "error": f"unknown_tool: {tool or '(empty)'}"}
    scope = TaskScope(
        self_id=str(context.get("self_id") or ""),
        contact_id=str(context.get("contact_id") or ""),
        conversation_id=str(context.get("conversation_id") or ""),
    )
    if not all((scope.self_id, scope.contact_id, scope.conversation_id)):
        return {"ok": False, "error": "task_scope_missing"}
    db_path = str(context.get("db_path") or "")
    if not db_path:
        return {"ok": False, "error": "task_db_missing"}
    run_id = str(context.get("run_id") or "")
    tool_call_id = str(request.get("tool_call_id") or "")
    args = request.get("args")
    args = args if isinstance(args, Mapping) else {}

    if tool == "report":
        return _execute_report(args, db_path, run_id)

    store = AuditStore(Path(db_path))
    try:
        now = datetime.now().astimezone()
        evidence_ids: list[str] = []
        evidence_at = now
        if tool == "task_record":
            message_times = _message_time_map(context)
            if isinstance(message_times, str):
                return {"ok": False, "error": message_times}
            evidence_ids = _clean_message_ids(args.get("evidence_message_ids"))
            facts = _clean_facts(args.get("facts"))
            unknown = [
                item for item in evidence_ids if item not in message_times
            ]
            if facts is not None:
                for fact in facts:
                    anchor = fact["anchor_message_id"]
                    # 覆盖集为空（纯 wake/面板轮）：只放行无锚事实，锚非空
                    # 即未知。覆盖集非空：锚必须落在覆盖消息上，空锚同样
                    # 按未知拒绝（照旧严格）。
                    if anchor and anchor not in message_times:
                        unknown.append(anchor)
                    elif not anchor and message_times:
                        unknown.append(anchor)
            if unknown:
                # 未知 ID 一律拒绝，绝不带着“当前时间”兜底落库：那会把
                # 无法核实的引用伪装成最新证据，让事项凭空调新。facts
                # 的锚定与证据共用同一覆盖集校验。
                return {
                    "ok": False,
                    "error": (
                        "unknown_evidence_message_id: "
                        + ", ".join(unknown[:3])
                    ),
                }
            # 证据时间取被引用消息自身的最新时间戳：引用旧消息，时间就
            # 是旧时间；未引用时才用本运行已覆盖消息的最新时间（新建
            # 事项的触发消息就是当前输入）。覆盖集为空（纯唤醒/面板轮）
            # 没有可引用的消息，证据时间退回当前时刻。
            if message_times:
                evidence_at = max(
                    (message_times[item] for item in evidence_ids),
                    default=max(message_times.values()),
                )
        if tool == "task_lookup":
            # 黑板读：跨联系人查整块黑板；include_done 含 completed/cancelled。
            response = store.apply_task_lookup(
                contact_id=_clean_str(args.get("contact"), 200),
                status=_clean_str(args.get("status"), 32),
                include_done=bool(args.get("include_done", False)),
                limit=_int_or(args.get("limit"), 20),
                now=now,
                run_id=run_id,
                tool_call_id=tool_call_id,
            )
        else:
            expected_version = args.get("expected_version")
            # 收据与事项变更同一事务落地：崩溃后要么都发生要么都不发生，
            # 重复事件不会重复创建或重复更新事项。
            response = store.apply_task_record(
                scope=scope,
                record={
                    "task_id": _clean_str(args.get("task_id"), 64),
                    "goal": _optional_str(args, "goal", 2000),
                    "status": _optional_str(args, "status", 32),
                    "waiting_on": _optional_str(args, "waiting_on", 500),
                    "evidence_summary": _optional_str(args, "evidence_summary", 2000),
                    "evidence_message_ids": evidence_ids,
                    "resources": (
                        _clean_resources(args.get("resources"))
                        if args.get("resources") is not None
                        else None
                    ),
                    "facts": _clean_facts(args.get("facts")),
                    "expected_version": (
                        int(expected_version)
                        if isinstance(expected_version, int)
                        and not isinstance(expected_version, bool)
                        else None
                    ),
                    "restore": bool(args.get("restore", False)),
                },
                evidence_at=evidence_at,
                now=now,
                run_id=run_id,
                tool_call_id=tool_call_id,
            )
        return response
    finally:
        store.close()


def _execute_report(
    args: Mapping[str, Any],
    db_path: str,
    run_id: str,
) -> dict[str, Any]:
    """report(progress)：落进度流水并刷新运行 headline，不结束会话。

    final/blocked 由 pi 扩展直接写 DWS_REPORT_FILE 终止会话，不进本后端。
    """
    kind = _clean_str(args.get("kind"), 16)
    text = sanitize_text(args.get("text"), 2000).strip()
    if kind not in _REPORT_KINDS:
        return {"ok": False, "error": f"invalid_report_kind: {kind or '(empty)'}"}
    if kind != "progress":
        return {
            "ok": False,
            "error": "report_final_blocked_not_via_backend",
        }
    if not text:
        return {"ok": False, "error": "report_text_required"}
    if not run_id:
        return {"ok": False, "error": "task_run_missing"}
    store = AuditStore(Path(db_path))
    try:
        now = datetime.now().astimezone()
        seq = store.add_progress(run_id, text, now=now)
        store.touch_run(run_id, headline=text[:80], now=now)
        return {"ok": True, "seq": seq}
    finally:
        store.close()


def _int_or(value: Any, default: int) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return default


def main() -> int:
    try:
        context = _context_from_env()
        line = sys.stdin.readline()
        request = json.loads(line) if line.strip() else {}
        if not isinstance(request, dict):
            request = {}
        response = execute_request(request, context)
    except Exception as exc:  # noqa: BLE001 - CLI 边界：任何错误都以 JSON 返回
        response = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(response, ensure_ascii=False, separators=(",", ":")), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
