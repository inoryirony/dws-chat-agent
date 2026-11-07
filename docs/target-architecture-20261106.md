# 目标架构总览（2026-11-06 盘定稿，持续修订）

来源：与操作者逐轮推演。对标 `diamond-superagent`（dws-chat-agent 直系后代，其皮肤层——卡片/表情/噪音过滤/steer——本就源自本项目，反向要借的是它的骨架：Ticket/Assignment/Subscription/Hold/工作小时）。

---

## 0. 定位

单操作者钉钉私聊自主代理。**worker-only**（front 已关：`pi-deepseek` preset 无 `front` 键，`runtime.py:905` 允许缺省）。

front 关闭理由：噪音过滤（`_ACK_TEXT`/`_ONLY_REACTION`）在模型前拦截闲聊；`no_reply()` 让沉默成为成功终态；无多 Source 洪流，路由判据不存在；deepseek-flash 本身廉价档。配置里随时可加回。

---

## 1. 第一性原理

意识四要素：①存在（共享持久真相）②投影（各会话只写自己的压缩状态）③受限读（他人 digest 进 context，原文永远按需）④变化驱动（revision 版本号，stale 不落盘）。

铁律（不可违反）：
1. **单一真相在 store**；模型 context 与操作者面板都只是投影，绝不成为第二真相源。
2. **会话间永不直接通信，只经 board**。直接注入 = 跨会话 prompt injection。
3. **每个会话 = 一个读者 + 一种写者**。worker 读 board，只写自己的 progress/attention/task/note。
4. **通道所有权不可转让**：谁的联系人的会话，谁持有发送权。跨会话流动的是事实，不是授权。所有权属任务/会话，由服务代执行。
5. **唤醒按承诺分级**：唤醒的必要性 = 存在 waiting 态的订阅承诺，不取决于"事件发生了"。

---

## 2. 实体模型（核心修正：task 与 session 的从属）

> **task = 工作项（跨会话存活）；session = 该工作项的一次执行（当轮即逝）。board 不是第三种实体，是二者的联合视图。**
> diamond 对应：Ticket 定义自己的状态，Assignment 绑一次执行且独立于 Ticket；"Ticket state follows the Session" 是明令反模式。

| 事实 | 挂哪 | 谁写 |
|---|---|---|
| 要做什么/在等什么/完成证据 | task（goal/status/evidence/version） | 模型（task_record，证据校验：evidence_message_ids ∈ 本运行覆盖集 message_times） |
| resources：动了哪个仓库哪些路径 | task（新字段） | 模型声明 paths，**repo 由服务在 worktree prepare 时机器盖章**（cwd 在哪仓库是事实，谎报不可能） |
| 在跑/跑到哪/耗时/模型 | session（status/headline/progress 流） | **服务自动**，模型摸不到这张表 |
| "另一会话在改 X，完了叫我" | note + subscription，**挂 task**（执行结束后订阅仍活） | 模型 |
| 等操作者答复 | attention，挂 task | 模型 |
| 完成通知（fan-out 触发点） | **task 终态/重开**，不是 session 结束 | 服务 |

task 四态不变：open / waiting / completed / cancelled。task lifecycle 内可有 N 个 session（重试/兜底/重启）；`session.task_id` 可空——闲聊直接回复的会话不建 task。

---

## 3. 工具面（reader + one writer）

| 工具 | 语义 |
|---|---|
| `report(kind, text, evidence?)` | kind=progress（不关 turn：里程碑进卡片+面板 headline，继续干）/ final（关 turn，text=给联系人正文，evidence 校验）/ blocked（关 turn，等外部） |
| `no_reply()` | 零参沉默，成功终态，不触发兜底 |
| `task_record(goal, status, evidence, expected_version, resources?)` | 建档/更新；乐观并发已有 |
| `task_lookup` | 查事项 |
| `subscribe(repo, paths?)` | 资源订阅（repo/paths 语法，见 §5） |
| `notify_session(to, text, urgent?)` | 定向便签，随时可写，不等终态 |
| `ask_operator(question, options?, blocking?)` | → attention；blocking=true 会话转 waiting 不烧 token |
| `board_lookup(filter)` | 按需拉他人会话/任务详情与 progress 历史 |

