# operator-writeback

## Sub-features

- 等我裁决作答（POST /api/task/answer → human fact 落库 + 任务恢复 open + pending wake 给联系人）
- steer 在跑会话（POST /api/run/:id/steer，操作者指令：不终止不冷却不写 manual）
- 操作者动作审计（wakes 表 contact_id='operator' 行，不进派发）

## How to get to it (user POV)

操作者看到面板红点 → 点选项按钮作答 → 联系人会话被唤醒，答复带过去。或直接在面板对在跑会话输入框输入指令。

## Driving it with 面板 API + SQLite

1. Launch 形态 A，直插一个 waiting 任务 waiting_on="天泽"（或操作者名，answer_waiting_task 路径 core.py:2180-2255）
2. `POST /api/task/answer {"task_id": ..., "answer": "同意"}`（token 头必带）
3. sqlite 断言三面：任务 facts 新增一行 source=human；status 恢复 open；wakes 出 pending 行给该联系人
4. `POST /api/run/<id>/steer {"text": "改用方案B"}` → 断言目标 session 收到 `<system_wake>` 块注入、manual_messages **无**新行（这是"操作者指令≠人工接管"的判别面，service.py:2792-2794 注释即契约）
5. 操作者动作审计：查 wakes 里 contact_id='operator' status='done' 行存在

终态判据：facts/wakes/manual_messages 三查 + steer 到达日志。

## Gotchas

- steer 写入口绝不写 manual（写了就是接管冷却误触，P0 级回归信号）
- answer 的文本会升格 human fact——授权变更只认 human 的执行面，这里必须 source=human 落库
- 作答后联系人起轮回复走 outbox，shadow 模式下查 deliveries 表而非真发
