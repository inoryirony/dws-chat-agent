# dws-chat-agent 验证 feature 映射

验证源清单。一次 proof 至少覆盖入口所在 feature 的"用户路径→可观测终态"；跨 feature 的改动按矩阵补。

## 目录

- [synthetic-session（面板开轮→合成输入→回复落库）](synthetic-session.md)
- [wake-dispatch（到点唤醒→steer/合成输入派发）](wake-dispatch.md)
- [panel-display（面板四视图+来源标渲染）](panel-display.md)
- [task-lifecycle（四态流转+终态 fan-out）](task-lifecycle.md)
- [operator-writeback（作答/steer 回流）](operator-writeback.md)

维护：架构改动新增触发源/工具时，此处加文件并更新对应 feature 的 Drive 步骤。