工具拒绝走 deny（写前拒、带原因），不抛错。

---

## 4. store（AuditStore 扩展）

```
已有: events / outgoing / delivery 队列 / runs / tasks(四态+证据+版本) / manual
新增:
  sessions(session_id PK, task_id?, contact_id, status, headline, revision,
           started_at, updated_at, ended_at, outcome, depth)
  progress(session_id, seq, kind, text, ts)
  attentions(id, task_id, contact_id, question, options_json, status,
             blocking, due_at, answered_at, answer)
  board_notes(id, from_task, to, text, ts)
  subscriptions(task_id, repo, paths_json)
```

- 单一 `board.revision` 单调递增：digest 带 seq，commit 校验 changedSince，stale 重读。
- session 行纯机器事实：服务开轮建、monitor 更新、终态落 outcome。进度谎报结构上不可能（模型只能给文本，服务盖章时间戳）。

---

## 5. 订阅本体：repo/paths，不是自由 topic

```ts
subscribe({repo: "lucid"})                // 仓库级
subscribe({repo: "lucid", paths: ["src/auth/**"]})  // 路径级
task_record(resources: [{repo, paths?}])
```

匹配 = repo 相等 + path glob 求交，纯集合运算。冲突检测免费：两个 running session 的 resources 求交非空 → 面板标红（Hold 入口，操作者裁决，系统永不裁决）。fan-out 事件携带硬证据（branch/commits，服务从 worktree 读取）。自由 topic 仅作人读标签，不参与匹配。

---

## 6. 注入协议（prompt = 唯一注入面）

```
worker.md 骨架（待整改，见 §10）
<untrusted_recent_context>   锚定扩窗：下限30，上限100，
                              向前扩到上一条 agent 消息 + 各活跃 task 首条消息
<untrusted_current_messages> 本轮 batch
<untrusted_board>
  tasks:    [{goal, status, resources, 跟进session, revision}]
  sessions: [{contact, status, headline, 跟的task}]   ← 排除自己
  notes_to_me: [...]
</untrusted_board>
[图片段: pi 原生 / codex localImage / claude 嵌路径]
```

运行中变化驱动：monitor 循环（5s）查 MAX(board.revision)，与该会话已知 seq 不同 → steer 极短 delta。只在变化时推。

---

## 7. 跨会话通信与唤醒分级

```
A --notify/subscribe--> board
A 的 task 终态/重开(含 failed) → completion 事件 → 路由给订阅方 B:
```

| 订阅方情形 | 动作 |
|---|---|
| B 在跑 | 下次 digest steer 带到；urgent=true 立即 steer |
| B idle + task=waiting + goal 是**转达/知会** | **模板直发**，不起 LLM：completion 字段（repo/outcome/branch/commits/headline）拼一句，以 B 任务名义经 outbox 发唐凯，标 agent_outgoing |
| B idle + task=waiting + goal 含**后续动作** | **合成输入唤醒完整 worker**：同一条流水线（worktree/证据集/takeover 检测），干完 final |
| B 无 waiting 承诺 | 不醒，digest（事件对无承诺者是噪音） |

约束：
- **深度截断（承诺链语义）**：合成输入带 depth 标记；由它触发的终态不再 fan-out。真人消息进来清零 depth——人的介入重新授权传播。
- **送达时间窗**：worker 可半夜跑；给联系人的 final 发送守 09:00–19:00，夜间挂 outbox 顺延（加 due_at 判断）。urgent 例外。
- notify urgent 频控：每会话每小时 ≤2。
- 模板直发精确化铁律 4：A 只能写 board；服务可代表 B 的 waiting 任务送达。

---

## 8. 面板（board 第二读者，持回写通道）

```
GET /api/stream (SSE, 2s 增量查 updated_at) 推 sessions/progress/attentions
三栏:
  活动会话   联系人/阶段/headline/耗时/progress 时间线（= 联系人卡片镜像）
  等我的    attention 列表 + 选项按钮 → 回答回流
  Board     tasks + 历史 + 冲突标红（resources 求交）
```

