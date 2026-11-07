# dws-chat-agent 审计问题清单（2026-11-07）

本文档是当日全面审计的沉淀，可当作工作清单交给任何没有本项目背景的同事。每一节先交代背景，再列问题；每个未修条目都标了期望的修复方向（标【需决策】的条目附了推荐方案，但需要项目负责人确认）。

---

## 〇、系统背景与术语表

### 系统是什么

dws-chat-agent 是一个钉钉 DM 聊天代理服务：监听钉钉私信，把消息交给 LLM agent 处理，再把回复发回联系人。处理分两个阶段（stage）：

- **front（前置）**：只读的快速分诊模型，判断消息要不要处理、要不要升级给 worker。不执行写操作。
- **worker（执行）**：真正干活的模型，可执行命令、调 MCP 工具、发文件。

一个 **preset** 是一套"front + worker + 补充消息策略"的组合配置。本项目 config.json 里共 10 个 preset：1 个生产在用（active，名为 `dsh-default`）+ 9 个备用（切换不同的 LLM 渠道用）。

### 四个 worker 运行时（渠道）

worker 阶段可以用四种 CLI 后端之一，各有独立的适配器层：

| 名字 | 是什么 | 服务工具挂载方式 |
|---|---|---|
| pi | pi CLI（RPC 模式） | 原生扩展（pi_dws_tools.ts） |
| claude | Claude Code（stream-json） | MCP server（--mcp-config） |
| codex | Codex（app-server） | MCP server（-c mcp_servers 注入） |
| dsh | dsh CLI（headless NDJSON） | MCP server（profile patch yml） |

### 术语表

| 术语 | 含义 |
|---|---|
| **escalate** | front 阶段的转交动作：front 判定自己不该处理，把请求交给 worker。是一个零参数工具。 |
| **服务工具** | dws 服务注入给模型的四个内置工具：`no_reply`（本轮不发消息）、`report`（进度里程碑/最终正文/阻塞上报）、`task_record` / `task_lookup`（长期事项的写与读）。 |
| **engram** | 一个团队记忆服务（MCP server），模型可查历史记忆。 |
| **事项 / 黑板** | "事项"是跨轮次记住的长期任务条目（goal、状态、证据、facts）；"黑板"是全部事项的集合，面板可看。 |
| **facts** | 事项下挂的事实列表，每条是 {text, anchor_message_id}。 |
| **shared.md 拼接** | service.py 渲染 worker 提示词时，无条件把 `prompts/shared.md`（身份/信任/输出契约）拼在阶段模板（worker.md 或 worker-dsh.md）前面。 |
| **supplement** | 联系人在 agent 处理期间补充发来的消息。两种消化策略：`steer`（运行中注入正在跑的会话）和 `restart_with_context`（重启会话并带上新消息——dsh 协议不支持运行中注入，只能用这种）。 |
| **origin（信封）** | 每条输入事件的来源标注：`dm`（联系人私信）、`wake`（定时唤醒）、`panel`（面板操作）、`operator`（操作者本人指令）。"信封"指这套来源标注机制。 |
| **@时刻** | 事项的 waiting_on 字段可写 `@2026-11-08 09:00` 这样的时刻值，服务会到点重新唤醒 worker，称"时刻唤醒"。与之相对，"任务唤醒"是别的事项终态触发的唤醒。 |
| **收口** | 一轮处理结束时的落定动作（发正文 / 沉默 / 报错 / 阻塞）。 |
| **交接协议** | 组件间传结果的机制。如 `DWS_REPORT_FILE`：report 工具把 {kind, text} 写进这个文件，runtime 读文件按终稿收口会话。 |
| **preset / stage / MCP** | preset 与 stage 见上文；MCP 是 Model Context Protocol，挂外部工具的标准协议。 |

### 状态标记（全文统一用这四种）

- **【已修】**：当日已改完，改动在本地工作区**尚未提交**（见第五节末尾的提交指引）。
- **【部分修复】**：主因已消除，但派生问题仍在（条目里会写清哪半没修）。
- **【未修】**：原样存在。
- **【搁置】**：有意不修，附理由。

### 审计边界

