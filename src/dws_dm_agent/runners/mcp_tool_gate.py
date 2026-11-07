"""通用 stdio MCP 工具闸门：把上游 MCP server 的工具面收窄到白名单。

用途：pi 的 mcp.json 有 directTools 原生白名单，claude/codex/dsh 没有等
价物（dsh-mcp-client 与 codex 的 server schema 均无工具过滤键；claude 的
--allowedTools 是 permission 语义，模型仍看得到全部工具）。本闸门在装配层
把带 directTools 声明的 server 包一层：

    python mcp_tool_gate.py --allow a,b -- <原 command> [原 args...]

协议处理（MCP stdio transport，NDJSON 逐行 JSON-RPC）：
- tools/list 响应：只保留白名单内工具，其余从模型视野移除；
- tools/call 请求：白名单外直接回 JSON-RPC error，不转发上游；
- 其余一切消息（initialize/cancel/通知/资源等）原样双向透传。

崩了 fail-loud：上游退出即整体退出，让宿主 adapter 的错误路径接手。
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any


def _parse_args(argv: list[str]) -> tuple[set[str], list[str]]:
    if "--" not in argv:
        raise SystemExit("mcp_tool_gate: expected -- before the upstream command")
    head, tail = argv[: argv.index("--")], argv[argv.index("--") + 1 :]
    allow: list[str] = []
    i = 0
    while i < len(head):
        if head[i] == "--allow" and i + 1 < len(head):
            allow.extend(
                name.strip() for name in head[i + 1].split(",") if name.strip()
            )
            i += 2
            continue
        raise SystemExit(f"mcp_tool_gate: unknown argument {head[i]!r}")
    if not tail:
        raise SystemExit("mcp_tool_gate: empty upstream command")
    return set(allow), tail


def main() -> None:
    allow, upstream_argv = _parse_args(sys.argv[1:])
    upstream = subprocess.Popen(
        upstream_argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        env=None,  # 继承当前进程环境（含 MCP server 需要的凭据变量）
    )
    assert upstream.stdin is not None and upstream.stdout is not None

    # JSON-RPC id → 请求方法：响应回来才知道哪条是 tools/list。
    pending: dict[Any, str] = {}

    def _error(message: str) -> dict[str, Any]:
        return {"code": -32601, "message": message}

    def _downstream_to_upstream() -> None:
        for line in sys.stdin:
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            method = message.get("method")
            if method == "tools/call":
                name = str((message.get("params") or {}).get("name") or "")
                if name not in allow:
                    response = {
                        "jsonrpc": "2.0",
                        "id": message.get("id"),
                        "error": _error(f"tool {name!r} is not in the allowlist"),
                    }
                    sys.stdout.write(json.dumps(response) + "\n")
                    sys.stdout.flush()
                    continue
            if "id" in message and method:
                pending[message["id"]] = str(method)
            payload = line if line.endswith("\n") else line + "\n"
            upstream.stdin.write(payload.encode("utf-8"))
            upstream.stdin.flush()
        upstream.stdin.close()
        upstream.wait()
        sys.exit(upstream.returncode if upstream.returncode is not None else 0)

    import threading

    pump = threading.Thread(target=_downstream_to_upstream, daemon=True)
    pump.start()

    for line in upstream.stdout:
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            sys.stdout.write(line.decode("utf-8", "replace"))
            sys.stdout.flush()
            continue
        if message.get("id") is not None and pending.get(message["id"]) == "tools/list":
            result = message.get("result")
            if isinstance(result, dict) and isinstance(result.get("tools"), list):
                result["tools"] = [
                    tool
                    for tool in result["tools"]
                    if str((tool or {}).get("name")) in allow
                ]
            pending.pop(message["id"], None)
            line = (json.dumps(message, ensure_ascii=False) + "\n").encode()
        sys.stdout.write(line.decode("utf-8", "replace"))
        sys.stdout.flush()


if __name__ == "__main__":
    main()
