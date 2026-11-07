#!/usr/bin/env python3
"""隔离实例启动器：verify-dws-chat-agent skill 的 Launch helper。

渲染最小可跑 config（假联系人、shadow 模式、独立 state_dir），
起 AgentService 并打印就绪行。仅用于验证，绝不接生产 state_dir。

用法:
  python3 scripts/verify_launch.py --repo <repo-root> --state-dir <dir> [--run-id <id>]
stdout 就绪行:  READY <port> <token> <pid> <config-path>
SIGINT/SIGTERM 优雅停。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="launch isolated dws-chat-agent for verification")
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--run-id", default="manual")
    args = parser.parse_args()

    args.repo = args.repo.resolve()
    args.state_dir = args.state_dir.resolve()
    args.state_dir.mkdir(parents=True, exist_ok=True)
    workspace = args.state_dir / "workspace"
    worktree = args.state_dir / "worktrees"
    workspace.mkdir(exist_ok=True)
    worktree.mkdir(exist_ok=True)

    config_path = args.state_dir / "verify-config.json"
    config = {
        "version": 3,
        "mode": "shadow",
        "timezone": "Asia/Shanghai",
        "self": {"name": "verify-self", "open_dingtalk_id": "VERIFY_SELF_OPEN_ID"},
        "contacts": [
            {
                "alias": "verify-contact",
                "display_name": "验证联系人",
                "user_id": "VERIFY_USER_ID",
                "open_dingtalk_id": "VERIFY_CONTACT_OPEN_ID",
            }
        ],
        "quiet_window_seconds": 1,
        "human_cooldown_seconds": 0,
        "workspace_root": str(workspace),
        "worktree_root": str(worktree),
        "state_dir": str(args.state_dir),
        "task_memory": {"inactive_days": 30},
        "agents": {
            "verify-worker": {
                "driver": "pi",
                "protocol": "pi-rpc",
                "command": ["pi"],
                "model": os.environ.get("DWS_VERIFY_MODEL", "ccr-internal/deepseek-flash"),
                "reasoning_effort": "high",
                "read_only": False,
                "timeout_seconds": 600,
                "options": {"extensions": []},
            },
            "verify-worker-dsh": {
                "driver": "dsh",
                "protocol": "dsh-headless-jsonl",
                "command": ["dsh"],
                "model": "",
                "reasoning_effort": "high",
                "read_only": False,
                "timeout_seconds": 600,
                "options": {
                    "dsh_profile": "headless",
                    "dsh_patches": [
                        str(args.repo / "deploy" / "dsh" / "dws-worker.patch.yml")
                    ],
                    "task_cli": True,
                },
            },
        },
        "workflows": {
            "active": os.environ.get("DWS_VERIFY_RUNTIME", "verify-default"),
            "presets": {
                "verify-default": {
                    "supplement_strategy": "steer",
                    "supplement_prompt": str(args.repo / "prompts" / "supplement.md"),
                    "auto_messages": {"ack_enabled": False},
                    "worker": {
                        "label": "验证 Worker",
                        "agent": "verify-worker",
                        "prompt": str(args.repo / "prompts" / "worker.md"),
                    },
                },
                "verify-dsh": {
                    "supplement_strategy": "restart_with_context",
                    "supplement_prompt": str(args.repo / "prompts" / "supplement-dsh.md"),
                    "auto_messages": {"ack_enabled": False},
                    "worker": {
                        "label": "验证 Worker（dsh）",
                        "agent": "verify-worker-dsh",
                        "prompt": str(args.repo / "prompts" / "worker-dsh.md"),
                    },
                },
            },
        },
        "skills": {},
        "dws": {"binary": "dws", "profile": "verify", "ai_tag": False, "streaming_cards": False},
        "security": {"max_message_chars": 12000},
    }
    # 透传生产 config 的 watches 段（存在时）+ 缩短轮询间隔，E2E 验证
    # watch 命中链路：首轮立即扫一次，无需等 300s。
    source_config = args.repo / "config.json"
    if source_config.is_file():
        source = json.loads(source_config.read_text(encoding="utf-8"))
        if isinstance(source.get("watches"), list) and os.environ.get(
            "DWS_VERIFY_WATCHES"
        ) != "0":
            config["watches"] = source["watches"]
            config["watch_interval_seconds"] = 30
            config["watch_lookback_hours"] = int(
                os.environ.get("DWS_VERIFY_WATCH_LOOKBACK_HOURS", 24)
            )
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")

    sys.path.insert(0, str(args.repo / "src"))
    from dws_dm_agent.config import load_settings  # noqa: E402
    from dws_dm_agent.service import AgentService  # noqa: E402
    import dws_dm_agent.service as _svc  # noqa: E402

    if os.environ.get("DWS_VERIFY_WAKE_WINDOW_24H"):
        # 验证用：wake 派发窗拉通全天，否则夜里跑 E2E 时到点 wake 全部
        # 顺延到次日 09:00，等不到真模型唤醒。只改本进程内存，不碰生产。
        _svc._WAKE_WINDOW_OPEN = _svc.time(0, 0)
        _svc._WAKE_WINDOW_CLOSE = _svc.time(23, 59, 59)

    settings = load_settings(config_path, args.state_dir / "verify.env")
    # verify 直构 AgentService 不走 main() 的 configure_logging：root
    # logger 默认 WARNING 会吞掉 INFO——E2E 要靠 "watch dispatcher
    # ready/hit" 的 INFO 行取证，这里补基础配置。
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    # watch 源（JiraSource/GitLabSource）直接读 os.environ；把 repo .env
    # 的凭据键带上（只注入验证需要的源键，不覆盖已设值）。
    repo_env = args.repo / ".env"
    if repo_env.is_file():
        for line in repo_env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                if key.startswith(("JIRA_", "GITLAB_")):
                    os.environ.setdefault(key, value)
    os.environ["DWS_CHAT_AGENT_DASHBOARD_PORT"] = "0"
    service = AgentService(settings, "shadow")
    token = service.dashboard.operator_token

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    run_task = loop.create_task(service.run())
    printed = {"ready": False}

    def _maybe_print_ready() -> None:
        if printed["ready"]:
            return
        server = getattr(service.dashboard, "server", None)
        if server is not None:
            printed["ready"] = True
            print(
                f"READY {server.server_port} {token} {os.getpid()} {config_path}",
                flush=True,
            )

    def _stop(*_: object) -> None:
        loop.call_soon_threadsafe(service.request_stop)

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    try:
        deadline = loop.time() + 60
        while not printed["ready"] and loop.time() < deadline and not run_task.done():
            _maybe_print_ready()
            loop.run_until_complete(asyncio.sleep(0.2))
        if not printed["ready"]:
            print("FAILED: dashboard did not start within timeout", file=sys.stderr)
            return 1
        loop.run_until_complete(run_task)
    finally:
        service.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