审计范围：工具配置错误、前后矛盾的提示词、不利于横向扩展的代码、胶水代码、魔法数字。**不含** agent 安全类问题（提示词注入、越权、沙箱逃逸——另一类课题，本次不审）。

---

## 一、工具与配置错误

### 1.1【已修】escalate_mcp.py 路径算错一级

`src/dws_dm_agent/runners/adapters/codex.py` 与 `adapters/claude.py` 原来写 `Path(__file__).parents[2] / "escalate_mcp.py"`。`parents[2]` 从 adapters/ 目录出发解析到 `src/dws_dm_agent/escalate_mcp.py`——文件不存在；实际位置是 `src/dws_dm_agent/runners/escalate_mcp.py`。

后果：名为 `claude-front` 的 agent 配置（注意层级：agent 配置 < preset，preset 的 front 槽位引用某个 agent 配置名）一旦被某个 preset 用作 front，它写的 mcp.json 里 escalate server 的 command 指向不存在的脚本，模型调 escalate 工具时 stdio server 启动失败，front 的转交通路整体不可用。当前 10 个 preset 的 front 都用 pi-front，所以没爆，属潜伏炸点。

修复：路径收编为 `mcp_catalog.escalate_script_path()` 单点函数（见第五节第 5 条）。

### 1.2【已修】.env.example 缺 AIMODEL_TENCENT_DS_API_KEY

`deploy/dsh/dws-worker.patch.yml`（dsh 渠道的配置补丁）引用环境变量 `AIMODEL_TENCENT_DS_API_KEY`。实际 `.env` 有这个键、`.env.example` 没有。照 example 初始化的新环境，dsh worker 的每次 LLM 调用都因缺 key 失败。已补进 example。

**顺带发现的另一个独立小问题（未修，见 P2 表）**：`.env` 里的 `DWS_CHAT_AGENT_WRITABLE_ROOTS_JSON` 在全仓库没有任何代码读取。处置【需决策，推荐：删除该键】——没有规划中的消费方，留着只会误导下一个人去配它。

### 1.3【未修】dashboard 协议映射副本漏 dsh

`src/dws_dm_agent/dashboard.py:38-43` 的 `_DEFAULT_PROTOCOLS` 字典手工抄了一份"CLI driver 名 → 协议名"映射，但缺 `"dsh": "dsh-headless-jsonl"` 一行。面板上 dsh-worker 的 protocol 字段显示为空串。

权威清单在 `src/dws_dm_agent/runners/adapters/registry.py` 的 `DRIVER_PROTOCOLS`（registry.py 的模块注释里承诺"runtime / service / dashboard 全部从这里读协议清单"——dashboard 没有遵守，所以说"承诺已被证伪"）。

修法：dashboard 删掉私有字典，改 `from .runners.adapters import DRIVER_PROTOCOLS`。这是全文最便宜的修复。

### 1.4【未修】死代码尸体

`src/dws_dm_agent/service.py:1484-1502`：`return _BatchOutcome("deliver", ...)` 之后跟着一个裸 `return` 和整段复制体——这段是从 `_finish_max_replans` 方法（1285-1301 行）抄来的旧函数体，缩进也没调。背景：replan 指"联系人补充消息太多，重启一轮处理"的机制；这段死代码是提取方法时留下的尾巴。

后果：同一份"补充消息过多"收口文案现存两份，将来改文案或状态值只改一处就会出现行为分叉。行动：直接删除 1485-1502 行（`return` 之后全部不可达）。

### 1.5【未修】dws.py 文件投递用被弃用的 CLI 旧别名

背景：`dws` 是外部的钉钉 CLI 工具（不在本仓库），dws.py 封装它的子进程调用。它的 `send_file` 命令有两组等价 flag：`--file` / `--idempotency-key`（当前文档化的正式名）和 `--file-path` / `--uuid`（未文档化的旧别名，暂时向后兼容）。

`src/dws_dm_agent/dws.py:341-348` 的 `send_file` 用的正是旧别名；而 `prompts/worker.md:64` 教模型用新名。CLI 属外部工具、由他人维护，旧别名何时移除不受本仓库控制——一旦移除，服务自身的文件投递链路先坏。

行动：dws.py 换成 `--file` / `--idempotency-key`，跑一次带附件的投递验证。

### 1.6【部分修复】四 worker 工具/MCP 不对等

