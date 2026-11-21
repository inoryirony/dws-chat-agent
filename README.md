# PROJECT D.I.A.M.O.N.D.

*DingTalk-Integrated Agent for Messaging, Orchestration & Networked Delegation*

[English](README.md) | [简体中文](README.zh-CN.md)

An autonomous DingTalk interface for coordinating local AI coding agents. It watches an explicit allowlist, answers direct messages, and can hand implementation work to a worker agent. It supports Codex, Claude Code, Pi, DSH, and thin custom adapters without fixing either role to one product.

The service runs on the operator's machine and reuses existing DWS and coding-agent logins. Personal identities, contact IDs, local paths, and internal domains stay in an ignored `.env` file.

## Let an agent configure it for you

Paste this sentence to a coding agent on the target machine:

> Install and configure DWS Chat Agent for me: https://raw.githubusercontent.com/inoryirony/dws-chat-agent/main/docs/agent-config.md

Already installed? The same guide covers safety checks and updates:

> Check and update DWS Chat Agent for me: https://raw.githubusercontent.com/inoryirony/dws-chat-agent/main/docs/agent-config.md

## What it does

- **Chat intake.** Direct messages from an explicit allowlist enter a per-conversation queue.
- **Watch rules.** Jira and GitLab streams are polled on an interval; new hits become ordinary pipeline tasks and report back over DingTalk when done.
- **Triage and delegation.** A read-only front stage answers directly or escalates to an executive worker; the preset decides whether a front stage exists at all.
- **Five agent protocols.** Codex, Claude Code, Pi, DSH, and the generic `custom-jsonl-v1` contract all plug into the same stage machinery.

## Runtime flow

```text
DingTalk event ──┐
                 ├─> per-conversation queue and serial lock
watch poll hit ──┘      -> stabilize and merge recent messages
                        -> pipeline steps in order (default: front -> worker)
                             front (triage, read-only)
                               -> natural-language reply
                               -> zero-argument escalate()
                             worker (executive)
                               -> natural-language reply
                        -> final freshness check
                        -> final body replaces the thinking card
```

The chat path starts with a zero-token coarse gate that drops empty acknowledgements and enforces the message-size limit. The watch path skips the gate: a polled Jira issue or GitLab merge request is already a concrete item. Both paths converge on the same queue, pipeline, and delivery code.

Each front or worker run gets a fresh agent session. Messages that arrive while it is running are sent into that same live session instead of being silently detached:

- Codex: `codex app-server` with `turn/steer`
- Pi: native `pi --mode rpc` with `streamingBehavior: "steer"`
- Claude Code: streaming JSON input/output
- DSH: no native steering; a supplement restarts the run with accumulated context

The front receives a session-local stdio MCP server with one zero-argument tool, `escalate()`. Codex and Claude mount it through their native CLI options. Pi loads the bundled thin adapter, which calls the same stdio MCP server. Nothing is added to global agent configuration.

If native steering fails, the current run is stopped safely and restarted with the accumulated context. A manual operator reply takes over the current turn. `human_cooldown_seconds` controls how long later messages stay with the operator. Set it to `0` to resume automation on the next incoming message.

Agent output is deliberately unstructured. A front agent either writes the final natural-language body or calls `escalate()` before writing a body. A worker writes only the final natural-language body. There is no output JSON schema, route object, change manifest, local Git verification, or delivery replanning layer; repository rules and the repository's normal Git/CI review flow remain authoritative.

Provider retry notifications are not terminal results. In particular, a Codex `willRetry=true` stream error is ignored until `turn/completed` supplies the final status, so a recovered TLS or response-stream interruption does not discard a completed answer.

## Repository layout

