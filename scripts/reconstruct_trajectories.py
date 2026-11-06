#!/usr/bin/env python3
"""从审计库 + git 证据重建 pi worker 运行轨迹。

真实轨迹（prompt.md / events.jsonl / stderr.log）在成功运行后被
service.py 的 rmtree 删掉，pi 又以 --no-session 启动，没有原生 session 文件。
本脚本用四类持久证据拼出可核对的重建版：

  1. audit.sqlite3: runs / events / outgoing / progress / facts / tasks /
     task_changes / board_runs
  2. prompts/*.md + config.json/.env：按真实渲染路径重放 prompt 骨架
  3. git 仓库：运行时间窗内实际产生的提交
  4. state/agent.log

不可恢复项在输出里显式标注：模型内部思考、每次 bash/read 调用、
中间 assistant 正文、完整 prompt（recent_context 来自钉钉实时历史，
未落库）。

用法:
    python3 scripts/reconstruct_trajectories.py 2026-11-06
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from dws_dm_agent.prompts import assemble_stage_prompt, render_prompt  # noqa: E402

STATUS_ACTIVE = ("open", "waiting")


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            env[key] = value
    return env


def is_synthetic(message_id: str) -> bool:
    return message_id.startswith("wake-") or message_id.startswith("panel-")


def facts_for_task(db: sqlite3.Connection, task_id: str, until: str) -> list[dict]:
    rows = db.execute(
        "select id, task_id, text, anchor_message_id, source, created_at "
        "from facts where task_id = ? and created_at <= ? order by id",
        (task_id, until),
    ).fetchall()
    return [dict(row) for row in rows]


def task_snapshot(db: sqlite3.Connection, contact_id: str, until: str) -> str:
    """重建运行开始时刻的 active_tasks_json（_active_tasks_snapshot 同构）。

    tasks 表只保留最终态；用 created_at <= until 且当时未归档近似。
    """
    rows = db.execute(
        """
        select * from tasks
        where scope_contact = ? and created_at <= ?
          and (archived_at = '' or archived_at > ?)
          and status in ('open','waiting')
        order by last_evidence_at desc limit 8
        """,
        (contact_id, until, until),
    ).fetchall()
    tasks = []
    for row in rows:
        item = {
            "id": row["id"],
            "contact_id": row["scope_contact"],
            "goal": row["goal"],
            "status": row["status"],
            "waiting_on": row["waiting_on"],
            "resources": json.loads(row["resources"] or "[]"),
            "contact_ids": json.loads(row["contact_ids"] or "[]"),
            "evidence_summary": row["evidence_summary"],
            "evidence_message_ids": json.loads(row["evidence_message_ids"] or "[]"),
            "last_evidence_at": row["last_evidence_at"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "archived_at": row["archived_at"] or "",
            "archive_reason": row["archive_reason"],
            "version": row["version"],
        }
        facts = facts_for_task(db, row["id"], until)[-3:]
        item["recent_facts"] = [
            {
                "id": f["id"],
                "task_id": f["task_id"],
                "text": f["text"],
                "anchor_message_id": f["anchor_message_id"],
                "source": f["source"],
                "created_at": f["created_at"],
            }
            for f in facts
        ]
        tasks.append(item)
    return json.dumps(tasks, ensure_ascii=False, indent=2)


def recent_context(db: sqlite3.Connection, contact_name: str, self_name: str,
                   contact_id: str, conversation_id: str,
                   until: str, exclude: set[str],
                   limit: int = 100, budget: int = 12000) -> tuple[list[dict], bool]:
    """重建 recent_context（与 _recent_context 同构 + 同预算裁剪）。

    钉钉侧人工消息与服务自己的完整发送正文不落库，只能拼审计库可见部分。
    """
    messages: list[dict] = []
    for row in db.execute(
        "select message_id, conversation_id, received_at, request_preview "
        "from events where conversation_id = ? and received_at <= ? "
        "order by received_at",
        (conversation_id, until),
    ):
        if row["message_id"] in exclude or is_synthetic(row["message_id"]):
            continue
        messages.append({
            "role": "contact",
            "sender": contact_name,
            "execution": "",
            "time": row["received_at"],
            "message_id": row["message_id"],
            "conversation_id": row["conversation_id"],
            "text": row["request_preview"],
        })
    for row in db.execute(
        "select o.session_id, o.conversation_id, o.sent_at, o.event_ids_json, r.handled "
        "from outgoing o left join runs r on r.session_id = o.session_id "
        "where o.conversation_id = ? and o.sent_at <= ? "
        "and o.source = 'agent' and o.event_ids_json != '[]' order by o.sent_at",
        (conversation_id, until),
    ):
        ids = json.loads(row["event_ids_json"] or "[]")
        messages.append({
            "role": "agent",
            "sender": self_name,
            "execution": row["session_id"],
            "time": row["sent_at"],
            "message_id": ids[0] if ids else "",
            "conversation_id": row["conversation_id"],
            # 真实正文只留在 runs.handled（已被 sanitize 截到 1000 字符）。
            "text": row["handled"] or "<正文未落库>",
        })
    messages.sort(key=lambda item: item["time"])
    context_limit = min(max(limit, 0), 100)
    recent = messages[-context_limit:] if context_limit > 0 else []
    if budget > 0:
        total = sum(len(item["text"]) for item in recent)
        drop = 0
        while drop < len(recent) and total > budget:
            total -= len(recent[drop]["text"])
            drop += 1
        if drop:
            recent = recent[drop:]
    return recent, True


def select_batch(db: sqlite3.Connection, run: sqlite3.Row) -> list[sqlite3.Row]:
    """用 runs.request_preview 反查本轮输入消息。

    events.received_at 早于 runs.started_at（入队到起跑有延迟），不能用简单
    时间窗；request_preview 是本轮所有输入按行拼接后再截断 500 字符，用它
    做前缀匹配最稳。
    """
    preview = run["request_preview"] or ""
    candidates = list(db.execute(
        "select * from events where conversation_id = ? "
        "and received_at <= ? order by received_at desc limit 20",
        (run["conversation_id"], run["finished_at"]),
    ))
    candidates.reverse()
    chosen: list[sqlite3.Row] = []
    for row in candidates:
        text = row["request_preview"] or ""
        if not text:
            continue
        if preview.startswith(text) or text in preview:
            chosen.append(row)
        elif chosen:
            break
    return chosen


def git_commits(env: dict[str, str], start: str, end: str) -> list[dict]:
    import subprocess

    root = Path(env["DWS_CHAT_AGENT_WORKSPACE_ROOT"])
    results = []
    for repo in sorted(root.glob("*")) + sorted((root / "devops").glob("*")):
        if not (repo / ".git").exists():
            continue
        out = subprocess.run(
            ["git", "-C", str(repo), "log", "--all", "--no-merges",
             f"--since={start}", f"--until={end}",
             "--format=%aI|%h|%an|%s"],
            capture_output=True, text=True,
        ).stdout.strip()
        for line in out.splitlines():
            when, sha, author, subject = line.split("|", 3)
            results.append({
                "repo": repo.name,
                "when": when,
                "sha": sha,
                "author": author,
                "subject": subject or "(空提交消息，git-ai 检查点)",
            })
    return results


def main() -> int:
    day = sys.argv[1] if len(sys.argv) > 1 else "2026-11-06"
    env = load_env()
    config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    contacts = {c["user_id"]: c for c in json.loads(env["DWS_CHAT_AGENT_CONTACTS_JSON"])}
    skills = config.get("skills") or {}
    db = sqlite3.connect(ROOT / "state" / "audit.sqlite3")
    db.row_factory = sqlite3.Row

    out_dir = ROOT / "state" / "reconstructed" / day
    out_dir.mkdir(parents=True, exist_ok=True)

    runs = list(db.execute(
        "select * from runs where started_at like ? order by started_at", (f"{day}%",)
    ))
    index = []
    for run in runs:
        sid = run["session_id"]
        started, finished = run["started_at"], run["finished_at"]
        contact = contacts.get(run["contact_user_id"], {})
        batch = select_batch(db, run)
        message_ids = {row["message_id"] for row in batch}
        context, context_partial = recent_context(
            db,
            contact.get("display_name", ""),
            env["DWS_CHAT_AGENT_SELF_NAME"],
            run["contact_user_id"],
            run["conversation_id"],
            started,
            message_ids,
            limit=int(config.get("recent_context_messages", 100)),
            budget=int(config.get("recent_context_chars", 12000)),
        )
        current_messages = [
            {
                "time": row["received_at"],
                "sender": contact.get("display_name", ""),
                "role": "contact",
                "conversation_id": row["conversation_id"],
                "message_id": row["message_id"],
                "text": row["request_preview"],
            }
            for row in batch if not is_synthetic(row["message_id"])
        ]
        variables = {
            "self_name": env["DWS_CHAT_AGENT_SELF_NAME"],
            "agent_name": "pi-worker-deepseek",
            "worker_name": "pi-worker-deepseek",
            "contact_name": contact.get("display_name", ""),
            "contact_alias": contact.get("alias", ""),
            "contact_user_id": run["contact_user_id"],
            "contact_open_dingtalk_id": contact.get("open_dingtalk_id", ""),
            "session_id": sid,
            "prior_attempt": "无",
            "workspace_root": env["DWS_CHAT_AGENT_WORKSPACE_ROOT"],
            "worktree_root": env["DWS_CHAT_AGENT_WORKTREE_ROOT"],
            "recent_context_json": json.dumps(context, ensure_ascii=False, indent=2),
            "current_messages_json": json.dumps(current_messages, ensure_ascii=False, indent=2),
            "active_tasks_json": task_snapshot(db, run["contact_user_id"], started),
            "now": started.replace("T", " ")[:19],
            "skill_humanizer": skills.get("humanizer_zh", ""),
            "skill_answer_html": skills.get("answer_me_with_html", ""),
            "script_am": skills.get("answer_me_script", ""),
            "deliveries_dir": str(ROOT / "state" / "deliveries"),
        }
        shared = render_prompt((ROOT / "prompts" / "shared.md").read_text(encoding="utf-8"), variables)
        stage = render_prompt((ROOT / "prompts" / "worker.md").read_text(encoding="utf-8"), variables)
        prompt = assemble_stage_prompt(shared, stage)

        changes = list(db.execute(
            "select * from task_changes where run_id = ? order by created_at", (sid,)
        ))
        progress = list(db.execute(
            "select * from progress where session_id = ? order by seq", (sid,)
        ))
        board = db.execute(
            "select * from board_runs where session_id = ?", (sid,)
        ).fetchone()
        outgoing = list(db.execute(
            "select * from outgoing where session_id = ? order by sent_at", (sid,)
        ))
        task_facts = []
        for change in changes:
            payload = json.loads(change["response_json"])
            task_facts.append({
                "tool_call_id": change["tool_call_id"],
                "task_id": change["task_id"],
                "created_at": change["created_at"],
                "tool_result": payload,
                "facts_written": [
                    f["text"] for f in facts_for_task(db, change["task_id"], finished)
                    if f["created_at"] == change["created_at"]
                ],
            })
        commits = git_commits(env, started, finished)

        # ---------- 写 prompt.md ----------
        (out_dir / f"{sid}.prompt.md").write_text(
            "# 重放 prompt（按 prompts/*.md + 审计库变量渲染）\n"
            "# 注意：recent_context_json 部分不完整（钉钉历史未落库），"
            "active_tasks_json 是按 created_at 近似快照。\n\n" + prompt,
            encoding="utf-8",
        )

        # ---------- 写 trajectory.md ----------
        lines = [
            f"# 轨迹重建：{sid}",
            "",
            f"- 联系人：{run['contact_name']}（{run['contact_user_id']}）",
            f"- 阶段/Agent：worker / pi-worker-deepseek（aimodel-zhizhen/deepseek-flash, thinking=high）",
            f"- 时间：{started} → {finished}",
            f"- 终态：action={run['action']} status={run['status']} "
            f"exit={run['codex_exit_code']}",
            f"- board_run：status={board['status']} outcome={board['outcome']} "
            f"origin={board['origin']} worktree={board['worktree_path'] or '（无）'}",
            "",
            "> 证据等级：`[EXACT]` = 审计库/git 原文；`[INFER]` = 由结果反推；"
            "`[LOST]` = 已删除无法恢复。",
            "",
            "## 1. 输入（本轮消息）[EXACT]",
            "",
        ]
        for row in batch:
            lines += [
                f"- `{row['message_id']}` @ {row['received_at']} "
                f"（{row['content_length']} 字符，status={row['status']}）",
                "",
                "```text",
                row["request_preview"],
                "```",
                "",
            ]
        lines += ["## 2. prompt 骨架 [EXACT 模板 / INFER 上下文]", ""]
        lines += [
            f"- 模板：`prompts/shared.md` + `prompts/worker.md`（本次运行时的 05:56 版本；之后如有改动以 git 为准）",
            f"- 渲染后长度：{len(prompt)} 字符",
            f"- `active_tasks_json`：{len(json.loads(variables['active_tasks_json']))} 条活跃事项",
            f"- `recent_context_json`：重建 {len(context)} 条"
            f"{'（仅审计库可见部分，钉钉人工消息缺失）' if context_partial else ''}",
            f"- 完整 prompt 见：`{sid}.prompt.md`",
            "",
            "## 3. 时间线",
            "",
        ]
        timeline: list[tuple[str, str]] = [
            (started, "会话启动，cwd=workspace_root，`pi --mode rpc --no-session`"),
            (started, "prompt 经 RPC 送入，agent_start"),
        ]
        sub_lines: list[tuple[str, str]] = []
        for item in progress:
            timeline.append((
                item["created_at"],
                f"report(progress) #{item['seq']} [EXACT]：{item['text']}",
            ))
        for change in task_facts:
            version = change["tool_result"]["task"]["version"]
            timeline.append((
                change["created_at"],
                f"task_record 调用 `{change['tool_call_id']}` → "
                f"任务 `{change['task_id']}` v{version} [EXACT]",
            ))
            for fact in change["facts_written"]:
                sub_lines.append((change["created_at"], f"fact：{fact}"))
        for commit in commits:
            checkpoint = not commit["subject"] or commit["subject"].startswith("(空提交消息")
            kind = "git-ai 自动检查点（非交付提交）" if checkpoint else commit["subject"]
            timeline.append((
                commit["when"],
                f"提交 `{commit['sha']}` @ {commit['repo']} [EXACT]：{kind}",
            ))
        for row in outgoing:
            kind = ("卡片同步（无关联输入消息）" if row["event_ids_json"] == "[]"
                    else "终稿投递")
            timeline.append((
                row["sent_at"],
                f"{kind} card={row['card_biz_id']} flow={row['card_flow_status']} "
                f"delivery={row['delivery_status']} [EXACT]",
            ))
        timeline.append((finished, f"会话结束，exit={run['codex_exit_code']}"))
        def _key(item: tuple[str, str]) -> datetime:
            return datetime.fromisoformat(item[0].replace("Z", "+00:00"))

        for when, text in sorted(timeline, key=_key):
            lines.append(f"- `{when}` {text}")
        for when, text in sorted(sub_lines, key=_key):
            lines.append(f"  - `{when}` {text}")
        lines += [
            "",
            "## 4. 最终答复 [EXACT，审计库截断至 1000 字符]",
            "",
            "````text",
            run["handled"] if run["handled"] else "（无正文：no_reply/silent）",
            "````",
            "",
            "## 5. 缺失段 [LOST]",
            "",
            "- pi RPC 事件流（`events.jsonl`）：成功后被 rmtree 删除",
            "- `stderr.log` / `result.txt` / `task-context.json`：同上",
            "- 全部 bash/read/grep/task_lookup 工具调用及其参数、输出",
            "- assistant 中间正文与思考过程",
            "- 完整 recent_context（钉钉历史由 dws 实时拉取，未落库；"
            "重建版只有审计库可见的 events/outgoing）",
            "- 钉钉侧人工发送的消息（不在任何本地表里）",
            "- token 用量与单次模型调用耗时",
            "",
            "## 6. 重建产物",
            "",
            f"- `{sid}.prompt.md`：按真实渲染路径重放的完整 prompt",
            f"- `{sid}.events.reconstructed.jsonl`：重建事件流（`reconstructed=true` 表示非原始）",
            f"- `{sid}.tool-calls.json`：task_changes 原文 + 本轮写入的 facts",
            "",
        ]
        (out_dir / f"{sid}.trajectory.md").write_text("\n".join(lines), encoding="utf-8")

        # ---------- 写 facts/任务变更原始证据 ----------
        (out_dir / f"{sid}.tool-calls.json").write_text(
            json.dumps(task_facts, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        # ---------- 写重建版 pi RPC 事件流 ----------
        # 只把审计库能证明的锚点排进事件流，缺失段用 gap 事件显式标出，
        # 每条带 reconstructed/evidence 字段，避免被误当成原始 events.jsonl。
        events: list[dict] = [
            {"type": "session_start", "reconstructed": True,
             "session_id": sid, "timestamp": started,
             "model": "aimodel-zhizhen/deepseek-flash", "thinking": "high",
             "cwd": env["DWS_CHAT_AGENT_WORKSPACE_ROOT"], "argv_session": "--no-session"},
            {"type": "response", "command": "prompt", "success": True, "reconstructed": True},
            {"type": "agent_start", "timestamp": started},
            {"type": "turn_start"},
            {"type": "message_start", "message": {"role": "user", "content": [
                {"type": "text", "text": prompt[:2000] + ("…" if len(prompt) > 2000 else "")}
            ]}, "reconstructed": True,
             "evidence": "按 prompts/*.md + 审计库变量重放；全文见 .prompt.md"},
            {"type": "gap", "what": "模型思考与全部 bash/read/grep/task_lookup 调用",
             "reason": "events.jsonl 运行成功后即被删除", "from": started},
        ]
        for item in progress:
            events.append({
                "type": "tool_execution_start", "toolName": "report",
                "arguments": {"branch": "progress", "text": item["text"]},
                "timestamp": item["created_at"], "reconstructed": True,
                "evidence": "progress 表",
            })
        for change in task_facts:
            task = change["tool_result"]["task"]
            events += [
                {"type": "tool_execution_start", "toolCallId": change["tool_call_id"],
                 "toolName": "task_record",
                 "arguments": {
                     "task_id": change["task_id"] if task["version"] > 1 else None,
                     "goal": task["goal"], "status": task["status"],
                     "waiting_on": task["waiting_on"],
                     "resources": task["resources"],
                     "evidence_summary": task["evidence_summary"],
                     "evidence_message_ids": task["evidence_message_ids"],
                 },
                 "timestamp": change["created_at"], "reconstructed": True,
                 "evidence": "task_changes.response_json（回显的任务快照；入参仅重建）"},
                {"type": "tool_execution_end", "toolCallId": change["tool_call_id"],
                 "result": change["tool_result"], "timestamp": change["created_at"]},
            ]
        if run["handled"]:
            events.append({"type": "message_end", "message": {"role": "assistant", "content": [
                {"type": "text", "text": run["handled"]}
            ]}, "timestamp": finished, "reconstructed": True,
             "evidence": "runs.handled（已截断至 1000 字符）"})
        else:
            events.append({"type": "tool_execution_start", "toolName": "no_reply",
                           "arguments": {}, "timestamp": finished,
                           "reconstructed": True, "evidence": "runs.action=no_reply"})
        events.append({"type": "agent_end", "timestamp": finished,
                       "exit": run["codex_exit_code"]})
        (out_dir / f"{sid}.events.reconstructed.jsonl").write_text(
            "\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n",
            encoding="utf-8",
        )
        index.append({
            "session_id": sid,
            "contact": run["contact_name"],
            "contact_user_id": run["contact_user_id"],
            "started_at": started,
            "finished_at": finished,
            "action": run["action"],
            "status": run["status"],
            "outcome": board["outcome"],
            "messages": len(batch),
            "tool_calls_visible": len(changes),
            "facts_written": sum(len(c["facts_written"]) for c in task_facts),
            "progress_updates": len(progress),
            "reply_chars": len(run["handled"]),
            "commits": [f"{c['repo']}@{c['sha']}" for c in commits],
        })
        print(f"rebuilt {sid}")

    (out_dir / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    summary = [
        f"# {day} 轨迹重建索引",
        "",
        "由 `scripts/reconstruct_trajectories.py` 从 `state/audit.sqlite3` + git 生成。",
        "原始 `events.jsonl`/`prompt.md` 已被服务删除，这里全部是重建，",
        "`reconstructed=true` 的字段均为回填，不是原始字节。",
        "",
    ]
    for item in index:
        summary += [
            f"## {item['session_id']}",
            f"- 联系人：{item['contact']}",
            f"- 窗口：{item['started_at']} → {item['finished_at']}",
            f"- 终态：{item['action']}/{item['status']}（{item['outcome']}）",
            f"- 输入消息：{item['messages']} 条；可见工具调用：{item['tool_calls_visible']}；"
            f"facts：{item['facts_written']}；进度：{item['progress_updates']}",
            f"- 答复长度：{item['reply_chars']} 字符",
            f"- 相关提交：{', '.join(item['commits']) or '无'}",
            f"- 明细：`{item['session_id']}.trajectory.md` / `.prompt.md` / `.tool-calls.json`",
            "",
        ]
    (out_dir / "README.md").write_text("\n".join(summary), encoding="utf-8")
    print(f"written to {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