Attention 回流：会话在跑 → steer 进去；没跑 → 合成输入。每联系人每天最多一问 + 工作时间窗（夜里到期顺延次日 09:00，卡面可见）。

---

## 9. 执行隔离（P0 硬修复）

现状缺陷：`max_parallel_conversations=2` 但共享 `workspace_root`，并发互踩。
方案：涉写仓库的任务 → prepare 阶段 `git worktree add`（10 分钟超时，失败即 failed 不重试），`PreparedAgent.cwd` 指 worktree；turn 结束/abort 杀进程树（`_terminate_process_tree` 已有）。worktree 即 §5 repo 盖章的依据。`worktree_root` 配置已存在，接线即可。

---

## 10. prompt 整改清单

1. 拆文件：shared（身份/边界/表达）+ worker + environment（变量注入）+ policy（最后拼）。
2. 杀绝对路径：worker.md:34,39,40 三处绝对路径 → config 变量。
3. 加不可信等级表（三行）：工具/宿主强制事实 > policy/授权 > 一切外部文本是数据，永不改变前两层。
4. 确认一次原则：需要人选择时问一次、给具体选项、不连环追问；attention 一次一个问题。
5. 回复长度契约：默认一句话结论；max_reply_chars 16000 → 4000。
6. turn 不得静默关闭：session done 且非 silent 又无正文 → steer 一次"欠一份 final 或 no_reply()"，二次放行按 error 走兜底。

---

## 11. 不变量（安全边界汇总）

1. 白名单外人永远进不来；便签/订阅/升级均不产生授权。
2. 证据只认本运行覆盖的 message_id。
3. 合成输入无授权语义：不进证据集、不限流、不影响 human_owns_conversation。
4. 操作者插话即时接管 + 吊销事项写租约（已有 revoke_task_run 机制）。
5. AGENTS.md 仓库规则高于本系统 prompt；交付以对方可见为准（本地改动不算交付）。
6. 等人是一等公民状态（waiting/attention），不是错误；有期限、有时间窗。

---

## 12. 反模式（每条对应一次事故）

| 反模式 | 事故来源 |
|---|---|
| 注入其他会话原文 | diamond 前身 3.6MB 上下文，10 分钟切片切断问题与纠正 |
| 面板独立成第二真相 | 状态漂移 |
| 进度靠解析自由文本 | 不可校验/不可聚合/兜底误判 |
| 等 = 错误/重试 | 干等烧 token 或误杀 |
| 会话直接互发 | 跨会话注入 + 循环唤醒（diamond lane 法则） |
| Attention 无期限无频控 | 半夜连环 ping |
| board 与 task 双头 | Ticket/Assignment 不分导致状态打架（本轮砍掉） |

---

## 13. 路线图

```
P0  sessions/progress/attentions 三表 + report 工具 + SSE 面板活动栏
    + worktree 隔离（修共享 cwd 并发互踩）
P1  ask_operator + Attention 回流 + 时间窗 + 每联系人日一问
P2a task.resources(repo 服务盖章) + board_notes + notify_session
P2b subscribe(repo/paths) + glob 求交冲突标红
P2c task 终态/重开触发路由（§7 三行表）+ urgent 逃生门 + 频控 + depth 截断
P3  recent_context 锚定扩窗 + prompt 拆文件/杀路径/等级表/长度契约 + turn-stopping
```

## 14. 现状基线（2026-11-06）

- preset `pi-deepseek`，worker=`pi-worker-deepseek`，`aimodel-zhizhen/deepseek-flash`，effort high，`pi --mode rpc --no-session`
- front 已移除（本日），launchd `com.inoryirony.dws-chat-agent` 重启生效
- 现有可复用：AuditStore 乐观并发（expected_version）、message_times 证据集、revoke_task_run 租约、steer/supplement 通道、delivery 队列、streaming card、`_terminate_process_tree`、worktree_root 配置位

---

## 15. 对抗审查发现登记（2026-11-06，30 天真实流量回放，待逐条盘）

