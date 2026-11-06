---
name: verify-dws-chat-agent
description: 驱动 dws-chat-agent 真实例做行为验证：起隔离实例、经面板 API/SSE/SQLite 三面取证、CDP 截前端。触发架构改造（TriggerEvent/pipeline/Watch）的每一步落地后都应跑本 skill 对应 feature 的 drive。
---

# verify-dws-chat-agent

对象：钉钉私聊自主代理（src/dws_dm_agent/）。面板 = 前端显示面（dashboard.html + board.js），loopback HTTP + SSE。

## 铁律

1. **绝不碰 launchd 生产实例**（`com.inoryirony.dws-chat-agent`，state_dir=`<repo>/state`，port 8765）。验证只起隔离实例：独立 config + 独立 state_dir + `port=0`。`_acquire_single_instance`（service.py:3022）按 state_dir 加锁，同 state_dir 双开必失败——这是特性不是障碍。
2. **绝不真发钉钉**。隔离 config 的 contacts 用假 identity，`dws` 客户端不可用或模式为 shadow 时外发必落 outbox 而非真发。验证"回复"看 store 落库，不看钉钉。
3. 证据落 `/tmp/dws-verify/<run-id>/`，cleanup 不删证据目录。

## Launch（起隔离实例）

两种形态，按需选：

**形态 A：全服（含 dws 监听器）**——验证触发链路用它。

```bash
python3 scripts/verify_launch.py --repo <repo根> --state-dir /tmp/dws-verify/<run-id>/state
# stdout: READY <port> <token> <pid> <config-path>；SIGINT/SIGTERM 优雅停
```

实测三坑（2026-11-06 自测打穿，勿重踩）：
1. **端口**：生产 launchd 实例常驻占 8765。helper 已设 `DWS_CHAT_AGENT_DASHBOARD_PORT=0`（service.py 的环境覆盖点）随机端口。若手起实例撞 "Address already in use"，先 `lsof -i :8765`。
2. **模型**：verify worker profile 必须显式 model，否则落 pi 全局默认 `aimodel-deepseek/deepseek-v4-flash`，本机网关 token 报 503 model_not_found（同网关不同 token 分组不同模型清单）。helper 默认 `ccr-internal/glm-5.2`（已 E2E 验证），可用 `DWS_VERIFY_MODEL` 覆盖。
3. **dws 报错是噪音**：假 identity 下 "DWS command failed (3)" / "listener exited" 属预期（dws CLI 不认识假用户），实例照常工作，勿判失败。

**形态 B：纯面板 + 手工驱动**——验证面板显示/接口用它，不起 dws 子进程。

照 `tests/test_dashboard_board.py:115-125` 的起法：`DashboardServer(audit_db, runtime.json, dashboard.html, contacts, tz, port=0, service=<stub>)`。stub 只需 `open_operator_session`/`steer_run`/`answer_task`/`list_x` 等被测方法，测试文件 100-135 行有完整样例。

就绪判据：`GET /api/board` 200 且 `GET /` 返回 HTML。

## Doctor

只读检查，任何 drive 前跑：

```bash
curl -sf "http://127.0.0.1:$PORT/api/board" -H "X-Operator-Token: $TOKEN" >/dev/null && echo OK
```
外加：`lsof -i :$PORT` 确认进程是本 run 起的；`sqlite3 $STATE/audit.sqlite3 "SELECT count(*) FROM board_runs"` 应答（库存在且可读）。

token 来源：Python 侧 `service.dashboard.operator_token`（形态 A）或 `server.operator_token`（形态 B，test_dashboard_board.py:155 同款）。

## Drive

**注入对话**（不依赖 dws CLI）：形态 A 下直接对 service 私有面操作——测试同款做法是构造 `IncomingEvent` 调 `service._enqueue`，或写 wake 行（`INSERT INTO wakes` 后 `_scan_time_wakes` 自然派发）。面板侧入口 `POST /api/session/new {"contact_alias": "...", "text": "..."}`（dashboard.py:920-934）。

