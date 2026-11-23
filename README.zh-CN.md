# PROJECT D.I.A.M.O.N.D.

*DingTalk-Integrated Agent for Messaging, Orchestration & Networked Delegation*

[English](README.md) | [简体中文](README.zh-CN.md)

一个用于协调本地 AI 编码 Agent 的钉钉自主交互层。它监视一份显式白名单，回答私聊消息，并可以把实施类工作交给执行 Agent。支持 Codex、Claude Code、Pi、DSH 和轻量自定义适配器，前台和执行 Agent 均不绑定具体产品。

服务运行在操作者自己的电脑上，复用现有 DWS 和编码 Agent 登录。个人身份、联系人 ID、本地路径和内部域名都保存在被忽略的 `.env` 文件中。

## 让 Agent 帮你配置

把下面这句话复制给目标电脑上的编码 Agent：

> 帮我安装并配置 DWS Chat Agent：https://raw.githubusercontent.com/inoryirony/dws-chat-agent/main/docs/agent-config.md

已经安装过？同一份指南也支持安全检查和更新：

> 帮我检查并更新 DWS Chat Agent：https://raw.githubusercontent.com/inoryirony/dws-chat-agent/main/docs/agent-config.md

## 它能做什么

- **消息接入。** 来自显式白名单的私聊消息进入按会话隔离的队列。
- **监视规则。** 按固定间隔轮询 Jira 和 GitLab 流；新的命中会变成普通流水线任务，结束后通过钉钉回报。
- **分诊与委派。** 只读的 front（前台）阶段可以直接回答，也可以升级给 executive 执行 Agent；是否设置 front 阶段由 preset 决定。
- **五种 Agent 协议。** Codex、Claude Code、Pi、DSH 以及通用的 `custom-jsonl-v1` 契约，都接入同一套阶段机制。

## 运行流程

```text
钉钉事件 ────────┐
                 ├─> 按会话排队 + 串行锁
监视轮询命中 ────┘      -> 稳定并合并最近消息
                        -> 按顺序执行流水线步骤（默认：front -> worker）
                             front（前台，只读）
                               -> 自然语言直接回复
                               -> 调用零参数 escalate()
                             worker（执行）
                               -> 自然语言最终回复
                        -> 定稿前新鲜度检查
                        -> 最终正文替换思考卡
```

聊天路径先经过零 token 的确定性粗筛：丢弃空的确认消息，并限制单条消息的体量。监视路径跳过粗筛——轮询到的 Jira issue 或 GitLab merge request 本身已经是具体条目。两条路径最终汇入同一套队列、流水线和投递代码。

每次 front 或 worker 运行都会拿到一个全新的 Agent 会话。运行期间到达的消息会被送进同一个活会话，而不是被静默丢弃：

- Codex：`codex app-server` + `turn/steer`
- Pi：原生 `pi --mode rpc` + `streamingBehavior: "steer"`
- Claude Code：流式 JSON 输入/输出
- DSH：没有原生 steer；补充消息会带着累积上下文重启运行

front 会收到一个会话级 stdio MCP server，其中只有一个零参数工具 `escalate()`。Codex 和 Claude 通过各自的 CLI 原生选项挂载；Pi 加载仓库内置的轻量适配器，调用同一个 stdio MCP server。不会向全局 Agent 配置写入任何内容。

如果原生 steer 失败，当前运行会被安全停止，并带着已累积的上下文重启。操作者在会话中回复后会接管当前轮。后续消息的冷却时间由 `human_cooldown_seconds` 控制，设为 `0` 时下一条新消息可立即触发 Agent。

Agent 输出刻意不使用结构化格式。front Agent 要么直接写出最终自然语言正文，要么在写正文之前调用 `escalate()`。worker 只写最终自然语言正文。这里没有输出 JSON schema、路由对象、变更清单、本地 Git 校验或投递重规划层；仓库规则和仓库正常的 Git/CI 审查流程仍是权威。

Provider 的重试通知不算终态。具体来说，Codex 的 `willRetry=true` 流错误会被忽略，直到 `turn/completed` 给出最终状态——这样 TLS 或响应流中断恢复之后，已完成的回答不会被丢掉。

### 卡死运行自动回收