数据：10 个联系人 30 天 ~1170 条双方消息（/tmp/dws-audit/）。三路并行审查。按主题归并如下，编号 A/B/C 对应三路报告。

### T1 资源本体错位（A-F1, A-F6, C-F5）★最高
30 天仅有的两次真实"改X影响Y"冲突（10-14 工具改名、10-16 二次改名）载体全是运维面：K8s 副本、catalog 承载、网关白名单、工具名、archery 工单——repo/paths 求交全盲。多数完成事件是运维回执（"已上线"），无 worktree 无 commits，§5"fan-out 带 branch/commits 硬证据"对主流事件失效。外部写者（10-30 自动同步覆盖 agent 合并）在系统外，永不可订阅。单 task 跨双 repo（10-30 浅语）与单 worktree 模型冲突。
修向：resources 增类 config:/env:/ticket:/pipeline:；盖章扩展到部署/tag/配置动作；多 worktree；订阅面诚实标注"仅覆盖系统内互知"。

### T2 注意力经济学空白（B-S1, B-S2, B-S6）★最高
steer 零节流：全局 board.revision 单调，任一会话 progress 落库 → 全部在跑 worker 每 5s 判变（11-04 杨培 2h 统计任务+黄 502 并行实证）。context 零预算：锚定扩窗 100 条 + 各 task 首条消息，黄峰值日单轮 30-60k token，digest 无条目/字数上限，17 天不闭环 task 永占位。x/q 300 字截断实证丢表名清单与报错文案。
修向：steer 前置相关性过滤（仅本 contact 新消息/notes_to_me/订阅终态）+ coalesce ≥30s + 每 turn 上限 3 次 + 工具间隙注入；recent_context 改 token 预算制；digest 硬上限+LRU；截断结构感知（代码/URL/列表不截）。

### T3 证据集跨运行失忆（B-S4, A-F8）★高
实录：10-09 15:29 黄问"早上说的 10109 咋样了"→AI 回"No session file mentions"（内部独白外泄）。授权约束当外存：黄 10-16"排查可以、修改需天泽确认"每 2-3 天须人工重申否则滑出窗口。一次性问答结论无沉淀（唐凯 MC0167），跨月回访无法恢复。
修向：task 增跨运行事实区（决议/约束/已排除假设，final 时 evidence 升格沉淀）；evidence 放宽为本运行消息 ∪ task 事实区；一问一答也落轻量 note 进 board 历史索引。

### T4 等待语义不完整（A-F4, B-S7, C-F4）★高
task.waiting 无 due_at/超时/升级（蒋涛账单反问挂 10 天）；三种真实态无载体：交付等验收（悬 21h+）、等外部第三方且无唤醒源（工单悬 12 天）、到点验证（靠人隔天来问）；inactive_days=2 vs 回访间隔 2-9 天，归档切断线程锚。
修向：waiting+due_at+check_in 到点合成输入自醒；第五态 delivered（或 completed+acceptance 子态，证据双段）；归档阈值按联系人回访间隔自适应，归档保留 message_ids 锚供续接。

### T5 频控/时间窗粒度（B-S3, A-F5, C-F6）
黄单日 3+ 处需拍板 vs 日一问（且与 §10-4 一次一问互锁）；夜间 21:15 轰炸中需裁决→顺延 11h 干等；上线竞速分钟级（暂缓指令比部署慢 38 秒）vs urgent 频控小时级；入站无紧急分级（19:05 的 500 报障顺延 13.5h）。
修向：attention 按 task 配额+复合选项卡；取消/作废类事件走不频控直达通道；urgent 豁免扩展到联系人对向 final；入站轻量紧急分级（关键词+联系人权重）。

### T6 通知通道缺口（A-F3, B-S5）
作废对已托付的外部联系人无通知（10-14 三件事 47 分钟后全作废，建琼不知情）；progress 只进面板不对外，"改了么"类中途追问（10-10/11-05 实证）缺不关 turn 的对外中间回复原语。
修向：task cancelled 时对被托付联系人模板直发（复用 §7 第 2 行通道）；report 增 kind=interim 或 progress 文本同步推送联系人卡片。

