from __future__ import annotations

import re
from typing import Any, Mapping


_PLACEHOLDER = re.compile(r"\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}")

STAGE_PROMPT_VARIABLES = frozenset(
    {
        "self_name", "agent_name", "worker_name", "contact_name",
        "contact_alias", "contact_user_id", "contact_open_dingtalk_id",
        "session_id", "prior_attempt",
        "workspace_root", "worktree_root", "recent_context_json",
        "current_messages_json", "active_tasks_json", "now",
        # 技能与交付路径由 config 注入，提示词文件里不再硬编码绝对路径。
        "skill_humanizer", "skill_answer_html", "script_am",
        "deliveries_dir",
    }
)
SUPPLEMENT_PROMPT_VARIABLES = frozenset(
    {
        "supplement_messages_json", "session_id", "contact_name",
        "contact_alias", "self_name",
    }
)

# worker 阶段的公共段文件名（身份/信任等级/表达与输出契约），与阶段
# 模板同目录；服务渲染 worker 时把它拼在阶段模板之前。
SHARED_PROMPT_NAME = "shared.md"


def render_prompt(template: str, variables: Mapping[str, Any]) -> str:
    missing = sorted(set(_PLACEHOLDER.findall(template)) - set(variables))
    if missing:
        raise ValueError(f"missing prompt variables: {', '.join(missing)}")
    return _PLACEHOLDER.sub(lambda match: str(variables[match.group(1)]), template)


def assemble_stage_prompt(shared: str, stage: str) -> str:
    """把渲染好的 shared 段拼在阶段模板前。

    两段入参都已渲染完成，这里只做拼接、不再二次替换：联系人消息里
    出现的 {{...}} 字样不会因此被展开。
    """
    if not shared.strip():
        return stage
    return f"{shared}\n\n{stage}"


def validate_template_variables(template: str, allowed: frozenset[str]) -> None:
    unknown = sorted(set(_PLACEHOLDER.findall(template)) - allowed)
    if unknown:
        raise ValueError(f"unsupported prompt variables: {', '.join(unknown)}")