monitor 协程一旦挂死，黑板心跳（`board_runs.updated_at`）就会停止刷新，而会话仍被当作「正在处理」，后续消息全部排队。看门狗任务 `run-reaper` 每分钟扫描一次在跑运行，仅当「黑板心跳超过 `run_reap_stale_seconds`（默认 1800 秒）未刷新」且「该运行的 agent 事件流沉默了同样久」两个条件同时成立时才回收：取消并摘除挂死的 worker、把黑板行标为 `aborted`（`watchdog_reclaimed_stale_heartbeat`）、把原批输入消息显式标为 `error`（不静默悬置）、吊销运行租约、收口处理中卡片，并在队列有积压时重拉 worker。正常的长工具调用只会让其中之一沉默；宿主睡眠或时钟大幅跳变后的第一轮扫描会自动跳过（墙钟与单调钟双护栏），防止墙钟失真误伤。回收运行的 worktree 清理仍依赖下次启动扫尾。

### PII lint（pre-commit）

个人数据不得入库：真名、钉钉 user/open ID、个人路径、手机号样式数字。`scripts/check_pii.py` 在提交时把关：通用模式规则 + 本地真名名单（`scripts/pii-names.local.txt`，已 gitignore，永不入库），并断言 gitignore 保护的文件（`config.json`、`.env`、`state/`、`.zcode/`）没有被 git 跟踪——ignore 规则对已跟踪文件无效，这条断言才是根因防护。每个 clone 装一次 hook：

```sh
scripts/manage.sh install-hooks
```

形似真数据的假 fixture 可在行尾加 `pii-allow` 标记逐行豁免。

## 仓库结构

```text
pyproject.toml               包元数据与 dws-chat-agent 控制台命令
config.json                  当前公开 profile、workflow、watch 与运行时策略
config.example.json          可移植的配置骨架
dm_agent.py                  既有安装使用的兼容 CLI
agent_core.py                兼容旧本地脚本的导入层
agent_runtime.py             兼容旧本地脚本的导入层
dashboard.py                 兼容旧本地脚本的导入层
src/dws_dm_agent/
  __main__.py                python -m dws_dm_agent 入口
  config.py                  环境变量展开与校验后的配置模型
  core.py                    消息、粗筛、事件与审计
  dws.py                     通过 DWS 读历史、发消息
  service.py                 队列、接入、监视轮询、流水线执行、投递
  prompts.py                 共享提示词变量、渲染与校验
  dashboard.py               localhost API、审计快照与安全配置编辑器
  front_eval.py              实时 front 路由回放运行器
  stages.py                  阶段名 -> 能力注册表（triage、executive）
  watches.py                 监视规则解析与 Jira/GitLab 流构造
  skills_catalog.py          命名技能池解析与提示词渲染
  channels/                  出站通道（base 协议、Jira、GitLab、注册表）
  sources/                   入站轮询器（Jira JQL、GitLab 项目、stdlib HTTP）
  runners/runtime.py         阶段执行、steer、worktree、fallback 链
  runners/adapters/          协议适配器：codex、claude、pi、dsh、custom、registry
  runners/escalate_mcp.py    会话级零参数 stdio MCP server
  runners/task_tools.py      服务工具后端（task_record、task_lookup、report）
  runners/dws_tools_mcp.py   服务工具 stdio MCP server
  runners/mcp_catalog.py     命名 MCP server 目录
  runners/mcp_tool_gate.py   对已挂载 MCP server 做 directTools 过滤
  runners/pi_escalate.ts     Pi 到 stdio MCP 的适配器
  runners/pi_dws_tools.ts    Pi 服务工具扩展
  web/                       面板、设置页、主题、动效与 favicon
tests/                       服务、面板、MCP、watch、stage、runner 回归测试
evals/front-routing.json     入库的 front 路由回放用例
prompts/                     共享、front、worker、supplement 业务规则
deploy/macos/                LaunchAgent 模板
deploy/dsh/                  应用到 DSH 安装目录的 worker 补丁
scripts/manage.sh            macOS LaunchAgent 生命周期实现
scripts/                     评测集构建、轨迹重建、启动校验
start-macos.command          macOS 一键安装/启动
start-windows.cmd            Windows 一键安装/启动
docs/                       Agent 配置指南、自定义协议、架构、规划
```

顶层 Python 文件和 `runners/` 下按驱动划分的模块（`claude.py`、`codex.py`、`custom.py`、`pi.py`）刻意保留为薄的兼容缝。现有 LaunchAgent 和本地脚本继续可用，而新代码从 `dws_dm_agent` 包和 `runners/adapters/` 下的适配器导入。运行配置、提示词、`.env`、审计状态和 worktree 都留在包外，升级永远不会覆盖操作者数据。

