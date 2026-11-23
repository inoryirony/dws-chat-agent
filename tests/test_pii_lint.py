"""check_pii.py 的单元测试：纯函数层，不依赖 git 状态。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import check_pii  # noqa: E402


def test_pattern_hits():
    cases = {
        "phone": "联系 13812345678 确认",  # pii-allow lint 测试样例
        "platform-id": "user_id: 16363377995367788",  # pii-allow lint 测试样例
        "open-id": "open_dingtalk_id=DAabViSQS2iSOzGURWPSzLMEIFdljUSs8qh",  # pii-allow lint 测试样例
        "personal-path": "读取 /Users/someone/work/x.md",  # pii-allow lint 测试样例
        "unicode-cjk": r'name": "\u6708\u7f28\u9ec4\u68a6\u7f18\u5b50"',  # pii-allow lint 测试样例
    }
    for rule, line in cases.items():
        hits = check_pii.scan_text("f.txt", line, names=[])
        assert hits and f"[{rule}]" in hits[0], f"{rule} 未命中: {line}"


def test_local_name_hit():
    hits = check_pii.scan_text("f.txt", "通知：某人已处理", names=["某人"])
    assert hits and "[local-name]" in hits[0]


def test_allow_marker_skips_line():
    assert check_pii.scan_text("f.txt", "13812345678  # pii-allow", names=[]) == []


def test_clean_line_passes():
    assert check_pii.scan_text("f.txt", "正常中文与 open-alice 假数据", names=["马天泽"]) == []  # pii-allow lint 测试样例


def test_forbidden_tracked_prefix_and_exact():
    paths = ["state/agent.log", "config.json", "src/dws_dm_agent/core.py",
             ".zcode/plans/x.md", "scripts/pii-names.local.txt"]
    bad = check_pii.forbidden_tracked(paths)
    assert bad == [".zcode/plans/x.md", "config.json", "scripts/pii-names.local.txt",
                   "state/agent.log"]
    # .env 是精确匹配，不得误伤 .env.example（gitignore 白名单文件）
    assert check_pii.forbidden_tracked([".env.example"]) == []
    assert check_pii.forbidden_tracked([".env"]) == [".env"]


def test_name_list_missing_is_ok(monkeypatch):
    monkeypatch.setattr(check_pii, "NAMES_FILE", Path("/nonexistent/pii.txt"))
    assert check_pii.load_names() == []