修复前的差距：codex-worker 与 claude-worker 完全没有 MCP 外部工具、没有任务工具；pi-worker 缺 engram；dsh-worker 只有 engram 和 bash 形态的 task 工具。当日已落地对齐（见第五节 1-4 条），现在四个 worker 的工具面一致：服务工具四件 + grafana×2 + jira + engram。

残留缺口【搁置】：codex 渠道挂不了 engram。机制：engram 是 http 形态的 MCP server，codex 客户端只支持 stdio 形态，注入时会被跳过并打一条 warning 日志。这不是 bug 而是上游能力缺口——codex CLI 客户端支持 http MCP 后，删掉 `runners/codex.py` 注入循环里的跳过分支即可自动覆盖。跟踪方式：升级 codex 后查其 MCP 配置文档。

---

## 二、前后矛盾的提示词

背景补充：生产 active preset 是 `dsh-default`，worker 用 dsh 渠道，提示词是 `prompts/worker-dsh.md`；其余 preset 的 worker 用 pi/claude/codex 渠道，提示词都是 `prompts/worker.md`。所有 worker 提示词 = shared.md（前置拼接）+ 阶段模板。front 提示词是 `prompts/front.md`（不拼 shared.md）。

### 2.1【部分修复】shared.md 的 report/no_reply 教学 vs dsh 无此工具

`prompts/shared.md:28-34` 教 `no_reply()` 与 `report(progress)` 的用法。修复前 dsh 渠道没有这两个工具（适配器声明的 `service_tools = False`，即"该协议能挂载服务工具"的能力位为否），同一份 prompt 里 shared 教"怎么用 report"、worker-dsh 教"没有 report"。

当日 dws-tools MCP 落地后，dsh/claude/codex 都有了同名工具，矛盾的根源已消除。

**没修完的两半**：

1. `worker-dsh.md` 的 `<closing>` 段仍写"确实无需传达时给一句简短、诚实的收尾"——这是无工具时代的旧措辞（那时 dsh 无法沉默，只能发一条收尾）。现在有真的 no_reply 工具了，应改回与 worker.md 的 `<silence>` 段一致的语义：无需传达就调 no_reply()。改时注意 2.6 提到的重复问题，别只改一处。
2. 校验盲区：`runtime.py:872-875` 的服务工具校验（检查"提示词里引用的服务工具，实际执行的 agent 是否有能力挂载"，不满足则启动报错）只扫阶段模板和 supplement 文件，**不扫 shared.md**。本次矛盾能存在半年就是因为它绕过了校验。修法：校验循环里把 shared.md 的文本也并入 `referenced` 集合（工具名命中 `_SERVICE_TOOL_NAMES` 即计入）。

### 2.2【未修，需决策】supplement-dsh.md 是死提示词

`prompts/supplement-dsh.md` 是给 dsh 渠道准备的"补充消息消化"提示词。但 `dsh-default` preset 配的策略是 `restart_with_context`（重启会话带新消息），而这份文件只在 `steer` 策略的代码路径里被渲染。结论：它配置了、存在，但从未进入任何会话。

决策依据：dsh 协议不支持运行中注入（steer），restart 是协议唯一选择，所以"改策略激活文件"不可行。**推荐：删除 supplement-dsh.md，并从 dsh-default preset 里移除 `supplement_prompt` 字段**。它的有用内容（"新消息并入当前任务"一句）已经在 worker-dsh.md 的 `<execution>` 段里了。

### 2.3【未修】"唤醒上限 3 次后挂起"承诺以偏概全

`prompts/worker-dsh.md:28`："否则系统会反复唤醒同一事项（上限 3 次后挂起等人）"。实际机制：`_TIME_WAKE_MAX_DISPATCHES = 3` 只作用于**时刻唤醒**（waiting_on=@时刻 的扫描分支）；**任务唤醒**由别的任务终态事件触发一次，没有 3 次上限。worker.md（pi 渠道版）没有这句承诺。

行动：措辞改为"时刻唤醒同一事项最多 3 次后挂起等人"。worker.md 不用动。

### 2.4【未修】带图消息指令自相矛盾

