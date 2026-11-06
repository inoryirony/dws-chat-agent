# panel-display

## Sub-features

- 在跑会话视图（runs running：联系人/headline/耗时/progress 时间线）
- 等我裁决视图（红点 badge + 选项按钮）
- 任务视图（看板三列 + facts 时间线）
- 通知视图（白名单 4 类派生）
- SSE 2s 增量推送（/api/stream）
- 来源标渲染（human/agent/contact 三色或图标）

## How to get to it (user POV)

浏览器开面板 URL（token 进 cookie）→ 看到三/四视图 → 点任务看 facts → 对"等我裁决"点选项按钮作答。

## Driving it with Chrome DevTools MCP

1. Launch 形态 B（纯面板，不起 dws）
2. 预置 store：直插 tasks（waiting/completed 各若干）、board_runs（running 一条带 progress）、attention 类 waiting_on="操作者"任务
3. `new_page` → `http://127.0.0.1:$PORT/?token=$TOKEN`
4. `take_snapshot` 断言各视图区头文案在树中
5. 操作"等我裁决"选项按钮：`click` uid → 断言 stub.calls 记到 answer_task（test_dashboard_board.py:100 样例）→ `take_screenshot` 存证据
6. SSE 验证：`evaluate_script` 里 `new EventSource('/api/stream')`，onmessage 收到首帧即断言过

终态判据：截图 + snapshot 文本行 + stub 调用记录三面齐。

## Gotchas

- 2s 轮询：插入数据后 `wait_for` 文本出现再操作，别抢跑
- 视觉规范（§18）：红=等你/失败/撞车、黄=等外部、绿=完成、灰=归档——截图后按此色语断言，色值用 CDP 读计算值（面板皮肤层记忆：Metropolis 切角 3px 非 18px，CDP 读计算值验样式）
- 两套皮肤（dashboard/diamond-preview）共用 board.js/board.css——改前端须两皮都截