```text
pyproject.toml               package metadata and dws-chat-agent console command
config.json                  current public profiles, workflows, watches, and runtime policy
config.example.json          portable configuration scaffold
dm_agent.py                  compatibility CLI used by existing installations
agent_core.py                compatibility import for older local scripts
agent_runtime.py             compatibility import for older local scripts
dashboard.py                 compatibility import for older local scripts
src/dws_dm_agent/
  __main__.py                python -m dws_dm_agent entry point
  config.py                  environment expansion and validated settings model
  core.py                    messages, coarse gate, events, and audit
  dws.py                     history and replies through DWS
  service.py                 queues, intake, watch polling, pipeline execution, delivery
  prompts.py                 shared prompt variables, rendering, and validation
  dashboard.py               localhost API, audit snapshot, and safe config editor
  front_eval.py              live front-routing replay runner
  stages.py                  stage-name -> capability registry (triage, executive)
  watches.py                 watch rule parsing and Jira/GitLab source construction
  skills_catalog.py          named skill pool resolution and prompt rendering
  channels/                  outbound transports (base protocol, Jira, GitLab, registry)
  sources/                   inbound pollers (Jira JQL, GitLab projects, stdlib HTTP)
  runners/runtime.py         stage execution, steering, worktrees, fallback chain
  runners/adapters/          protocol adapters: codex, claude, pi, dsh, custom, registry
  runners/escalate_mcp.py    session-local zero-argument stdio MCP server
  runners/task_tools.py      service-tool backend (task_record, task_lookup, report)
  runners/dws_tools_mcp.py   service-tool stdio MCP server
  runners/mcp_catalog.py     named MCP server catalog
  runners/mcp_tool_gate.py   directTools filtering for mounted MCP servers
  runners/pi_escalate.ts     Pi-to-stdio-MCP adapter
  runners/pi_dws_tools.ts    Pi service-tool extension
  web/                       dashboard, settings, themes, motion, and favicon
tests/                       service, dashboard, MCP, watch, stage, and runner regressions
evals/front-routing.json     checked-in front-routing replay cases
prompts/                     shared, front, worker, and supplement business rules
deploy/macos/                LaunchAgent template
deploy/dsh/                  worker patch applied to a DSH install
scripts/manage.sh            macOS LaunchAgent lifecycle implementation
scripts/                     eval dataset build, trajectory reconstruction, launch verification
start-macos.command          one-click macOS setup/start
start-windows.cmd            one-click Windows setup/start
docs/                        agent setup guide, custom protocol, architecture, plans
```

The top-level Python files and the per-driver modules under `runners/` (`claude.py`, `codex.py`, `custom.py`, `pi.py`) are deliberately thin compatibility seams. Existing LaunchAgents and local scripts keep working while new code imports the `dws_dm_agent` package and the adapters under `runners/adapters/`. Runtime configuration, prompts, `.env`, audit state, and worktrees remain outside the package so updates never overwrite operator data.

