# task-lifecycle

## Sub-features

- 建档/更新（task_write 工具，乐观锁 expected_version）
- 四态流转 open/waiting/completed/cancelled
- 证据校验（evidence_message_ids ∈ 本运行覆盖集 message_times）
- 终态事件（completed/cancelled 同事务写 task_terminal_events）
- facts 追加（text+anchor 去重，只增）
- 归档（inactive_days 阈值）

## How to get to it (user POV)

模型在会话中调用 task_write 登记工作项；操作者在面板任务视图看状态流转与 facts。

## Driving it with task_tools CLI 真路径

1. Launch 形态 A 或直接造 store
2. 经 `task_tools` CLI 直连审计库执行 task_record（这是 worker 侧真路径，task_tools.py:202）：`echo '<json request>' | python3 -m dws_dm_agent.runners.task_tools`（request 形状 task_tools.py:179 execute_request）
3. 重复同一 request 断言幂等（task_changes 收据表去重）
4. 置 completed，断言 task_terminal_events 出行
5. expected_version 传错值 → 断言拒绝（乐观锁）
6. evidence 传运行覆盖集外 message_id → 断言拒绝

终态判据：sqlite tasks/ task_changes/ task_terminal_events 三查齐。

## Gotchas

- task_tools 是独立 CLI 进程直连库——并发写靠 BEGIN IMMEDIATE，别在主进程持锁时调
- facts 去重键 text+anchor，同文不同 anchor 会两条（设计如此）
- 归档阈值 inactive_days=2（audit 记忆判过短，§15-T4），验证归档行为时用旧时间戳直插