**steer 在跑会话**：`POST /api/run/<id>/steer {"text": "..."}`（dashboard.py:936-945）。

**作答 waiting 任务**：`POST /api/task/answer`（dashboard.py:947-959）。

**前端显示**：Chrome DevTools MCP。`new_page` → `http://127.0.0.1:$PORT/?token=$TOKEN`（token 一次进 cookie，dashboard.py:699-705）→ `take_snapshot` 拿 uid → 断言视图元素 → `take_screenshot` 存证据。稳定句柄：视图标题文案（"在跑会话"/"任务"/"黑板 Board"区头）、联系人显示名、Channels 区的 DisclosureTriangle 行（会话历史，点开见"对方消息/模型路径/处理结果"三段）。面板 2-3s 轮询，操作后 `wait_for` 文本出现再截图。

**截图路径坑（实测）**：CDP screenshot 的 filePath 只许 workspace root 内，/tmp 与仓库子目录新建路径都拒。做法：不带 filePath 让截图进 artifact 目录（`~/.zcode/cli/artifacts/<sess>/call-*-tool-result-*.png`），再 `cp` 到 `verify-evidence/`。

## Evidence

每次 drive 至少三面证据：

- 仓内：`verify-evidence/`（repo 根，gitignore 外，注意勿提交大文件——本项目默认不推送）
- 仓外：`/tmp/dws-verify/<run-id>/`（sqlite 快照、HTTP 原文、launch 日志）

1. **动作面**：HTTP 请求/响应原文（curl -i 或脚本存 json）
2. **状态面**：`sqlite3 $STATE/audit.sqlite3` 三查——`tasks`（goal/status/waiting_on）、`board_runs`（origin/headline/outcome）、`wakes`（status/error）。**改架构后此三查是 TriggerEvent 字段化的直接验证面**（origin/authorization 字段落没落对，查库即知）
3. **显示面**：CDP 截图 PNG + snapshot 里的关键文本行

证明标准：走真实用户路径（面板 API/真 enqueue），不用内部 setter 改状态；动作+结果状态都留证；mock 只许在 dws 边界（形态 A 本就不起真 dws 监听器交互）。

## Cleanup

只杀自己起的：
- 形态 A：`service.request_stop()` + task cancel + `service.close()`（脚本内置，SIGTERM 亦可）
- 形态 B：`server.stop()`
- 进程残留兜底：按 launch 记下的 pid kill，**绝不按进程名 kill**（生产实例同进程名！）
- 临时 config/state_dir 删除（在 /tmp 下）；证据目录保留

## Helpers

`scripts/verify_launch.py`——隔离实例启动器。用法：

```bash
python3 scripts/verify_launch.py <config.json> --state-dir /tmp/dws-verify/<run-id>/state
# stdout: READY <port> <token> <pid>；SIGINT 优雅停
```

自包含：渲染 config（假 contacts）、起 AgentService（shadow）、打印就绪行。exit code 非 0 = 起失败，读 stderr。

## 当前基线

- worker-only（front 机制槽位在，配置缺省即无）；触发链 = events 入队 → `_conversation_worker` → `_handle_batch`（主循环 + `_handle_batch_once` + `_BatchOutcome` stop/replan/deliver）
- 信封已字段化：`IncomingEvent.origin ∈ {dm, wake, panel, operator}`，全判定走字段，`wake-`/`panel:` 前缀约定已删（构造点标 origin，测试也标）。drive 验证点：合成轮 `board_runs.origin`（synthetic=唤醒链 / live=其余）、panel 轮回复只进 `panel_replies`、operator 轮（带 contact 面板开轮）走全链路外发
- stage 配置驱动：preset 里写什么 stage 键就有什么 step，worker 必填，front 是惯例槽位名
- 面板三视图 + 写入口两个；SSE `/api/stream`
- 源级断言（架构改动后跑）：`grep -c "_SYNTHETIC_PREFIXES\|startswith(\"wake-\")" src/dws_dm_agent/service.py` 应为 0