**DIAMOND Network** 面板（[http://127.0.0.1:8765/](http://127.0.0.1:8765/)）聚焦运行状态、队列、通道和 diamond-unit 状态。Metropolis 保留最初的卡片控制台布局；Diamond 使用 [http://127.0.0.1:8765/diamond-preview](http://127.0.0.1:8765/diamond-preview) 的战术拓扑视图。两个视图共享 PROJECT D.I.A.M.O.N.D. 的词汇、实时数据、功能、配色体系和主题偏好。独立的设置页 [http://127.0.0.1:8765/settings](http://127.0.0.1:8765/settings) 可以切换界面主题、独立应用内置或自建配色、检索钉钉用户以维护本地白名单，并编辑 diamond-unit、workflow 和提示词。外观只保存在当前浏览器。配置保存会做版本校验和 schema 校验，有任务活跃或排队时禁止写入，原子落盘，并对后续会话生效、无需重启。页面绝不暴露或覆盖档案的环境变量、provider 选项、认证文件、联系人 ID 或 `.env` 值。

## 运行要求

- Python 3.11 或更新
- 已登录可用的 `dws` CLI
- Git
- 当前 workflow 用到的二进制：Codex、Claude Code、Pi 或 DSH

服务本身只用 Python 标准库，不创建项目虚拟环境。

## 首次启动

### macOS

双击 `start-macos.command`。

首次运行会创建 `.env`，用 TextEdit 打开后退出。填好值后再双击一次。它会校验运行环境、安装并启动当前用户的 LaunchAgent，然后打开 [http://127.0.0.1:8765](http://127.0.0.1:8765)。

命令行生命周期：

```bash
./manage.sh doctor
./manage.sh start
./manage.sh status
./manage.sh logs
./manage.sh restart
./manage.sh stop
```

### Windows

双击 `start-windows.cmd`。

首次运行会创建 `.env`，用记事本打开后退出。配置完成后再运行一次。运行期间控制台保持打开，面板会自动打开。按 `Ctrl+C` 停止。

### 手动启动

```bash
cp .env.example .env
chmod 600 .env
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 dm_agent.py doctor
python3 dm_agent.py run --mode shadow --open-dashboard
```

包内入口等价：

```bash
PYTHONPATH=src python3 -m dws_dm_agent doctor
PYTHONPATH=src python3 -m dws_dm_agent run --mode shadow --open-dashboard
```

当 `config.json` 和 `.env` 不在源码目录下时，设置 `DWS_CHAT_AGENT_HOME`。否则服务优先使用包含 `config.json` 的源码目录，再回退到当前目录。

用已配置的线上 front 模型回放仓库内置的 front 路由用例：

```bash
PYTHONPATH=src python3 -m dws_dm_agent.front_eval --concurrency 1
```

不调用任何模型，只对单条消息跑一遍确定性粗筛：

```bash
PYTHONPATH=src python3 -m dws_dm_agent probe-gate "收到"
```

配置验证完成之前先用 `shadow` 模式。shadow 模式会做分析和审计，但不发钉钉消息。需要真正回复时把 `DWS_CHAT_AGENT_MODE` 切到 `live`。

## 命令行参考

```text
dws-chat-agent [--config PATH] [--env-file PATH] [--verbose] <command>

  run [--mode shadow|live] [--open-dashboard]   消费 DWS 事件
  doctor                                        校验本地运行环境，不发送消息
  probe-gate <text>                             对单条消息跑零 token 粗筛
```

`--mode` 只覆盖本次运行的环境模式。`--config` 和 `--env-file` 默认取解析后 home 目录下的 `config.json` 和 `.env`。

## Agent 与 workflow 配置

`config.json` 在 `agents` 下存放可复用档案，在 `workflows.presets` 下存放预设流水线。可以用面板齿轮打开 `/settings` 编辑安全的公开字段，也可以在服务停止时直接改文件。`workflows.active` 选择当前 preset。

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

preset 必须包含一个 executive 阶段（惯用名 `worker`），`front` 可选。省略 `front` 就把原始会话直接交给 worker。随仓库提供的提示词引用的服务工具（`no_reply`、`task_record`、`task_lookup`、`report`）只挂给 `pi` 类型的 Agent（`pi-rpc` 协议）。这个校验按阶段逐个进行：某个阶段或其任一 fallback Agent 配了其他驱动，而该阶段自己的提示词或共享的补充提示词（它会被 steer 进任何活阶段会话）引用了这些工具，就会在加载期直接失败。提示词不引用这些工具的阶段可以保留任意驱动——给这类阶段配 pi Agent，或者把服务工具说明从提示词里删掉。

档案把可执行启动器和会话协议分开：

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

`command` 是精确的参数数组，绝不经过 shell 执行。因此 `ccr code` 这类代理启动器、可执行文件的绝对路径、Windows 上的 `.cmd` shim，或带固定参数的 wrapper 都能直接用，不需要引号技巧。适配器会自行追加 `protocol` 所需的参数。凭据放在 `environment` 或进程登录里，永远不要写进 `command`。

`label` 是 workflow 阶段的展示文本。`options` 存放协议专属设置，`extra_args` 在适配器必需参数之后追加字面 CLI 参数。`timeout_seconds: 0` 表示不设 Agent 超时。对 Pi，`options.tools: null` 会省略 `--tools` 白名单并开放内置工具；授权边界仍由 front 提示词和 `read_only: true` 把守。

`options.extensions` 是 Pi 的扩展白名单。只要这个键存在——包括空数组——适配器就会传 `--no-extensions`，只加载列出的扩展加上自己的服务工具扩展。空数组是有意义的：它表示"不要任何第三方扩展"，正是只读 front 想要的。省略这个键则保留 Pi 的默认发现行为。

`options.mcp` 通过 `pi-mcp-adapter` 按档案挂载 MCP server。每一项可以是 catalog 名（`grafana-dev`、`grafana-prod-logging`、`hsj-atlassian`，见 `runners/mcp_catalog.py`）、覆盖 catalog 条目字段的对象，或完整的 `{command|url, args, ...}` 定义。适配器会写一份会话级 `mcp.json`，并以 `PI_MCP_CONFIG_MODE=exclusive` 传 `--mcp-config`，所以 `~/.pi/agent/mcp.json` 里的全局 server 永远进不了会话。优先用 `directTools` 配一个短列表，而不是把一个 60 工具的 server 全量声明：schema 体积每次请求都要计费。只在 `options.mcp` 里声明而 `options.extensions` 里没有对应适配器时，会在 prepare 期失败，而不是启动一个会拒绝 `--mcp-config` 的 Pi。

executive 档案上的 `fallbacks` 是一串有序的其他档案名。worker 阶段在主 Agent 失败时按顺序尝试它们；链条的锚点必须是 executive 阶段。

Pi 的会话完成等的是 `agent_settled`，不是 `agent_end`。`agent_end` 只关闭一次底层运行：之后还可能有重试、溢出恢复、压缩重试，以及排队的 steer 或后续消息，而 Pi 在什么都不剩时才报告 `agent_settled`。

提示词正文以普通 UTF-8 文件保存，当前生效的文件在 localhost 设置页上原样展示。阶段提示词可用 `{{self_name}}`、`{{agent_name}}`、`{{worker_name}}`、`{{contact_name}}`、`{{contact_alias}}`、`{{contact_user_id}}`、`{{contact_open_dingtalk_id}}`、`{{session_id}}`、`{{prior_attempt}}`、`{{workspace_root}}`、`{{worktree_root}}`、`{{skills}}`、`{{recent_context_json}}`、`{{current_messages_json}}` 和 `{{now}}`。补充提示词还会收到 `{{supplement_messages_json}}`、`{{session_id}}`、联系人身份字段和 `{{self_name}}`。

配置了 front 档案时必须设 `read_only: true`。它可以用 provider 提供的工具做有边界的只读调查，然后直接回答；任何写入或外部副作用都必须升级。worker 拥有普通编码 Agent 的权限，依据原始会话和仓库规则判断授权；升级动作本身从不授予写权限。

启用流式卡片后，服务会立即展示已接收状态。两个角色使用同一套 provider 无关的流式接口：Codex 映射推理摘要，Pi 映射原生 thinking，Claude 映射 thinking 块，自定义适配器可以发 `thinking`。服务不会编造进度标签、固定进度间隔或心跳文案。不产出原生 thinking 的模型也就不会产生虚构的进度。最终自然语言正文在投递前先落到本地 outbox，临时失败后用同一个发送 UUID 重试，并在定稿前的新鲜度检查之后替换思考卡。

### 流水线顺序

`front` 和 `worker` 是惯用槽位名，不是内置常量。preset 可以声明有序的 `pipeline`：

```json
"pipeline": ["front", "worker"]
```

各步骤严格依次执行——没有 DAG，没有并行。省略 `pipeline` 时，服务按已声明的阶段推导顺序（triage 在前，executive 在后）。能力表里没有的槽位名一律按 executive 阶段处理。阶段能力由它在 `src/dws_dm_agent/stages.py` 中的 kind 固定：

| kind | 只读 | 挂载 `escalate()` | worktree | 角色 |
| --- | --- | --- | --- | --- |
| `triage` | 是 | 是 | 否 | 调查后回答，或升级 |
| `executive` | 否 | 否 | 是 | 实施并汇报 |

要新增节点类型，给某个 kind 注册一个新的槽位名即可；运行时不存在任何针对阶段名的字符串比较。

### Agent 适配器

`runners/adapters/registry.py` 里注册了五种协议。档案可以只写 `driver`，匹配的协议会自动补上。

| 驱动 | 协议 | 图像输入 | 原生 steer | 服务工具 | 完成信号 |
| --- | --- | --- | --- | --- | --- |
| `pi` | `pi-rpc` | native | 是 | 是 | `agent_settled` |
| `codex` | `codex-app-server` | native | 是 | 否 | `turn/completed` 且 status 为 `completed` |
| `claude` | `claude-stream-json` | embedded | 是 | 否 | 流结束 |
| `dsh` | `dsh-headless-jsonl` | embedded | 否 | 否 | 最后一条 JSONL 记录 |
| custom | `custom-jsonl-v1` | embedded | 是 | 否 | 由协议自行定义 |

`claude-code` 是 `claude` 的别名。未知协议报 `unsupported agent protocol: ...`。`custom-jsonl-v1` 的 stdin/stdout 契约见 [docs/custom-agent-protocol.md](docs/custom-agent-protocol.md)。front 适配器通过 `DWS_ESCALATE_MCP_CONFIG` 拿到会话级 MCP 路径；Pi 的 escalate 是原生扩展，所以它的 front 完全不需要 MCP server。

DSH 没有原生 steer。DSH 运行期间收到补充消息时，当前进程会被停掉，带上此前的消息重新启动，因此一句短追问的代价是一次完整重跑。

### 技能

顶层 `skills` 池把名字映射到绝对路径：

```json
"skills": {
  "humanizer_zh": "/absolute/path/to/skills/humanizer-zh",
  "answer_me_with_html": "/absolute/path/to/skills/answer-me-with-html"
}
```

阶段提示词通过 `{{skills}}` 引用这个池。未知名字或非绝对路径在加载期直接失败。各适配器的落地方式不同：Pi 传 `--skill`，Claude 传 `--plugin-dir`，Codex 在会话级 home 里建符号链接，DSH 则打上 `customSkillDirs` 补丁。

### 监视

`watches` 把外部流变成普通流水线任务：

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
      "instruction": "分析此工单，结论交由服务写回原工单。保留工单状态。",
      "notify": "dm"
    },
    {
      "source": "gitlab",
      "name": "zk-ai core merge requests",
      "projects": ["xk/product/zk-ai/change-monitor"],
      "instruction": "审查此合并请求，摘要交由服务写回原 MR。"
    }
  ]
}
```

- `source` 只支持 `jira` 和 `gitlab`。不存在单独的 `sources` 或 `channels` 配置键；注册表固定在代码里。
- `watch_interval_seconds` 默认 300，下限 60。
- `watch_lookback_hours` 默认 24。
- 每条规则独立保存轮询位置。事件按来源资源和规则目标去重，同一工单的「修复」和「客户说明」可以分别验收。
- `notify` 取 `dm`（默认）或 `none`。获准的终态私聊和原渠道评论各有收件目标、动作 ID 和回执。评论已写入、私聊待重试时，看板分别显示。远端结果不明时，服务按原动作标记核对。
- Jira 和 GitLab 沿用已配置的工作流、项目工具、shell 和备用 Agent。worker 自主选择调查、修改和测试的方法，最终评论由服务写回。
- GitLab 规则可用 `merge_targets` 为指定项目开启条件合并，默认空；目标分支由 `GITLAB_MERGE_TARGETS`（逗号分隔，默认 `dev,main`，如 `test,dev,main`）限定。worker 阅读完整差异和讨论，说明为什么这次改动小且集中。服务提交前复核授权分支、审过的 SHA、对应 CI 和阻塞状态。合并有独立回执。

### 恢复与等待

服务从本机审计库恢复已接收的输入，旧记录缺少正文时查原会话历史。对失去执行者的 Pi 运行，服务核对最后一轮 `agent_settled`、原始输入和授权、已有回执、人工接管及后续补充。只剩摘要且没有投递记录的旧终稿交给操作者核实。看板显示最早排队的输入、失去执行者的运行以及各目标投递回执。

投递回执里出现「待核实远端回执」（`remote_uncertain`）= 写请求已发往远端但没有回执，成功与失败都无法证明。服务绝不自动重发此类行（防重复投递），Jira/GitLab 评论与合并会持续用只读 GET 对账，查到回执即自动转「已确认」。没有对账能力的通道（如私聊）会一直留在回执条里，理由码以 `needs_operator_reconciliation: <detail>` 标注原因：按行内联系人/远端 ID 去对应系统人工核对一眼即可，确认送达或确认不送达都无需在面板操作——行不会重发，留着只是提醒。重试仅在明确失败时发生，且受 24 小时幂等窗保护（窗内重发沿用同动作 ID 去重，窗外宁可转待核实也不冒险重发）。

同一条消息可以为多个事项提供证据，每个事项独立更新版本和验收状态。按期提醒绑定原事项、原会话及等待条件。两天未活跃的事项退出提示词后，定时调度继续。操作者可以把已接收的其他来源关联到事项，关联仅登记来源，原会话的正文和写权限继续由各自受众控制。

跨组织联系人：钉钉对组织外的用户区别对待。列出他们的消息历史需要临时数据授权（`dws chat data-auth cross-org --all --grant-type timed --ttl 24h`）。授权约 24 小时后过期；缺失期间历史对账会失败，投递会被推迟到授权续上为止。跨组织用户的 `userId` 常常不能用于列消息（`peerUid is required`），因此服务在可用时回退到 `{{contact_open_dingtalk_id}}`。

## 面板 API

面板服务绑定 `127.0.0.1:8765`，提供：

| 路径 | 用途 |
| --- | --- |
| `/` | 运行看板 |
| `/diamond-preview` | 拓扑视图 |
| `/settings` | 主题、白名单、workflow、提示词 |
| `/api/snapshot` | 当前队列与阶段状态 |
| `/api/board` | 看板行与运行历史 |
| `/api/config` (GET/POST) | 读取或保存公开配置 |
| `/api/contacts/search` | 白名单用的钉钉用户检索 |
| `/api/session/new` | 开启一个干净会话 |
| `/api/task/answer` | 回答待处理的任务提问 |
| `/api/task/link-source` (POST) | 操作者用 `task_id` 与 `message_id` 显式关联已接收的来源 |

## 安全边界

- 一个小的确定性粗筛只负责丢弃空确认和限制消息体量；所有语义层面的安全判断都留在模型里。`probe-gate` 单独跑这道闸，便于检查。
- 仓库的 `AGENTS.md` 和编码 Agent 负责项目特定的修改、审查、测试和发布策略。
- 服务会在确认和最终回复前重新检查会话。
- 自动钉钉消息保留平台 AI 标记。

运行状态、日志、审计数据和临时会话保存在 `state/`，不会提交。`.env` 和 `.env.*` 一律忽略，只有 `.env.example` 例外。

## 文档

| 文件 | 内容 |
| --- | --- |
| [docs/agent-config.md](docs/agent-config.md) | 编码 Agent 可照做的安装与配置指南 |
| [docs/custom-agent-protocol.md](docs/custom-agent-protocol.md) | `custom-jsonl-v1` 的 stdin/stdout 契约 |
| [docs/target-architecture-20261106.md](docs/target-architecture-20261106.md) | 架构总览与未决问题 |
| [docs/latency-and-streaming-plan.md](docs/latency-and-streaming-plan.md) | 延迟测量与流式设计 |
| [docs/no-reply-task-memory-plan-20261012.md](docs/no-reply-task-memory-plan-20261012.md) | 静默回复与任务记忆设计 |
| [docs/audit-findings-20261107.md](docs/audit-findings-20261107.md) | 完整审计发现，作为工作清单保留 |
