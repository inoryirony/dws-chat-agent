"""协议 → adapter 单点注册表。

加一个新 CLI 接入 = 在这里注册一行 + runners/ 下放命令构造模块。
runtime / service / dashboard 全部从这里读协议清单，不再各自维护
字符串分叉。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .claude import ClaudeAdapter
from .codex import CodexAdapter
from .custom import CustomAdapter
from .dsh import DshAdapter
from .pi import PiAdapter

ADAPTERS: dict[str, Any] = {
    adapter.protocol: adapter
    for adapter in (
        CodexAdapter(),
        ClaudeAdapter(),
        CustomAdapter(),
        PiAdapter(),
        DshAdapter(),
    )
}

# driver 名 → 默认协议。from_config 用它补全只写了 driver 的 profile；
# 反查（协议 → driver）走 _protocol_driver，"custom" 兜底。
DRIVER_PROTOCOLS: dict[str, str] = {
    "codex": "codex-app-server",
    "claude": "claude-stream-json",
    "pi": "pi-rpc",
    "dsh": "dsh-headless-jsonl",
}

_PROTOCOL_DRIVER = {protocol: driver for driver, protocol in DRIVER_PROTOCOLS.items()}


def default_driver(protocol: str) -> str:
    return _PROTOCOL_DRIVER.get(protocol, "custom")


SUPPORTED_PROTOCOLS = tuple(sorted(ADAPTERS))

# 服务工具（no_reply/task_record/task_lookup/report）目前只在 pi-rpc 挂载。
# 有新协议具备等价挂载能力时，改这里为能力查询（adapter.service_tools）；
# 在那之前保持单一协议锁死，配置校验按这份名单报错。
SERVICE_TOOL_PROTOCOL = "pi-rpc"

__all__ = [
    "ADAPTERS",
    "DRIVER_PROTOCOLS",
    "SERVICE_TOOL_PROTOCOL",
    "SUPPORTED_PROTOCOLS",
    "default_driver",
    "Path",
]