dsh 渠道处理图片的方式（embed 模式）：图片下载到本地，**路径写进 prompt 文本**，模型需要用文件读取工具逐个打开。服务端注入的说明是对的："请先用 read 工具逐个读取"。

但 `prompts/worker-dsh.md:11` 写："图片直接查看，不要为看图调用工具"——这是从 pi 渠道复制来的措辞（pi 的 native 模式下图片真随消息原生附带、模型直接可看），对 dsh 是错误指令。同一条消息里两段系统级指令方向相反，模型靠位置先后猜。

**期望行为以服务端注入文案为准**。行动：删掉 worker-dsh.md:11 的这句（保留"图片会作为附件发给你"的事实陈述即可）。

### 2.5【未修】shared.md 空头引用 `<board>` 节

两个事实链：

1. `prompts/shared.md:17` 写："唯一例外见阶段提示词 `<board>` 节"。shared.md 只拼给 worker，worker.md 确实有 `<board>` 节——所以 worker 渠道这句是通的。问题在维护层面：shared.md 是四渠道共享的身份与契约基座，维护者会把它当跨渠道契约文档来读和改；front.md 没有 `<board>` 节，这句引用在 front 渠道的对应物上落空，改提示词的人无从知道 front 侧该不该有这个例外。
2. front 渠道会收到 `<system_wake>` 合成输入块（服务拼接时对 front/worker 无差别），其中含 disclaimer："系统标注为操作者指令的输入出自本人，按最高授权执行"。但 front.md 的 `<tool_bounds>` 段（工具边界禁令）禁止 front 外发消息。操作者本人从前台通道下指令时，front 会按禁令拒绝或走 escalate 升级——与 worker 通道的"按最高授权执行"语义相反。

修法【需决策，推荐：给 front.md 补一个两行的 `<board>` 节】，内容说明"操作者指令 = 本人指令，按最高授权执行，但仍受 tool_bounds 的外发限制（需要外发时 escalate）"。另一选项是删 shared.md 的引用改写成条件句，但那会让 worker 渠道失去这条豁免说明，不推荐。

### 2.6【未修】`<override>` 段与 silence 判据多处重复

- `<override>` 段（最高优先级声明）在 shared.md:1-4 与 front.md:1-4 逐字重复。改一处漏一处。
- `<silence>` 判据（"对方纯致谢/只通知部署完成/确认已定稿结论时应当沉默"那五条）在 front.md:41-47、worker.md:18-24、front.md:19（direct_reply 段又复述一遍）、supplement.md:6 共四处近乎逐字重复。
- worker-dsh.md 没有对应段（用 `<closing>` 替代沉默语义）——这是刻意的协议适配，但提示词和代码注释里都没有说明，后续维护者会误当遗漏去"统一"。

行动：认领一个单点（建议 shared.md 或 prompts.py 的组装函数）收编 silence 判据和 override 文本，其余文件删除或改为引用。重复清除前先解决 2.1 的语义统一，否则收编的是两个互相矛盾的版本。

### 2.7【未修】worker-dsh 的 task_memory 信息量低于 worker

worker.md:54 写明"8 条 facts / 12 个证据 ID / 12 个资源标签会被静默截断"（与 `task_tools.py:36-38` 的三个上限常量吻合）+ restore 参数说明；worker-dsh.md:27-30 全无。两个渠道共用同一个后端（同样的截断发生），dsh 的模型却不知情，被截断后会困惑"为什么 facts 少了"。

行动：把截断上限和 restore 说明抄进 worker-dsh.md 的 `<task_tools>` 段。注意上限数字必须与 task_tools.py 的常量同步——更好的做法是这三个数字经 prompts.py 的模板变量注入，不再手写。

### 2.8【未修】提示词杂项

