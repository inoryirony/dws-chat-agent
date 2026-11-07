"""dws 服务工具的通用 stdio MCP server（server 名 dws-tools）。

为什么需要：no_reply/report/task_record/task_lookup 此前只有 pi 挂载形态
（pi_dws_tools.ts 扩展）与 task_cli 的 bash 形态（task_record/task_lookup
子集）。claude/codex/dsh worker 要拿到与 pi 相同的工具面，就经本 server
以 MCP 工具暴露同名工具：

- task_record/task_lookup/report(progress)：直接复用 task_tools.execute_request，
  与 pi/bash 通路共用同一套边界校验、覆盖集锚定与 (run_id, tool_call_id)
  幂等收据，不另写第二套后端。
- report(final/blocked)：与 pi 扩展同一交接协议——把 {kind, text} 写进
  DWS_REPORT_FILE，runtime 在工具收尾事件（各 adapter 已接线）读文件
  按终稿收口会话。
- no_reply：本工具即信号，adapter 在工具事件里匹配名字即 _capture_silent；
  server 侧只回执。

环境依赖（runtime.prepare 在 task_cli/service_tools_mcp 时注入）：
DWS_TASK_CONTEXT / DWS_TASK_CONTEXT_FILE / DWS_REPORT_FILE。
缺 DWS_TASK_CONTEXT 时 task_record/task_lookup/report(progress) 返回
错误文本（模型可读到原因），no_reply 与 report(final/blocked) 不依赖它。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

# 与 task_tools.py 同理：本脚本可能被外部进程（claude/codex/dsh 拉起的
# MCP client）直接 spawn，没有包上下文，必须先把 src/ 加进 sys.path。
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from dws_dm_agent.runners import task_tools  # noqa: E402


TOOLS = [
    {
        "name": "no_reply",
        "description": "结束本次处理且不发送最终正文。",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "report",
        "description": (
            "progress：对外可见的简短里程碑；final/blocked：以给定正文收口本轮"
            "（final=正常交付，blocked=等待对方或外部条件）。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["progress", "final", "blocked"],
                },
                "text": {"type": "string"},
            },
            "required": ["kind", "text"],
            "additionalProperties": False,
        },
    },
    {
        "name": "task_record",
        "description": "登记/更新一条长期事项（goal、状态、等待条件、证据、事实）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "goal": {"type": "string"},
                "status": {"type": "string", "enum": ["active", "waiting", "completed", "cancelled"]},
                "ask": {"type": "string"},
                "waiting_on": {"type": "string"},
                "evidence_summary": {"type": "string"},
                "evidence_message_ids": {"type": "array", "items": {"type": "string"}},
                "facts": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "text": {"type": "string"},
                            "anchor_message_id": {"type": "string"},
                        },
                        "required": ["text"],
                    },
                },
                "resources": {"type": "array", "items": {"type": "string"}},
                "expected_version": {"type": "integer"},
                "restore": {"type": "boolean"},
            },
            "required": ["task_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "task_lookup",
        "description": "查询长期事项（含跨联系人黑板读）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "contact": {"type": "string"},
                "status": {"type": "string"},
                "include_done": {"type": "boolean"},
                "include_archived": {"type": "boolean"},
                "limit": {"type": "integer"},
            },
            "additionalProperties": False,
        },
    },
]


def _respond(request_id: Any, result: Any) -> None:
    print(
        json.dumps(
            {"jsonrpc": "2.0", "id": request_id, "result": result},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        flush=True,
    )


def _error(request_id: Any, message: str) -> None:
    print(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32000, "message": message},
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        flush=True,
    )


def _execute_tool(name: str, request_id: Any, arguments: Any) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        arguments = {}
    if name == "no_reply":
        return {"ok": True}
    if name == "report":
        return _execute_report(arguments)
    response = task_tools.execute_request(
        {"tool": name, "tool_call_id": str(request_id), "args": arguments},
        task_tools._context_from_env(),
    )
    return response if isinstance(response, dict) else {"ok": False, "error": "bad_response"}


def _execute_report(arguments: dict[str, Any]) -> dict[str, Any]:
    kind = str(arguments.get("kind") or "")
    text = str(arguments.get("text") or "").strip()
    if kind == "progress":
        response = task_tools.execute_request(
            {
                "tool": "report",
                "tool_call_id": "report-progress",
                "args": arguments,
            },
            task_tools._context_from_env(),
        )
        return response if isinstance(response, dict) else {"ok": False, "error": "bad_response"}
    if kind not in {"final", "blocked"}:
        return {"ok": False, "error": f"invalid_report_kind: {kind or '(empty)'}"}
    if not text:
        return {"ok": False, "error": "report_text_required"}
    report_file = os.environ.get("DWS_REPORT_FILE", "")
    if not report_file:
        return {"ok": False, "error": "report_file_not_configured"}
    payload = json.dumps({"kind": kind, "text": text}, ensure_ascii=False)
    Path(report_file).write_text(payload, encoding="utf-8")
    return {"ok": True}


def main() -> None:
    for line in sys.stdin:
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(request, dict):
            continue
        method = request.get("method")
        request_id = request.get("id")
        if method == "initialize":
            _respond(
                request_id,
                {
                    "protocolVersion": request.get("params", {}).get(
                        "protocolVersion", "2025-06-18"
                    ),
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "dws-tools", "version": "0.1.0"},
                },
            )
        elif method == "ping":
            _respond(request_id, {})
        elif method == "tools/list":
            _respond(request_id, {"tools": TOOLS})
        elif method == "tools/call":
            params = request.get("params", {})
            name = str(params.get("name") or "") if isinstance(params, dict) else ""
            if name not in {"no_reply", "report", "task_record", "task_lookup"}:
                _error(request_id, f"unknown_tool: {name or '(empty)'}")
                continue
            try:
                response = _execute_tool(name, request_id, params.get("arguments"))
            except Exception as exc:  # noqa: BLE001 - 工具边界：错误回给模型
                response = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            _respond(
                request_id,
                {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                response, ensure_ascii=False, separators=(",", ":")
                            ),
                        }
                    ]
                },
            )
        elif request_id is not None:
            _error(request_id, "Method not found")


if __name__ == "__main__":
    main()
