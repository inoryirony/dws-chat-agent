"""AgentAdapter 基类：协议能力的声明面与会话行为的分发面。

能力字段是配置校验与 prompt 装配的依据（不是运行时开关）：
- images："native"（runtime 编码后随首条输入送出）或 "embed"（service
  层把路径写进 prompt，agent 自己用工具读图）。
- native_steer：运行中能否注入补充消息。False 时 service 层的
  steer 策略应退化为重启续跑（restart_with_context 等价物）。
- service_tools：能否挂载 no_reply/task_record/task_lookup/report。
  引用这些工具的提示词把阶段锁死在具备该能力的协议上。
"""

from __future__ import annotations

from typing import Any, Mapping, Protocol, runtime_checkable

from ...core import sanitize_text

_IMAGE_NATIVE = "native"
_IMAGE_EMBED = "embed"


def image_mode(protocol: str) -> str:
    """协议的图片接入模式；未知协议按 embed 处理（最保守）。"""
    from .registry import ADAPTERS

    adapter = ADAPTERS.get(protocol)
    return adapter.images if adapter is not None else _IMAGE_EMBED


@runtime_checkable
class AgentAdapter(Protocol):
    """一个 CLI 协议的全部接入面。"""

    protocol: str
    images: str
    native_steer: bool
    service_tools: bool

    def command(
        self,
        profile: Any,
        mcp_config_path: Any = None,
        model_override: str | None = None,
        *,
        mount_escalate: bool = False,
    ) -> list[str]: ...

    def thread_options(self, profile: Any, workspace_root: Any) -> dict[str, Any]: ...

    def mcp_config(
        self, profile: Any, session_dir: Any, stage_name: str
    ) -> Any: ...

    async def start(self, session: Any) -> None: ...

    async def steer(self, session: Any, prompt: str) -> bool: ...

    async def abort(self, session: Any) -> None: ...

    async def parse_event(self, session: Any, event: Mapping[str, Any]) -> None: ...


def adapter_for(protocol: str) -> Any:
    """按协议取 adapter；未注册的协议抛错（fail-loud，不静默降级）。"""
    from .registry import ADAPTERS

    adapter = ADAPTERS.get(protocol)
    if adapter is None:
        raise ValueError(f"unsupported agent protocol: {protocol}")
    return adapter


__all__ = ["AgentAdapter", "adapter_for", "image_mode", "sanitize_text"]