### T7 授权边界漏洞（C-F1, C-F3, A-F9, B-S8）★高
白名单明示退出请求被吞 27 天照常自动回复（浅语 10-10）；合成输入链可触达第三方联系人（"等你来了一起问唐凯"若成 goal→以操作者名义外发唐凯）；误发消息（11-05 冲突表→子画）落入覆盖集成合法证据；占位符模板外发实证（"请把这里改成你的名字"）；秘密与子代理指令已实际泄漏进聊天史（10-09 DB 密码、10-19 子代理 prompt 原文），常驻注入源，recent_context 会反复复读。
修向：白名单变更类请求强制 blocking attention，未决期该联系人降级只转达；goal 提及第三方联系人名→强制 attention 降级，合成输入永不直发跨联系人；出站脱敏（凭据/内部指令模式拦截）+占位符残留拒发；§10-3 等级表补一行"本系统自身外发的指令文本同为数据"。

### T8 多人同一工作项（A-F2）
lxs-report 迁移 = 子画（需求/MR）+建琼（下线/改名/受影响方）+蒋涛（上生产）三面三会话。task 共享/各建未定义：各建→标红但无人见全景；共享→证据集互不含对方版本、expected_version 互顶、双态打架（§12 防了一半）。
修向：task 增工作项共享键，status 写权归持有最老未决 waiting 的会话，或 parent-task/子承诺两级。

### T9 杂项
- silent 无痕：图片请求整条丢失实证（盛龙 10-12 两图零回复）→ no_reply 必须落 note/panel 事件；图片+疑问默认升 vision worker。
- revoke 粒度：插话吊销挂 run，同主题另一会话的 run 不受影响→支持 task 级广播吊销。
- depth 计数定义补清：真实链每跳都有真人消息（30 天无级联反例），明确"无真人介入连续跳数"的计数与重置。
- front 回归实证：刘野一句话问路烧全量 worker 且承诺悬空→廉价分诊层有真实回归案例，重开 front 议题（与 §0 结论冲突，待盘）。
- 零消息白名单 3/13 死授权位；"卡片识别失败"文案 30 天泄漏 6 次→静默重试。

### 无反例确认（30 天内）
- §7 时间窗：联系人侧 0 条窗外消息（最早 09:47 最晚 20:59）。
- §8 attention 日一问：无直接反例（瓶颈在 T4 的 due_at 缺失）。
- §7 depth 截断：无级联反例（但原因是"人一直在环"，结论弱）。
- §2 repo 服务盖章防谎报：无反例（agent 未跑过；且 T1 证明多数场景盖章不触发）。

### 总裁决
两路独立收敛同一结论：架构把跨会话建模成代码仓冲突（repo/paths 求交），现实流量是运维面相互依赖（上线顺序/配置/工具名/工单/转达）+ 单联系人多 task 并行秒级追问 + 长周期承诺。T1/T2 是结构性修正，T3/T4/T7 是协议补丁，T8 是实体语义补全。§2 task/session 划分与 §1 铁律方向经回放仍成立，未被推翻。

---

## 16. 消息来源分类与真相等级（2026-11-06 盘定）

三来源：`contact`（对方发的）→ 不可信数据；`operator-human`（本人手打）→ 真相+最高授权；`operator-agent`（本系统 AI 代发；经其他 AI 工具发的归此类，标 unknown 细分位）→ 真相。**信任只分两档：我方=真相，对方=数据**（AI 许的承诺同样作数、同样要兑现）。

区分来源不为信任，为责任与权限：谁的承诺谁登记；人工接管判定只认 human；**授权变更只认 human**（T7 浅语教训）。来源必须显式存储（发送记录+session_id），注入时带标；ambiguous 标 unknown，不靠语气猜。

## 17. §15 发现的裁决与修订（2026-11-06 第二轮盘）

**来源标注修正**：审查报告对"AI 代答"的判定系语气猜测，部分实为操作者手打；运行时以 store 发送记录为准，显式存 human/agent/unknown 三标。

