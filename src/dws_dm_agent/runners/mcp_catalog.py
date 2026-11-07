"""本机 MCP server 目录与按阶段生成的 session 级 mcp.json。

为什么需要这一层：
- pi 的原生 MCP 只认 `~/.pi/agent/mcp.json`（全局）和受信任项目的 `.pi/mcp.json`，
  没有 `--mcp-config`，无法按 agent profile 收窄；dws 是"多联系人 + 每阶段不同
  权限边界"，必须 session 隔离。
- 因此 MCP 走 `pi-mcp-adapter` 的 `--mcp-config <session>/mcp.json`，并配
  `PI_MCP_CONFIG_MODE=exclusive`，让 adapter 只认这一份，不串全局 server。

launch 参数（command/args）集中在本文件的目录里，不散落到 config.json；
profile 只需在 `options.mcp` 列出要挂的 server 名，或给单项加覆盖。
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Mapping


# 常用 MCP server 的启动方式。键名即 profile `options.mcp` 里引用的名字，
# 也是 mcp.json 里的 server 名（工具名前缀 mcp__<name>__）。
# 本机 launcher 路径与身份头全部经 ${VAR} 占位：真实值放 .env / 进程
# 环境（config.example.json 有样例），跨环境不改码。
CATALOG: dict[str, dict[str, Any]] = {
    "grafana-dev": {
        "command": "${DWS_MCP_GRAFANA_LAUNCHER}",
        "args": ["dev"],
    },
    "grafana-prod-logging": {
        "command": "${DWS_MCP_GRAFANA_LAUNCHER}",
        "args": ["prod-logging"],
    },
    "hsj-atlassian": {
        "command": "${DWS_MCP_HSJ_BINARY}",
    },
    "engram": {
        "type": "http",
        "url": "${DWS_ENGRAM_URL}",
        "headers": {"X-Engram-User": "${DWS_ENGRAM_USER}"},
    },
}

# 目录里不存在的名字也可以直接用完整定义（含 command/args/url/headers），
# 便于临时接入而不改代码。
_ALLOWED_KEYS = {
    "command",
    "args",
    "env",
    "url",
    "headers",
    "type",
    "auth",
    "protocolVersion",
    "description",
    "exposure",
    "toolExposure",
    "enabled",
    # adapter 专属
    "directTools",
    "excludeTools",
    "includeTools",
    "lifecycle",
    "idleTimeout",
    "startup_timeout_sec",
    "toolCallTimeoutMs",
}


def _normalize_entry(name: str, value: Any) -> dict[str, Any] | None:
    """把 profile 里的一条挂载声明展开成 mcp.json 的 server 定义。

    支持三种写法：
      "grafana-dev": True / None      → 直接用 CATALOG 定义
      "grafana-dev": {"directTools": [...]} → CATALOG 打底，覆盖给定字段
      "my-server": {"command": "...", "args": [...]} → 完整自定义定义
    """
    if value is False:
        return None
    catalog = CATALOG.get(name)
    if isinstance(value, Mapping):
        overrides = {
            str(key): item for key, item in value.items() if str(key) in _ALLOWED_KEYS
        }
        if catalog is None and "command" not in overrides and "url" not in overrides:
            raise ValueError(
                f"MCP server {name!r} is not in the catalog and has no command/url"
            )
        entry = {**catalog} if catalog is not None else {}
        entry.update(overrides)
        return entry
    if catalog is None:
        raise ValueError(f"unknown MCP server {name!r}; add it to CATALOG or pass a full definition")
    return dict(catalog)


def _expand(value: Any, env: Mapping[str, str]) -> Any:
    """递归展开 CATALOG/覆盖里的 ${VAR} 占位（env 缺失时保留原样，
    由 write_session_config 之后的消费方或运行时报错兜底）。"""
    if isinstance(value, str):
        return re.sub(
            r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}",
            lambda m: str(env.get(m.group(1), m.group(0))),
            value,
        )
    if isinstance(value, list):
        return [_expand(item, env) for item in value]
    if isinstance(value, dict):
        return {key: _expand(item, env) for key, item in value.items()}
    return value


def resolve_servers(
    declaration: Any, env: Mapping[str, str] | None = None
) -> dict[str, dict[str, Any]]:
    """把 profile 的 `options.mcp` 解析成 mcp.json 的 mcpServers 映射。

    declaration 可以是 list[str]（全部用目录定义）或
    Mapping[str, Any]（见 _normalize_entry）。env 提供 CATALOG 里
    ${VAR} 占位（launcher 路径、身份头）的真实值：缺省回退进程
    环境变量，再缺则占位原样透出（fail loud 在消费方）。
    """
    effective_env = dict(os.environ)
    if env:
        effective_env.update(env)
    if not declaration:
        return {}
    if isinstance(declaration, (list, tuple)):
        items: list[tuple[str, Any]] = [(str(name), True) for name in declaration]
    elif isinstance(declaration, Mapping):
        items = [(str(name), value) for name, value in declaration.items()]
    else:
        raise ValueError("profile options.mcp must be a list or an object")
    servers: dict[str, dict[str, Any]] = {}
    for name, value in items:
        entry = _normalize_entry(name, value)
        if entry is not None:
            servers[name] = _expand(entry, effective_env)
    return servers


def write_session_config(
    path: Path,
    *,
    servers: dict[str, dict[str, Any]],
    escalate_script: Path | None = None,
    auto_enable_codemode: bool = False,
) -> Path | None:
    """写出 session 级 mcp.json；没有 server 且没有 escalate 时不写文件。

    escalate_script 仅给 claude/codex 这类"前台靠外部 MCP 转交"的协议用；
    pi 的 escalate 是原生扩展（pi_escalate.ts），不在这里挂。
    """
    payload_servers = dict(servers)
    if escalate_script is not None:
        payload_servers["dws-escalate"] = {
            "type": "stdio",
            "command": sys.executable,
            "args": [str(escalate_script)],
        }
    if not payload_servers:
        return None
    body: dict[str, Any] = {"mcpServers": payload_servers}
    # codemode 会被 MCP 自动激活，在我们的场景里没必要：工具直接声明给模型，
    # 少一层"写 JS 才能调工具"的中间层。
    if not auto_enable_codemode:
        body["autoEnableCodemode"] = False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(body, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return path
