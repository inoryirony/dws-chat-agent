"""workflow 节点的 kind 注册表：stage 槽位名 → 能力声明。

奥卡姆 D1 修订版的骨架层：机制骨架（漏斗/Outcome/账本）不认识具体
stage 名，只认识 kind；编排空间（preset 的 stage 槽位 + prompt）决定
哪个名字落到哪个 kind。front/worker 是惯例槽位名，不是内置常量：

- triage（惯例名 front）：只读分诊节点。read_only、挂 escalate、
  无 worktree、codex 走隔离 home。
- executive（惯例名 worker）：执行节点。非只读、可领 worktree、
  拼 shared.md 身份段、承接 fallback 链。

新节点类型 = register_stage 一行（可覆盖个别能力位）+ service 侧补
对应处理方法；runtime 侧的 escalate/worktree/隔离 home 判定全部查
本表，不出现 stage 名字符串比较。
"""

from __future__ import annotations

from dataclasses import dataclass, replace


@dataclass(frozen=True)
class StageSpec:
    kind: str
    read_only: bool
    mount_escalate: bool
    use_worktree: bool
    # executive 专属：shared.md 身份段拼接 + 作为 fallback 链锚点。
    executive: bool = False


KIND_SPECS: dict[str, StageSpec] = {
    "triage": StageSpec(
        kind="triage",
        read_only=True,
        mount_escalate=True,
        use_worktree=False,
    ),
    "executive": StageSpec(
        kind="executive",
        read_only=False,
        mount_escalate=False,
        use_worktree=True,
        executive=True,
    ),
}

# 槽位名 → kind。front 是 triage 的惯例名，worker 是 executive 的；
# preset 里写其他名字默认按 executive 处理（要当 triage 用就显式注册）。
_SLOT_KINDS: dict[str, str] = {
    "front": "triage",
    "worker": "executive",
}

# 槽位级能力覆盖：只影响该槽位，不动 kind 基座。
_SLOT_OVERRIDES: dict[str, StageSpec] = {}


def register_stage(name: str, kind: str, **overrides: bool) -> None:
    """把自定义槽位名注册到某个 kind（可覆盖个别能力位）。"""
    base = KIND_SPECS.get(kind)
    if base is None:
        raise ValueError(f"unknown stage kind {kind!r}; known: {sorted(KIND_SPECS)}")
    _SLOT_KINDS[name] = kind
    if overrides:
        _SLOT_OVERRIDES[name] = replace(base, **overrides)


def stage_spec(stage_name: str) -> StageSpec:
    override = _SLOT_OVERRIDES.get(stage_name)
    if override is not None:
        return override
    kind = _SLOT_KINDS.get(stage_name)
    return KIND_SPECS[kind] if kind is not None else KIND_SPECS["executive"]


def is_executive(stage_name: str) -> bool:
    return stage_spec(stage_name).executive


__all__ = [
    "KIND_SPECS",
    "StageSpec",
    "is_executive",
    "register_stage",
    "stage_spec",
]