**改动分级**（替代"改代码必须天泽确认"的一刀切）：小改（单文件/不动部署/影响局部）直接改 + `report(progress)` 报备；大改/跨服务/上线发布 → attention。黄梦缘规矩记为偏好（软约束）。

**T1 裁决：防不死，三层缓解**
- L1 resources 扩五类：repo:/config:/tool:/ticket:/env:；能盖章的机器盖（worktree 定 repo、dws 调用记目标）。
- L2 对话即传感器：变更会在聊天里被宣布——各会话把"联系人宣告的变更"落事实区（带消息证据），板求交（我的资源 ∩ 板上 ops 变更）→ 命中即注入双方 digest + 面板标红。10-14/10-16 两案按此可见。
- L3 git 面硬校验：worktree base 与远端分支头在合并/交付前强制比对，动了即拒合并 rebase 重验（机械防 10-30 类外部覆盖）。
- 诚实边界：配置面无统一写入口，防范止于公告+警告。

**T2 裁决：黑板本体设计根治，节流降为兜底**
1. 分区计数：黑板分会话私有区（本联系人消息/给我的便签/我订阅的资源事件）与公共区（台账），版号按区计——跨会话 steer 污染结构性消失。
2. 事件分级：**流水（progress/heartbeat）只进面板事件流，不碰版号；台账（任务状态/便签/终态/事实区新增）才 bump 版号**。黑板是台账不是日志（升入铁律）。
3. 条目一句话形态：每条目强制 headline（写入方负责），注入/digest 只取 headline，正文永远 board_lookup；注入统一走 render(token_budget)，预算不足丢最旧。

**T3 裁决：现在就做——task 事实区**
```
facts: [{kind: 决议|约束|承诺|已排除, text, anchor_message_id, source, at}]
```
写入规则：第 N 轮运行覆盖了消息 m，即可把 m 升格为事实锚；此后引用事实，不必再覆盖 m。约束分硬约束/偏好。并入 P0。解 10109/授权规矩/MC0167 三实录。

**T4 裁决：晨检语义**
waiting/交付待验收/到点验证三类带 check_at：
- 到点验证：check_at 入窗 → 合成输入自动唤醒会话执行验证并答复联系人。
- 等外部无期限：不自动催联系人；每日 09:00 生成操作者晨报（面板 Attention 顶部 + 可选钉钉一条汇总悬置清单：事项/天数），人决定催/作废。
- 晨报=既有通道汇总视图，非新机制。

**front 重开**：刘野案实证廉价分诊价值；方向=超轻量首轮（no-tool 快答或仅判"要不要建任务"），非恢复旧 front.md 全量。待盘。

---

# §18 极简定稿（奥卡姆剃刀版，本节起为正文；§1-17 为推演过程归档）

## 世界

一个操作者，一群联系人，一块黑板，一个会话工厂。消息进来，工厂起一个会话，带着黑板上属于该联系人的记忆干活，结果发出去。干不完的因为要等——等人/等任务/等时刻，等待也是黑板记录，到点系统自己醒。

## 黑板 = 三张表

```
tasks   goal | status(open/waiting/completed/cancelled) | waiting_on(联系人|task|时刻)
        | resources[](字符串; repo 由 worktree 机器盖章) | contact_ids[](自动累积)
        | facts[](跨轮记忆, 见下) | expected_version
runs    task? | contact | status | headline | started | ended；progress 流水挂 run(只给人看,不触发通知)
messages(已有) + 来源标: contact / human / agent
```

无 attention 表(waiting_on:"天泽")、无 notes 表(=facts)、无 subscriptions 表(=waiting_on:task)、无板级 revision。

## 工具 4 个

report(progress|final|blocked, text) — progress 更新卡片+面板不结束; final=给联系人正文
no_reply() — 沉默
task_write(goal?,status?,waiting_on?,resources?,facts?) — 登记/更新/记忆
task_read(过滤) — 读黑板(自己的/别人的/等我的/含completed)

## 路由 3 条(什么可打扰在跑会话): 本联系人新消息 | 我 waiting_on 的任务变了 | 我的 resources 撞了别人新 facts。其余写入皆流水。黑板是台账不是日志。