The **DIAMOND Network** dashboard at [http://127.0.0.1:8765/](http://127.0.0.1:8765/) stays focused on operations, the queue, channels, and diamond-unit status. Metropolis preserves the original card-console layout; Diamond uses the tactical topology view at [http://127.0.0.1:8765/diamond-preview](http://127.0.0.1:8765/diamond-preview). Both views share the PROJECT D.I.A.M.O.N.D. vocabulary, live data, functionality, color system, and theme preference. The separate settings page at [http://127.0.0.1:8765/settings](http://127.0.0.1:8765/settings) switches interface themes, independently applies built-in or user-created colors, searches DingTalk users for the local allowlist, and edits diamond-units, workflows, and prompts. Appearance is stored only in the current browser. Configuration saves are revision-checked, schema-validated, blocked while work is active or queued, written atomically, and applied to subsequent sessions without restarting. The page never exposes or overwrites profile environment variables, provider options, auth files, contact IDs, or `.env` values.

## Requirements

- Python 3.11 or newer
- A working `dws` CLI login
- Git
- The binaries used by the active workflow: Codex, Claude Code, Pi, or DSH

The service itself uses only the Python standard library; it does not create a project virtual environment.

## First run

### macOS

Double-click `start-macos.command`.

On the first run it creates `.env`, opens it in TextEdit, and stops. Fill in the values and double-click it again. It validates the runtime, installs/starts the per-user LaunchAgent, and opens [http://127.0.0.1:8765](http://127.0.0.1:8765).

Command-line lifecycle:

```bash
./manage.sh doctor
./manage.sh start
./manage.sh status
./manage.sh logs
./manage.sh restart
./manage.sh stop
```

### Windows

Double-click `start-windows.cmd`.

On the first run it creates `.env`, opens Notepad, and stops. After configuration, run it again. The console stays open while the agent runs and the dashboard opens automatically. Stop it with `Ctrl+C`.

### Manual setup

```bash
cp .env.example .env
chmod 600 .env
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 dm_agent.py doctor
python3 dm_agent.py run --mode shadow --open-dashboard
```

The packaged entry point is equivalent:

```bash
PYTHONPATH=src python3 -m dws_dm_agent doctor
PYTHONPATH=src python3 -m dws_dm_agent run --mode shadow --open-dashboard
```

Set `DWS_CHAT_AGENT_HOME` when `config.json` and `.env` live outside the source checkout. Otherwise the service uses the source project when it contains `config.json`, then falls back to the current directory.

Replay the checked-in front-routing cases against the configured live front model:

```bash
PYTHONPATH=src python3 -m dws_dm_agent.front_eval --concurrency 1
```

Check the deterministic gate against a single message without calling any model:

```bash
PYTHONPATH=src python3 -m dws_dm_agent probe-gate "收到"
```

Start in `shadow` mode until configuration is verified. Shadow mode performs analysis and audit but does not send DingTalk messages. Switch `DWS_CHAT_AGENT_MODE` to `live` when it should reply.

## Command-line reference

```text
dws-chat-agent [--config PATH] [--env-file PATH] [--verbose] <command>

  run [--mode shadow|live] [--open-dashboard]   consume DWS events
  doctor                                        validate local runtime without sending
  probe-gate <text>                             run the zero-token gate on one message
```

`--mode` overrides `DWS_CHAT_AGENT_MODE` for that run. `--config` and `--env-file` default to `config.json` and `.env` in the resolved home directory.

## Agent and workflow configuration

`config.json` contains reusable profiles under `agents` and preset pipelines under `workflows.presets`. Use the dashboard gear to open `/settings` and edit the safe public fields, or change the file directly when the service is stopped. `workflows.active` selects a preset.

```json
{
  "workflows": {
    "active": "pi-default",
    "presets": {
      "pi-default": {
        "supplement_strategy": "steer",
        "supplement_prompt": "prompts/supplement.md",
        "auto_messages": {
          "ack_enabled": true,
          "ack": "已收到消息，正在处理中。"
        },
        "front": {
          "label": "快速前台",
          "agent": "pi-front",
          "prompt": "prompts/front.md"
        },
        "worker": {
          "label": "执行 Agent",
          "agent": "pi-worker",
          "prompt": "prompts/worker.md"
        }
      }
    }
  }
}
```

A preset always requires an executive stage; `worker` is the conventional name for it, `front` is optional. Omitting `front` sends the original conversation directly to the worker. The service tools referenced by the shipped prompts (`no_reply`, `task_record`, `task_lookup`, `report`) are mounted only for `pi` agents (`pi-rpc` protocol). The check is per stage: configuring a stage, or any of its fallback agents, with another driver while that stage's own prompt or the shared supplement prompt (it is steered into any live stage session) references these tools fails at load time. Stages whose prompts do not reference them can keep any driver. Use a pi agent for such stages or remove the service-tool instructions from the prompts. A profile separates the executable launcher from the session protocol:

```json
{
  "ccr-worker": {
    "driver": "claude",
    "protocol": "claude-stream-json",
    "command": ["ccr", "code"],
    "model": "sonnet",
    "read_only": false,
    "timeout_seconds": 10800
  },
  "omp-worker": {
    "driver": "pi",
    "protocol": "pi-rpc",
    "command": ["omp"],
    "model": "gpt-5.6",
    "reasoning_effort": "high",
    "read_only": false,
    "timeout_seconds": 10800
  }
}
```

`command` is an exact argument array and is never executed through a shell. Therefore a proxy launcher such as `ccr code`, an absolute executable path, a `.cmd` shim on Windows, or a wrapper with fixed arguments all work without quoting tricks. The adapter appends the flags required by `protocol`. Keep credentials in `environment` or the process login, never in `command`.

`label` is display text for a workflow stage. `options` contains protocol-specific settings, while `extra_args` appends literal CLI arguments after the adapter's required flags. `timeout_seconds: 0` means no agent deadline. For Pi, `options.tools: null` omits the `--tools` allowlist and exposes its built-in tools; the front prompt and `read_only: true` remain the authorization boundary.

`options.extensions` is an allowlist for Pi. When the key is present — including an empty array — the adapter passes `--no-extensions` and loads only the listed extensions plus its own service-tool extensions. An empty array is meaningful: it means "no third-party extension", which is what a read-only front wants. Omitting the key keeps Pi's default discovery.

`options.mcp` mounts MCP servers per profile through `pi-mcp-adapter`. Each entry is either a catalog name (`grafana-dev`, `grafana-prod-logging`, `hsj-atlassian`, see `runners/mcp_catalog.py`), an object that overrides fields on the catalog entry, or a full `{command|url, args, ...}` definition. The adapter writes a session-local `mcp.json` and passes `--mcp-config` with `PI_MCP_CONFIG_MODE=exclusive`, so global servers in `~/.pi/agent/mcp.json` never reach the session. Prefer `directTools` with a short list over leaving a 60-tool server fully declared: schema size is charged on every request. Declaring `options.mcp` without an adapter in `options.extensions` fails at prepare time instead of starting a Pi that rejects `--mcp-config`.

`fallbacks` on an executive profile is an ordered list of other profile names. The worker stage tries them in turn when the primary agent fails; the chain anchor must be an executive stage.

Pi session completion waits for `agent_settled`, not `agent_end`. `agent_end` only closes one low-level run: retries, overflow recovery, compaction retries, and queued steering or follow-up messages can still follow, and Pi reports `agent_settled` once nothing remains.

Prompt text is stored in normal UTF-8 files and the active files are shown verbatim on the localhost settings page. A stage prompt may use `{{self_name}}`, `{{agent_name}}`, `{{worker_name}}`, `{{contact_name}}`, `{{contact_alias}}`, `{{contact_user_id}}`, `{{contact_open_dingtalk_id}}`, `{{session_id}}`, `{{prior_attempt}}`, `{{workspace_root}}`, `{{worktree_root}}`, `{{skills}}`, `{{recent_context_json}}`, `{{current_messages_json}}`, and `{{now}}`. The supplement prompt receives `{{supplement_messages_json}}`, `{{session_id}}`, contact identity fields, and `{{self_name}}`.

When configured, the front profile must set `read_only: true`. It may use the tools available to its provider for bounded read-only investigation, then answer directly; any write or external side effect must escalate. The worker has normal coding-agent permissions and judges authorization from the original conversation plus repository rules; escalation itself never grants write permission.

With streaming cards enabled, acceptance is shown immediately. Both roles use the same provider-neutral stream surface: Codex maps reasoning summaries, Pi maps native thinking, Claude maps thinking blocks, and custom adapters may emit `thinking`. The service does not invent progress labels, fixed progress intervals, or heartbeat text. Models that emit no native thinking produce no fabricated progress. The final natural-language body is persisted in the local outbox before delivery, retried with the same send UUID after transient failures, and replaces the thinking card after the final freshness check.

### Pipeline ordering

`front` and `worker` are conventional slot names, not built-in constants. A preset may declare an ordered `pipeline`:

```json
"pipeline": ["front", "worker"]
```

Steps run strictly one after another — no DAG, no parallelism. When `pipeline` is omitted the service derives the order from the declared stages (triage first, executive last). Any slot name not in the capability table is treated as an executive stage. A stage's capability is fixed by its kind in `src/dws_dm_agent/stages.py`:

| kind | read-only | mounts `escalate()` | worktree | role |
| --- | --- | --- | --- | --- |
| `triage` | yes | yes | no | inspect and answer, or escalate |
| `executive` | no | no | yes | implement and report |

To add a node type, register a new slot name against a kind; no string comparison against stage names exists in the runtime.

### Agent adapters

Five protocols are registered in `runners/adapters/registry.py`. A profile may give `driver` only; the matching protocol is filled in.

| driver | protocol | image input | native steer | service tools | completion signal |
| --- | --- | --- | --- | --- | --- |
| `pi` | `pi-rpc` | native | yes | yes | `agent_settled` |
| `codex` | `codex-app-server` | native | yes | no | `turn/completed` with status `completed` |
| `claude` | `claude-stream-json` | embedded | yes | no | stream end |
| `dsh` | `dsh-headless-jsonl` | embedded | no | no | final JSONL record |
| custom | `custom-jsonl-v1` | embedded | yes | no | protocol-specific |

`claude-code` is accepted as an alias for `claude`. An unknown protocol fails with `unsupported agent protocol: ...`. The `custom-jsonl-v1` stdin/stdout contract is documented in [docs/custom-agent-protocol.md](docs/custom-agent-protocol.md). The front adapter receives the session-local MCP path in `DWS_ESCALATE_MCP_CONFIG`; Pi's own escalate is a native extension, so its front needs no MCP server at all.

DSH has no native steering. A supplement during a DSH run stops the current process and restarts it with prior messages carried forward, so short follow-up messages cost a full re-run.

### Skills

A top-level `skills` pool maps names to absolute paths:

```json
"skills": {
  "humanizer_zh": "/Users/me/skills/humanizer-zh",
  "answer_me_with_html": "/Users/me/skills/answer-me-with-html"
}
```

Stage prompts reference the pool through `{{skills}}`. Unknown names or non-absolute paths fail at load time. Each adapter delivers the resolved skills differently: Pi passes `--skill`, Claude passes `--plugin-dir`, Codex creates symlinks inside a per-session home, and DSH is patched with `customSkillDirs`.

### Watches

`watches` turns external streams into ordinary pipeline tasks:

```json
{
  "watch_interval_seconds": 300,
  "watch_lookback_hours": 24,
  "watches": [
    {
      "source": "jira",
      "name": "ZKAI issues assigned to me",
      "jql": "assignee = currentUser() AND created >= -1d ORDER BY created DESC",
      "projects": ["ZKAI"],
      "instruction": "Analyze the issue and return a conclusion for the service to post. Keep its status unchanged.",
      "notify": "dm"
    },
    {
      "source": "gitlab",
      "name": "zk-ai core merge requests",
      "projects": ["xk/product/zk-ai/change-monitor"],
      "instruction": "Review this merge request and return the summary for the service to post."
    }
  ]
}
```

- `source` is `jira` or `gitlab`. There is no separate `sources` or `channels` configuration key; the registry is fixed in code.
- `watch_interval_seconds` defaults to 300 and is clamped to a minimum of 60.
- `watch_lookback_hours` defaults to 24.
- Each watch persists its own cursor. Resource and rule objective identify a watch event. A fix and a customer explanation on the same issue can have separate acceptance criteria.
- Set `notify` to `dm` (default) or `none`. An authorized DingTalk notification and a source comment have separate destinations, action IDs and receipts. The dashboard shows the status of each. Uncertain remote results are reconciled against the original action marker.
- Watches use the configured workflow, including its project tools, shell and fallbacks. The worker chooses how to investigate and test. The service posts the final comment.
- Set `merge_targets` on a GitLab watch to enable conditional merges for its listed projects. The default is empty. Allowed branches come from `GITLAB_MERGE_TARGETS` (comma-separated, default `dev,main`; e.g. `test,dev,main`). The worker reads the full diff and discussions, judges whether the change is small and focused, and records its reasoning. The service rechecks the authorized target, exact SHA, successful CI and mergeability before submission. The merge has its own receipt.

### Recovery and scheduled work

The service resumes pending inputs from the local inbox and consults channel history for legacy rows without a stored payload. For an orphaned Pi run, it checks the last turn's `agent_settled`, the original input and authorization, existing receipts, human takeover and later messages. A legacy run with only a preview and no recorded delivery goes to operator review. The board shows the oldest queued inputs, unowned runs and per-destination receipts.

A receipt in the "remote uncertain" state (`remote_uncertain`) means the write request left for the remote system but no receipt came back — neither success nor failure can be proven. The service never auto-resends such rows (duplicate delivery outranks a lost one). Jira/GitLab comments and merges keep reconciling via read-only GET until a receipt is found and the row turns confirmed automatically. Channels without reconciliation support stay listed with a `needs_operator_reconciliation: <detail>` reason: check the destination system by the row's recipient and remote ID — a one-glance confirmation, no panel action required, the row will not resend. Retries only happen on proven failure and are bounded by the 24-hour idempotency window (same action ID inside the window; past it the row turns uncertain rather than risk a duplicate).

One message can provide evidence for several tasks. Each task has its own version, acceptance evidence and original conversation. Timed waits survive the two-day inactivity cutoff for prompt context. The operator can link additional received sources without granting their audience access to another conversation's content or write permissions.

Cross-organization contacts: DingTalk treats users outside your organization differently. Listing their message history requires a temporary data grant (`dws chat data-auth cross-org --all --grant-type timed --ttl 24h`). The grant expires after about 24 hours; while it is missing, history reconciliation fails and deliveries are deferred until the grant is renewed. A cross-org user's `userId` is often not usable for message listing (`peerUid is required`), so the service falls back to `{{contact_open_dingtalk_id}}` when available.

## Dashboard API

The dashboard server binds `127.0.0.1:8765` and serves:

| path | purpose |
| --- | --- |
| `/` | operations board |
| `/diamond-preview` | topology view |
| `/settings` | theme, allowlist, workflows, prompts |
| `/api/snapshot` | current queue and stage state |
| `/api/board` | board rows and run history |
| `/api/config` (GET/POST) | read or save the public configuration |
| `/api/contacts/search` | DingTalk user lookup for the allowlist |
| `/api/session/new` | start a clean session |
| `/api/task/answer` | answer a pending task prompt |
| `/api/task/link-source` (POST) | let the operator link a received `message_id` to a `task_id` |

## Safety

- A small deterministic gate only drops empty acknowledgements and enforces the message-size limit; all semantic safety decisions stay with the model. `probe-gate` runs that gate alone, for inspection.
- Repository AGENTS.md files and the coding agent enforce project-specific change, review, test, and release policy.
- The service rechecks the conversation before acknowledgements and final replies.
- Automated DingTalk messages keep the platform AI tag enabled.

Operational state, logs, audit data, and temporary sessions live under `state/` and are not committed. `.env` and `.env.*` are ignored; `.env.example` is the only exception.

## Documentation

| file | contents |
| --- | --- |
| [docs/agent-config.md](docs/agent-config.md) | guide a coding agent can follow to install and configure the service |
| [docs/custom-agent-protocol.md](docs/custom-agent-protocol.md) | `custom-jsonl-v1` stdin/stdout contract |
| [docs/target-architecture-20261106.md](docs/target-architecture-20261106.md) | architecture overview and open decisions |
| [docs/latency-and-streaming-plan.md](docs/latency-and-streaming-plan.md) | latency measurements and streaming design |
| [docs/no-reply-task-memory-plan-20261012.md](docs/no-reply-task-memory-plan-20261012.md) | silent-reply and task memory design |
| [docs/audit-findings-20261107.md](docs/audit-findings-20261107.md) | full audit findings, kept as a work list |