1. worker.md:36 / worker-dsh.md:29 硬编码示例日期 `@2026-11-08 09:00`。写作当天看像"明天"，长期运行后变成过时日期，模型可能误读。行动：改成 `@<日期> <HH:MM>` 占位写法。
2. "有限使用 `--user` ..."（worker.md:12，worker-dsh.md:12 同）疑为"优先使用"的笔误——语义显然是"优先用 user_id，为空再用 open-dingtalk-id"。两份同错说明是复制源头的笔误。行动：改"优先"。
3. 时间窗语义"09:00-19:00 含头不含尾"只写在 worker.md，worker-dsh.md:29 只写"时间窗 09:00-19:00，窗外顺延"。19:00 整点进来的唤醒在 dsh 版描述下会被当成窗口内，实际顺延到次日。行动：补齐同样的边界语义。
4. `prompts.py:47-50` 的"结构标签失效化"（把联系人消息里伪造的结构标签名打上全角变形，防注入）用的正则只覆盖 `untrusted_*` / `system_wake` / `override` / `conflict-rule` 四类，而实际提示词里的结构标签共有 22 个（含 `<board>` `<execution>` `<task_memory>` 等）。联系人消息里伪造 `</board>` 这类闭合标签不会被失效化。行动：把正则改为覆盖全部实际标签名（从各模板文件动态收集或维护一份清单常量）。

---

## 三、不利于横向扩展

### 3.1【未修，缓做】front/worker 二阶段假设横穿全栈

runtime.py 的注释宣称"stage 名配置驱动，preset 里写什么键就有什么 step"，但代码里 front/worker 字符串硬编码共 11 处：service.py 四处（`has_stage("front")` 探测、布尔参数映射回固定 stage 名、shared.md 只拼 worker 的条件、worker fallback 链）、runtime.py 三处（escalate 只挂 front、codex 隔离 home 只给 front、worktree 只给 worker）、dashboard.py 三处（三个 `("front", "worker")` 元组循环，面板编辑面不认其他 stage 名）、board.js 一处（PHASES 阶段条）。

新增一个 stage（比如"verifier"）名义上只需写 config，实际要改 5 个文件 10+ 处。行动：缓做。每次为其他条目触碰这些文件时顺手把硬编码换成配置驱动，不专门立项。

### 3.2【未修】协议字符串分叉未收编完

适配器注册表只声明了三个能力位（images / native_steer / service_tools），但还有 8 处代码直接比较协议名字符串：service.py 三处（图片说明注入、多模态模型 override、task_context 注入条件）、runtime.py 四处（图片处理、codex home×2）、dashboard 一处（即 1.3 的协议映射副本）。

另：`runtime.py:1136` 的 `describe()` 输出里 `"nativeSteer": True` 对所有协议硬编码 True——而 native_steer 的含义是"该协议支持运行中注入补充消息"（dsh 明明是 False）。面板展示的 steer 能力实际另有来源，这个字段是恒真的误导。

行动：给能力位集合补齐（如 `native_multimodal`、`task_context_env`），8 处字符串比较改查能力位；describe() 的假字段删除或改读真实能力。验收标准：新增一个协议时，service/runtime 不需要新增任何协议名字符串。

### 3.3【未修，缓做】新增 origin 无单点策略

背景见术语表"origin（信封）"。现状：operator 这个来源在信封机制里有、在黑板 origin 映射里没有——`service.py` 的映射只二分 `origin == "wake"`，operator 轮被记成 live（普通联系人轮）。如果这是刻意的设计，代码里没有注释说明。

新增一个触发通道（如 webhook）需要触碰 core.py 两处、service.py 约 8 处分散判定。本质问题：四个正交问题——"这个 origin 是不是联系人消息 / 带不带新授权 / 能不能外发 / 终态要不要回声唤醒等待方"——散落在 8 处回答。

行动：缓做。方向是收敛成单个 OriginPolicy 配置对象（每个 origin 一行，四个布尔/行为字段），8 处判定改查策略对象。先在 core.py 给 operator 记 live 补一条注释说明是否有意。

### 3.4【未修】phase 文案前后端双维护

背景：phase 是面板显示的处理阶段名（stabilizing/screening/routing 等九个）。同一 phase 的中文文案存在三份：service.py:841-851 的 labels 字典（写入 runtime.json 的 `phaseLabel` 字段）、service.py 里 6 个调用点的裸字符串、board.js:29-33 的 PHASES 数组（前端自己再翻一遍，不读后端已写好的 phaseLabel）。

行动：board.js 改读 runtime.json 的 phaseLabel；service.py 的裸字符串收敛到 labels 字典单点。同类问题：黑板终态映射有两张字符串翻译表（service.py 的 `_BOARD_CLOSE_OUTCOME` 与 core.py 的 `BOARD_RUN_OUTCOME_STATUS`），两表的中间词汇必须人工互锁，应合并或加测试锁住。