## 唤醒 2 条: 任务终态→唤醒 waiting_on 它的会话(合成输入, 回声不再传) | waiting_on=时刻→到点入窗唤醒验证。每日09:00晨报=waiting 清单汇总一条。

## 铁律 4 + 小规则 3

真相只在黑板(prompt/面板皆投影) | 会话间只经黑板 | 只写自己的 run 和触碰的 task facts | 联系人会话持有发送权,跨会话流动的是事实不是授权。
小规则: 给人的消息守09:00-19:00 | 授权变更只认human手打 | 小改直接改+报备,大改/上线才问。

## 机械件 3 个(不靠模型)

worktree 隔离+合并前校验远端分支头 | facts 锚定放宽(覆盖过 m 的运行可升格 m,此后永久可引) | 来源显式存库不猜。

## 跨轮记忆三层

聊天记录=短期(开窗读,锚定扩窗,token预算) | task.facts=长期(text+anchor_message_id+source+at,只增不删,注入只带活跃任务最近几条,老任务等task_read;一次性问答=completed task当笔记) | 世界=永久(commit/文件/部署,直接重读)。task.contact_ids[] 解决多人一事共用事实区。

## 面板(黑板投影,loopback,2s轮询)

视图4:
  等我裁决  waiting_on="天泽"&waiting, 选项按钮作答→在跑steer/没跑唤醒
  通知      白名单事件派生视图,仅4类: 裁决到期未答/晨报/会话失败兜底耗尽/资源撞车; 同task 24h聚合,无期限等待不催; 其余皆面板数据非通知; 可选镜像钉钉DM一条
  在跑会话  runs running: 联系人/headline/耗时/progress时间线/每行steer输入框
  任务      tasks 过滤, 点开facts时间线+触碰会话
写2:
  POST /api/session/new {contact?, text}  合成输入起一轮,来源human,无contact=纯内部任务
  POST /api/run/:id/steer {text}          立即注入
关键语义: 钉钉侧手打回复联系人=人工接管(终止+吊销+冷却); 面板steer=操作者指令(不终止不冷却)。判定=来源通道。写操作全落库带来源标。

## 已砍清单(理由见对话记录,防止回潮)

attention表/notify_session/urgent/subscribe+五类资源枚举+glob/模板直发分支/分区计数+render预算/板级revision/interim/T8共享键/front重开/facts kind枚举/steer节流参数(留实测调参)。
复杂度对账: 新表2张、工具4个、机制9个。30天回放23条发现全覆盖。

## 路线图(重排)

P0  tasks/runs/facts schema + 4工具 + worktree隔离 + 面板(在跑/任务两视图,2s轮询)
P1  waiting_on 路由2条 + 晨报 + 等我裁决视图 + 作答回流 + 面板两个写入口
P2  resources求交撞车标红 + git合并校验 + 通知白名单/聚合
P3  开窗token预算化 + prompt拆文件/杀绝对路径/等级表/长度契约 + turn-stopping

## 卡片两相协议（2026-11-06 补）

进度=状态不是内容。联系人卡片一生两相，不混装：
- 相1 工作中：正文=里程碑列表（append-only 状态的幂等全量渲染，每次 update 发完整列表，内容逻辑不丢），⏳状态标+3~7行 icon 短句；里程碑仅由 report(progress) 触发，天然稀疏。
- 相2 交付：正文整体切换为最终答案（流式只在此相），一次性覆盖。
- 砍：原始思考 delta 流进联系人卡片（消失问题源头+不该给联系人看推理）。
- 文件只承载交付物（final 触发一次性发送），不为进度发文件；文件不可撤回。

## 面板视觉规范（2026-11-06 补）

icon-first 零术语：状态用形状颜色（红=等你/失败/撞车，黄=等外部，绿=完成，灰=归档），身份用头像，动作用 icon（💬=steer，＋=开会话，🔔=通知，🐛🔧📌=发现/修改/决议），列头只有 ▶⏸✅。面板上唯一成段文字=模型写的 headline 与答案。等我裁决=红点badge+头像+一句话+选项按钮；任务=看板三列；撞车=两卡红连线，无文字。
