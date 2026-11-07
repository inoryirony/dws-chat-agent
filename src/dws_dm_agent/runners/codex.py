from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any

from . import mcp_catalog


def command(
    profile: Any,
    mount_escalate: bool = False,
    mcp_config_path: Path | None = None,
) -> list[str]:
    result = [*profile.command, "app-server", "--listen", "stdio://"]
    if mount_escalate:
        result.extend(
            [
                "-c",
                "mcp_servers.dws-escalate.command="
                + json.dumps(sys.executable),
                "-c",
                "mcp_servers.dws-escalate.args="
                + json.dumps([str(mcp_catalog.escalate_script_path())]),
            ]
        )
    if mcp_config_path is not None:
        # worker 的 options.mcp + dws-tools：codex 只认 config 语义的
        # mcp_servers.<name>.command/args（stdio），session mcp.json 逐键
        # 转 -c 注入；http/url 形态 codex 客户端不支持，跳过并告警，
        # 不注入未知键（-c 对不认识的嵌套键是硬错误）。
        # engram 是 http server：codex 渠道无等价挂载形态，能力缺口
        # 已知，等 codex 客户端支持 http MCP 后自动覆盖。
        servers = json.loads(
            Path(mcp_config_path).read_text(encoding="utf-8")
        ).get("mcpServers", {})
        for name, server in servers.items():
            if not isinstance(server, dict) or "command" not in server:
                logging.getLogger(__name__).warning(
                    "codex mcp injection skips non-stdio server %s", name
                )
                continue
            result.extend(
                [
                    "-c",
                    "mcp_servers.{0}.command={1}".format(
                        name, json.dumps(str(server["command"]))
                    ),
                    "-c",
                    "mcp_servers.{0}.args={1}".format(
                        name,
                        json.dumps([str(item) for item in server.get("args", [])]),
                    ),
                ]
            )
    result.extend(profile.extra_args)
    return result


def thread_options(profile: Any, workspace_root: Path) -> dict[str, Any]:
    options = profile.options
    sandbox = "read-only" if profile.read_only else str(
        options.get("sandbox", "danger-full-access")
    )
    config: dict[str, Any] = {}
    if options.get("web_search"):
        config["web_search"] = options["web_search"]
    if sandbox == "workspace-write":
        config["sandbox_workspace_write"] = {
            "network_access": bool(options.get("network_access", False)),
            "writable_roots": [
                str(Path(value).expanduser().resolve())
                for value in options.get("writable_roots", [])
            ],
        }
    result: dict[str, Any] = {
        "cwd": str(workspace_root),
        "ephemeral": True,
        "approvalPolicy": "never",
        "sandbox": sandbox,
    }
    if profile.model:
        result["model"] = profile.model
    if config:
        result["config"] = config
    return result
