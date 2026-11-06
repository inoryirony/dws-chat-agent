"""Per-protocol agent adapters.

每个 CLI 协议一个 adapter：把 runtime.py 里散落的四组 protocol 分叉
（start 握手 / steer / abort / 事件解析）加上命令构造、thread_options、
MCP 配置生成集中到一个类里。runtime.AgentSession 只保留进程与读写
等通用管线，协议行为全部查 `_ADAPTERS` 注册表分发。

adapter 实例必须无状态：单例被所有并发会话共享，任何会话私有状态
（如 pi 的 thinking 索引、dsh 的 callId→工具名映射）一律挂在
session._protocol_state 上。
"""

from .base import AgentAdapter, adapter_for, image_mode
from .claude import ClaudeAdapter
from .codex import CodexAdapter
from .custom import CustomAdapter
from .dsh import DshAdapter
from .pi import PiAdapter
from .registry import (
    ADAPTERS,
    DRIVER_PROTOCOLS,
    SERVICE_TOOL_PROTOCOL,
    SUPPORTED_PROTOCOLS,
    default_driver,
)

__all__ = [
    "ADAPTERS",
    "DRIVER_PROTOCOLS",
    "SERVICE_TOOL_PROTOCOL",
    "SUPPORTED_PROTOCOLS",
    "AgentAdapter",
    "ClaudeAdapter",
    "CodexAdapter",
    "CustomAdapter",
    "DshAdapter",
    "PiAdapter",
    "adapter_for",
    "default_driver",
    "image_mode",
]