### 3.5【未修，缓做】字段命名两套写法无契约

同层 JSON 输出 snake_case（recent_context 的 message_id/conversation_id）与 camelCase（dashboard 快照的 contactName/batchSize）混用；前端 board.js 出现 `recent_facts ?? facts` 双字段名兜底、`_taskId` / `dataset.dwsTaskId` 双通道传同一个 id；state/ 下的 JSON 读写全部裸 `.get()`，没有键清单。

行动：缓做。方向：写一页字段命名规约（对外 camel、对内 snake 的分界），新增字段先查规约；存量双写法按"触碰时迁移"清理。

### 3.6【未修，缓做】超长函数

超过 100 行的函数有 11 个，最大的三个：`DashboardServer.start` 392 行（含嵌套 Handler 类）、`AgentService._run_agent` 378 行（会话生命周期、租约、轮询、steer、图片接入九件事共享局部变量闭包，无法单测其中一段）、`AuditStore._migrate` 227 行。core.py 的 AuditStore 类 2400 行，黑板（事项运行轨迹）与审计（事件/投递）两套生命周期共用一个类。

行动：缓做，触碰时拆。_run_agent 优先——它挡住了服务层一半路径的单测。

---

## 四、魔法数字与胶水

### 4.1【未修】唤醒窗口三处硬编码联动

可打扰时间窗 09:00-19:00 同时硬编码在：service.py:81-82 的 `time(9,0)/time(19,0)` 常量、`_roll_into_wake_window` 的窗口滚动逻辑、两份 worker 提示词的"时间窗 09:00-19:00"文案。改窗口要人肉同步三处（其中提示词文案两份，实际四处）。

行动：时间窗进 config（config.py 已有 quiet_window 的 Settings property 先例可仿）；提示词经模板变量注入。与 2.8 第 3 条相关（窗口边界语义也在提示词里）。

### 4.2【未修，需决策】投递无限重试

service.py:2714 的投递退避是 `min(2**attempt_count, 60)` 秒——有封顶间隔，但**没有最大重试次数**：attempt_count 无限涨，永远失败的投递（如对方账号已注销）会以 60s 间隔重试到天荒地老。也没有测试约束这个行为。

决策点：重试耗尽后做什么。**推荐：上限 20 次（约 24h 内退避完），耗尽后标记 `failed_permanent` 状态并在面板可见，写一条 warning 日志**。不推荐直接丢弃——投递失败对用户是丢消息，需要留痕可查。

### 4.3【未修】同值多副本（魔法数字）

同一业务数字在多处独立写死，改一处漏其他：

| 数字 | 含义 | 出现位置 |
|---|---|---|
| `12000` | 单条消息字符上限 | service.py:375、service.py:3169、dashboard.py:64 三处字面量 |
| `60` | agent 超时秒数默认值 | runtime.py:748、dashboard.py:166、dashboard.py:376 三处 |
| `8` | 活跃事项快照条数 | service.py:76 常量与 core.py:1636 函数默认参两处定义 |
| `3` | 每条事项随快照带的 facts 条数 | core.py:1740 与 board.js:398 前后端各一份 |
| `400` / `30` / `6` | 面板实时事件流的内存保留条数 / API 投影条数 / 前端展示条数 | runtime.py:54、service.py:3006、board.js:483 三个数字无单点 |

行动：各自收敛为单点常量或 config 键；前后端共享的（3、6）经 runtime.json 传递。

### 4.4【未修】dws.py 魔法字符串/数字

- 三处 `str(exc).endswith(": NETWORK_ERROR")`：靠错误消息的**尾部字符串**识别网络错误来决定重试/降级。错误消息措辞或拼接格式一变，重试和 user_id 降级全部静默失效。行动：`_json_command` 抛错时结构化携带 error_code，匹配改查字段。
- 超时族：默认 45s、download_media 90s、send_file 180s、进程 kill 宽限 5s，全部内联数字。行动：提为模块级具名常量。
- emoji `"专注"` 作为消息状态标记（发消息时钉钉卡片上的专注表情）硬编码在 dws.py:304、321 两处默认参。行动：提为常量并允许 config 覆盖。

