# synthetic-session

## Sub-features

- 无联系人开轮（panel: 会话，回复只进 panel_replies，零外发）
- 带联系人开轮（进真实会话队列走全链路）
- 回复落 panel_replies 且面板可见

## How to get to it (user POV)

操作者打开面板 → 想让 agent 干一件不针对任何联系人的活 → 点 + 开会话、输入文本 → 面板出现新在跑会话 → 结束后回复出现在面板。

## Driving it with 面板 API + SQLite

1. Launch 形态 A，取 port/token
2. `POST /api/session/new {"text": "..."}`（无 contact_alias）
3. 轮询 `/api/board` 至该 run 出现终态
4. sqlite 查证：`SELECT origin, outcome, headline FROM board_runs ORDER BY id DESC LIMIT 1`（origin=live，面板轮按 live 记，service.py:1936-1937）
5. `SELECT * FROM panel_replies` 回复存在且 conversation_id 带 `panel:`（信封字段化后：验证 source 字段而非前缀）

终态判据：run.outcome 非空 + panel_replies 有行。

## Gotchas

- 面板轮走全 `_handle_batch`，`_stabilize` 会拉 dws.history —— 假 identity 拉不到历史属正常，进程不得崩
- panel: 会话无 manual 记录，不受 human_cooldown 拦截（这是设计行为，验证时别当 bug）
