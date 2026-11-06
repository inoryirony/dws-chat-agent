# wake-dispatch

## Sub-features

- 到点 wake（waiting_on=@时刻）→ 窗内派发
- 窗外顺延（_roll_into_wake_window，09:00-19:00）
- 在跑会话 steer 优先（_steer_live_session），否则合成输入入队
- 终态 fan-out（task completed → waiting_on:task:<id> 的任务各得 wake；synthetic 轮回声切断）
- 崩溃回收（claimed 超 30 分钟回置 pending）

## How to get to it (user POV)

间接路径：模型经 task_write 登记 waiting_on=@时刻 或等待另一任务 → 时间到/上游终态 → 会话自动醒、进度出现在面板。

## Driving it with SQLite 直插 + API

1. Launch 形态 A
2. sqlite 直插 task：`INSERT INTO tasks (id, scope_self, goal, status, waiting_on, ...) VALUES (..., 'waiting', '@<2分钟后的时刻>')`（字段清单 core.py:360-380；三 scope 按 core.py:42-47 填）
3. 等 wake_interval（默认 5s）× 3 轮，查 `SELECT status, error FROM wakes`
4. 终态路径：手工把另一任务置 completed，确认 task_terminal_events 有行 → 5s 内生成 fanout wake
5. 回声验证：把该 fanout wake 派发出的轮的 run origin 置 synthetic（或用面板轮测），终态不再 fan-out

终态判据：wakes.status=done 且无 error；board_runs 新行 origin 正确。

## Gotchas

- 时刻格式必须匹配 `_TIME_WAITING_PATTERN`（service.py:99），插库前 grep 该正则确认格式
- 直插库绕过了乐观锁 version——测试可容忍，验证"模型写入"须走 task_tools CLI 真路径
- claim 后派发失败置 done+error，不会无限重试（core.py docstring 即契约）