### 4.5【未修，缓做】胶水代码清单

- `except Exception → LOG.warning(sanitize_text(str(exc), 300))` 包装模式约 10 处，无统一 helper。行动：提 `_log_warn(exc)` 之类。
- `streaming_card_send_uuids` 属性的防御性初始化抄 4 遍（为直写 DB 的进程恢复兜底）。行动：提为方法。
- 补充消息排序 lambda `key=lambda item: item.created_at` 抄 8 遍。
- 联系人查找 O(n) 线性 `next(...)` 三处热路径，无 alias→Contact 的 dict 索引。
- service.py:702-752 重新拼了一遍 dws CLI 的联系人搜索命令（`DwsClient._json_command` 已有同样逻辑），还 import 了 dws.py 的私有函数 `_find_dws_error_code`。行动：下沉为 DwsClient 方法。
- dashboard.py:601-614 用 `getattr(probe, "__self__")` 从回调绑定方法反查 AgentService 实例（构造注入缺失的兜底）。后果：回调改成 partial/staticmethod/独立函数，黑板 API 静默 503。行动：改构造函数显式注入。
- 5 个适配器文件全部 `import sys` 不使用、还在 `__all__` 里导出。行动：删除。

### 4.6【未修】测试盲区

- 监听主循环（钉钉事件的长连接消费与重连退避，service.py:903-953）零测试。
- `restart_with_context` 策略（dsh 生产实际走的补充消息消化路径）没有任何 service 级测试——现有用例全部测 steer 路径。
- `front_eval`（评测 front 路由正确率的脚本，`src/dws_dm_agent/front_eval.py`）手工复刻了一份提示词变量 dict，漏了 4 个变量：front 提示词一旦引用它们，评测脚本直接 KeyError。注意：这是**评测脚本的 bug**，不影响生产服务（生产不走 front_eval）；但它让评测集静默失效。
- 投递重试上限无测试（对应 4.2）。

行动：优先补 restart_with_context 的 service 级用例（生产路径无守护最危险）；front_eval 的变量 dict 改 import prompts.py 的权威定义。

---

## 五、修复记录（2026-11-07 当日已落地）

对应关系：第 1-4 条 ↔ 问题 1.6；第 5 条 ↔ 1.1；第 6 条 ↔ 1.2；第 7 条是验证。

1. 新建 `src/dws_dm_agent/runners/dws_tools_mcp.py`：通用 stdio MCP server（server 名 `dws-tools`），暴露 no_reply / report / task_record / task_lookup 四个工具，与 pi 的原生扩展同名、走同一后端——task 三件直接调 task_tools.execute_request（同校验、同幂等收据），report 的 final/blocked 写 `DWS_REPORT_FILE` 走既有交接协议，no_reply 由事件接线捕获。
2. 三个适配器接线：claude/codex 的 `mcp_config()` 增加 worker 分支（目录 server + dws-tools 写进 session mcp.json）；codex 逐键转 `-c mcp_servers.*` 命令行注入；dsh 经 patch yml 加 stdio MCP 条目（`DWS_TOOLS_PYTHON/DWS_TOOLS_SCRIPT` 经环境变量注入）。事件面：三个协议的 parse_event 都接了 no_reply→沉默收口、report→读 DWS_REPORT_FILE 收口。
3. config 对齐：codex-worker / claude-worker / dsh-worker 三个 agent 配置挂同一套 MCP（grafana×2 + jira + engram）+ `service_tools_mcp: true`；pi 全家族 worker 补上 engram。注意：codex 的 engram 是"配置里写了、注入时因协议不支持 http 而跳过"，见 1.6 残留说明。
4. 启动校验放行：runtime 的服务工具校验新增 `options.service_tools_mcp` 条件。修复前 codex-default / claude-default 两个 preset 一切换就启动报错（worker.md 引用了 no_reply/task_record 而这两个 worker 无挂载能力）；修复后四个渠道各自的默认 preset——dsh-default（active）、codex-default、claude-default、pi-default——启动校验全部通过。其余 preset 共享这四个 worker 配置，同样受益。
5. escalate_mcp 路径收编为 `mcp_catalog.escalate_script_path()` 单点函数。
6. `.env.example` 补 `AIMODEL_TENCENT_DS_API_KEY`。
7. 验证：全套测试 327 用例全绿（新增 tests/test_dws_tools_mcp.py 12 个）；四 preset 的 prepare 级装配（命令行参数、mcp.json、环境变量）实测符合预期。dsh 真模型端到端未跑（需等生产轮次），claude 真机端到端受 OAuth 过期限制。

