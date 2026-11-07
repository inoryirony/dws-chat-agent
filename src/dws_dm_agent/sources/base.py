"""InboundSource：触发源协议（蓝图步 7/8）。

一个源回答一个问题：外部世界发生了什么变化。实现产出一批
`_SourceItem`（稳定 dedup key + 内容 + 携带的 Contact 上下文），
service 包装轮询循环：claim_event 幂等去重 → 匹配 Watch 规则 →
建 task + 建会话事件。与 _listen_contact 的 dws 长轮询平级，互不
感知；新源 = 实现一个类 + 注册表一行 + config 加一段。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Protocol, runtime_checkable

from ..core import Contact


@dataclass(frozen=True)
class SourceItem:
    """一次外部变化的标准化描述。

    dedup_key 全局稳定（`jira:PROJ-123` / `gitlab:group!1!42`），直接当
    events 表的 message_id（UNIQUE 幂等靠它）；conversation_id 决定
    投递通道（前缀查 Channel 注册表）；contact 是该会话的投递身份
    （外部源用虚拟 Contact，alias 即 issue key / project!mr）。
    """

    dedup_key: str
    conversation_id: str
    contact: Contact
    title: str
    body: str
    url: str = ""
    created_at: datetime | None = None
    meta: Mapping[str, Any] | None = None


@runtime_checkable
class InboundSource(Protocol):
    name: str

    async def poll(self, since: datetime) -> list[SourceItem]:
        """返回 since 之后的新对象；空列表 = 本轮无事。异常由 service
        的轮询循环吞掉记日志（照 _consume_wakes 的容错骨架）。"""
        ...


__all__ = ["InboundSource", "SourceItem"]
