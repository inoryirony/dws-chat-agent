from __future__ import annotations

import os
from pathlib import Path
from typing import Any


# 本服务自有工具：no_reply 控制结果 + 事项记忆读写 + report 进度/终态，
# front/worker 都挂载。
_DWS_TOOL_NAMES = ("no_reply", "task_record", "task_lookup", "report")


def _resolve_mcp_adapter(profile: Any, extensions: list[Any]) -> str:
    """找到声明了 --mcp-config 的 adapter 扩展。

    --mcp-config 是 pi-mcp-adapter 注册的 flag；没加载它时 pi 会直接报
    "Unknown option: --mcp-config" 退出。所以有 MCP 配置就必须能定位 adapter。
    优先级：profile options.mcp_adapter → 白名单里的 adapter → 环境变量默认值。
    """
    explicit = str(profile.options.get("mcp_adapter") or "").strip()
    if explicit:
        return explicit
    for extension in extensions:
        if "pi-mcp-adapter" in str(extension):
            return str(extension)
    default = os.environ.get("DWS_PI_MCP_ADAPTER", "").strip()
    if default:
        return default
    raise ValueError(
        "profile declares options.mcp but no MCP adapter is configured; "
        "add pi-mcp-adapter to options.extensions, set options.mcp_adapter, "
        "or export DWS_PI_MCP_ADAPTER"
    )


def command(
    profile: Any,
    mount_escalate: bool = False,
    *,
    model_override: str | None = None,
    mcp_config: Path | None = None,
) -> list[str]:
    result = [*profile.command, "--mode", "rpc", "--no-session"]
    model = model_override or profile.model
    if model:
        result.extend(["--model", model])
    if profile.reasoning_effort:
        result.extend(["--thinking", profile.reasoning_effort])
    # 扩展白名单：options.extensions 存在（含空数组）时禁用自动发现，
    # 只加载显式列出的扩展；键缺省时保持 pi 默认的发现行为。
    # 空数组是有意义的取值：前台要"一个扩展都不装"，不能被当成缺省。
    declared = profile.options.get("extensions")
    extensions = list(declared or [])
    if mcp_config is not None:
        # --mcp-config 只存在于 pi-mcp-adapter；显式确保它在白名单里，
        # 否则下面的 --mcp-config 会被 pi 当成未知参数拒绝启动。
        adapter = _resolve_mcp_adapter(profile, extensions)
        if not any(str(item) == adapter for item in extensions):
            extensions.append(adapter)
    if declared is not None or extensions:
        result.append("--no-extensions")
        for extension in extensions:
            result.extend(["--extension", str(extension)])
    # MCP 配置走 adapter 的 --mcp-config；配合 PI_MCP_CONFIG_MODE=exclusive
    # 只认这一份，不会串到 ~/.config/mcp 或 ~/.pi/agent 的全局 server。
    if mcp_config is not None:
        result.extend(["--mcp-config", str(mcp_config)])
    # 服务工具扩展始终加载（no_reply/task_record/task_lookup），不受白名单影响。
    result.extend(["--extension", str(Path(__file__).with_name("pi_dws_tools.ts"))])
    if mount_escalate:
        result.extend(["--extension", str(Path(__file__).with_name("pi_escalate.ts"))])
    configured_tools = profile.options.get(
        "tools", "read,grep,find,ls" if profile.read_only else ""
    )
    if configured_tools is not None:
        tool_names = [
            name.strip() for name in str(configured_tools).split(",") if name.strip()
        ]
        if mount_escalate and "escalate" not in tool_names:
            tool_names.append("escalate")
        if tool_names:
            # 只在已经显式限制工具集时追加服务工具；未限制时保持全量工具，
            # 否则 worker 会被误收窄成只有这三个工具。
            for name in _DWS_TOOL_NAMES:
                if name not in tool_names:
                    tool_names.append(name)
            result.extend(["--tools", ",".join(tool_names)])
        elif profile.read_only:
            result.append("--no-tools")
    if profile.options.get("provider"):
        result.extend(["--provider", str(profile.options["provider"])])
    result.extend(profile.extra_args)
    return result
