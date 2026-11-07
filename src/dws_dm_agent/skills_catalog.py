"""config 顶层 skills 池的解析与节点装配。

config 的 `skills` 键是 {name: path} 映射，path 指向 SKILL.md 或含
SKILL.md 的目录。agent profile 的 `options.skills` 列出要挂的名字，
各协议 adapter 把路径分流到自己的原生 skill 通道（pi --skill /
dsh patch 的 customSkillDirs / codex per-session home / claude
plugin-dir）。历史遗留的三个提示词路径变量（skill_humanizer 等，
service._prompt_variables 消费）继续从同一个池读，单一真相。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping


def resolve_skill_path(value: Any) -> Path | None:
    """把池里的一条声明解析成 SKILL.md 的绝对路径。

    支持直接指 SKILL.md、指目录（拼 SKILL.md）、不存在返回 None。
    """
    if not value:
        return None
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        return None
    if path.is_dir():
        path = path / "SKILL.md"
    return path if path.is_file() else None


def resolve_skills(
    pool: Mapping[str, Any] | None, requested: Any
) -> list[tuple[str, Path]]:
    """(name, SKILL.md 路径) 列表：profile options.skills 引用池。

    引用了池里不存在的名字或路径失效时抛错（fail loud：静默少挂
    一个 skill 比启动失败更难排查）。requested 为空返回空列表。
    """
    pool = pool if isinstance(pool, Mapping) else {}
    if not requested:
        return []
    names = [str(name) for name in requested]
    result: list[tuple[str, Path]] = []
    for name in names:
        if name not in pool:
            raise ValueError(
                f"profile references skill {name!r} not in config skills pool"
            )
        path = resolve_skill_path(pool[name])
        if path is None:
            raise ValueError(
                f"skill {name!r} path does not resolve to a SKILL.md: {pool[name]!r}"
            )
        result.append((name, path))
    return result


_FRONTMATTER_DESC = re.compile(r"^description:\s*(.+)$", re.MULTILINE)


def skill_description(path: Path) -> str:
    """SKILL.md frontmatter 的 description 一行（缺省空串）。"""
    try:
        head = path.read_text(encoding="utf-8")[:2000]
    except OSError:
        return ""
    match = _FRONTMATTER_DESC.search(head)
    return match.group(1).strip() if match else ""


def skills_prompt_block(skills: list[tuple[str, Path]]) -> str:
    """提示词 {{skills}} 变量：名字 + 一句话描述 + 路径，每行一条。"""
    if not skills:
        return ""
    lines = []
    for name, path in skills:
        desc = skill_description(path)
        lines.append(f"- {name}: {desc} ({path})" if desc else f"- {name} ({path})")
    return "\n".join(lines)


__all__ = [
    "resolve_skill_path",
    "resolve_skills",
    "skill_description",
    "skills_prompt_block",
]
