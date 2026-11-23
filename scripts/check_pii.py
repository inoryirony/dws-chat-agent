#!/usr/bin/env python3
"""隐私数据入库 lint：模式规则 + 本地真名名单 + 禁跟踪文件断言。

背景（2026-11-23 事故）：真通讯录/轨迹/人名曾进入公开仓历史，
全史改写清除后落地本检查防复发。三层防护：

1. 通用模式（不含真名，可进公开仓）：手机号、15-18 位平台 ID、
   open_dingtalk_id 串、/Users/ 个人路径、连续 unicode 中文转义
   （真通讯录 JSON 的特征形态）。
2. 本地真名名单 scripts/pii-names.local.txt（gitignore，永不入库）：
   真名与别名 ID。文件缺失时跳过（其他 clone / CI 场景）。
3. 禁跟踪断言：.gitignore 对已跟踪文件无效（config.json 当年
   进仓的根因），故显式断言 git ls-files 不含禁入路径。

豁免：行内标记 `pii-allow` 跳过该行（假 fixture 需要形似真数据时用）。

用法：
  check_pii.py --staged   # pre-commit hook：扫暂存区
  check_pii.py --all      # 扫全部跟踪文件（CI / 手动巡检）
  check_pii.py PATH...    # 扫指定文件
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# (规则名, 正则, 说明)
PATTERNS = [
    ("phone", r"\b1[3-9]\d{9}\b", "手机号样式 11 位数字"),
    ("platform-id", r"\b\d{15,18}\b", "15-18 位平台用户 ID（钉钉 user_id 形态）"),
    ("open-id", r"\bD[A-Za-z0-9]{28,40}\b", "open_dingtalk_id 串形态"),
    ("personal-path", r"/Users/[^/\s]+/", "个人绝对路径（/Users/<user>/）"),  # pii-allow 自述示例
    ("unicode-cjk", r"(\\u[4-9a-fA-F][0-9a-fA-F]{3}){4,}",
     "连续 unicode 中文转义（真通讯录 JSON 特征）"),
]

# 禁止进入 git 跟踪的路径（前缀匹配；.gitignore 对已跟踪文件无效，
# 这条断言才是根因防护）。全部以 ROOT 相对路径表示。
FORBIDDEN_TRACKED = [
    "config.json",
    ".env",
    "state/",
    ".zcode/",
    "verify-evidence/",
    "scripts/pii-names.local.txt",
]

ALLOW_MARK = "pii-allow"
NAMES_FILE = ROOT / "scripts" / "pii-names.local.txt"


def load_names() -> list[str]:
    """本地真名名单；缺失返回空（其他 clone 无名单是预期状态）。"""
    if not NAMES_FILE.is_file():
        return []
    return [
        line.strip()
        for line in NAMES_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]


def scan_text(path: str, text: str, names: list[str]) -> list[str]:
    """返回命中描述列表；输出脱敏，不回显命中内容。"""
    hits: list[str] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if ALLOW_MARK in line:
            continue
        for name, pattern, desc in PATTERNS:
            if re.search(pattern, line):
                hits.append(f"{path}:{lineno} [{name}] {desc}（行尾加 {ALLOW_MARK} 可豁免）")
                break
        else:
            for term in names:
                if term in line:
                    hits.append(
                        f"{path}:{lineno} [local-name] 命中本地名单「{term}」"
                        f"（行尾加 {ALLOW_MARK} 可豁免）"
                    )
                    break
    return hits


def scan_paths(paths: list[str]) -> list[str]:
    names = load_names()
    hits: list[str] = []
    for path in paths:
        file = ROOT / path
        if not file.is_file():
            continue
        text = file.read_text(encoding="utf-8", errors="ignore")
        hits.extend(scan_text(path, text, names))
    return hits


def staged_paths() -> list[str]:
    out = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout
    return [line for line in out.splitlines() if line]


def all_tracked_paths() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout
    return out.splitlines()


def forbidden_tracked(paths: list[str]) -> list[str]:
    """断言 git 跟踪集合与禁入路径不相交；条目以 / 结尾按前缀匹配，否则精确。"""
    bad = []
    for path in paths:
        for forbidden in FORBIDDEN_TRACKED:
            if forbidden.endswith("/"):
                if path.startswith(forbidden):
                    bad.append(path)
            elif path == forbidden:
                bad.append(path)
    return sorted(set(bad))


def main(argv: list[str]) -> int:
    if len(argv) >= 1 and argv[0] == "--staged":
        paths = staged_paths()
    elif len(argv) >= 1 and argv[0] == "--all":
        paths = all_tracked_paths()
    elif argv:
        paths = argv
    else:
        print(__doc__)
        return 2

    hits = scan_paths(paths)

    # 禁跟踪断言：staged 模式查暂存集合，--all/显式路径查全部跟踪文件。
    scope = staged_paths() if argv and argv[0] == "--staged" else all_tracked_paths()
    bad = forbidden_tracked(scope)
    for path in bad:
        hits.insert(0, f"{path} [forbidden-tracked] 禁入文件被 git 跟踪（git rm --cached <path>）")

    if hits:
        print(f"PII lint: {len(hits)} 处命中", file=sys.stderr)
        for hit in hits:
            print(f"  {hit}", file=sys.stderr)
        print("真名/ID/个人路径不得入库；假数据请用明显占位（open-alice 等）。", file=sys.stderr)
        return 1
    if not NAMES_FILE.is_file():
        print("note: 本地名单 scripts/pii-names.local.txt 不存在，真名规则未启用", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