**未提交改动的交接说明**：以上修复全部在本地工作区，未 commit、未 push。涉及文件：`.env.example`、`config.example.json`、`deploy/dsh/dws-worker.patch.yml`、`src/dws_dm_agent/runners/{mcp_catalog,codex,runtime,dws_tools_mcp}.py`、`src/dws_dm_agent/runners/adapters/{claude,codex,dsh}.py`、新增 `tests/test_dws_tools_mcp.py`。另 `config.json`（gitignored）与 `.env`（gitignored）有对应改动。提交前跑 `.venv/bin/python -m pytest tests/ -q`，预期 327 passed。

---

## 六、修复优先级总表（覆盖全部未修项，无遗漏）

### P0 零成本立即修（一次提交可全带）

| 条目 | 动作 |
|---|---|
| 1.4 | 删 service.py:1485-1502 死代码 |
| 1.3 | dashboard 改 import DRIVER_PROTOCOLS |
| 1.5 | dws.py send_file 换 `--file` / `--idempotency-key` |
| 2.8-1 | 示例日期占位符化 |
| 2.8-2 | "有限使用"改"优先使用" |
| 2.8-3 | worker-dsh 补窗口边界语义 |
| 4.5 末条 | 删 5 个适配器的死 `import sys` |

### P1 提示词收口（批复后一次落地）

| 条目 | 动作 |
|---|---|
| 2.1 前半 | worker-dsh `<closing>` 改 no_reply 沉默语义 |
| 2.2 | 删 supplement-dsh.md + 移除 preset 引用【需决策，推荐已给】 |
| 2.3 | 措辞改"时刻唤醒" |
| 2.4 | 删 worker-dsh.md:11 图片指令句 |
| 2.6 | silence 判据 / override 文本收编单点 |
| 2.7 | 截断上限与 restore 说明补进 worker-dsh（数字走模板变量） |
| 2.5 | front.md 补 `<board>` 节【需决策，推荐已给】 |
| 2.8-4 | 失效化正则覆盖全部结构标签 |

### P2 结构止血（小步快跑）

| 条目 | 动作 |
|---|---|
| 3.4 | board.js 改读 phaseLabel；labels 单点化 |
| 3.2 | 能力位补齐，8 处协议字符串改查能力；describe 假字段清理 |
| 2.1 后半 | 校验收编 shared.md 文本 |
| 4.1 | 唤醒窗口进 config + 模板变量 |
| 4.2 | 投递重试上限 20 次 + failed_permanent 状态【需决策，推荐已给】 |
| 4.3 | 魔法数字各自单点化 |
| 4.6 | 补 restart_with_context 测试；front_eval 变量 dict 改 import 权威定义；监听主循环补重连退避用例 |
| 1.2 残留 | 删 .env 的 WRITABLE_ROOTS_JSON 死键【需决策，推荐已给】 |

注：4.6 的"投递重试上限无测试"不单列——4.2 落地时随修复一起补。

### P3 缓做（触碰相关文件时顺手收，不立项）

| 条目 | 动作 |
|---|---|
| 3.1 | stage 名配置驱动补全（11 处硬编码） |
| 3.3 | OriginPolicy 单点化；先给 operator→live 补注释 |
| 3.5 | 字段命名规约页 + 存量迁移 |
| 3.6 | 超长函数拆分（_run_agent 优先） |
| 4.4 | dws.py 错误码结构化、超时常量化、emoji 常量化 |
| 4.5 | 胶水清理：异常 helper、防御初始化方法化、排序 lambda 收编、联系人 dict 索引、CLI 拼装下沉、dashboard 构造注入 |

### 搁置（有意不做）

| 条目 | 理由 |
|---|---|
| 1.6 残留（codex 挂不了 engram） | 上游 codex 客户端不支持 http MCP；支持后删 runners/codex.py 跳过分支即可 |
