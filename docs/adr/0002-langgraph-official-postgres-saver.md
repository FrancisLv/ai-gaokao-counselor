# 0002 · 断点续跑采用官方 langgraph-checkpoint-postgres

- 状态：已接受（2026-11-18）
- 决策者：Francis
- 关联：`gap-analysis.md §6` 决策2、审查 §6.3-4 / P1-09、PRD FR-07、`services/orchestrator/graph/build.py:48`

## 背景

`build.py` 的 `compile()` 未接 checkpointer；现有 `plan_checkpoint` 是自定义表（session_id/node/state/seq），审查 §6.3-4 指出它不能仅凭名字当作 LangGraph 官方 saver 使用。FR-07 的断点续跑与 P1-09 的并发安全执行权缺少地基。

## 决策

采用官方 `langgraph-checkpoint-postgres` 作为 checkpointer，`compile(checkpointer=...)` 接入；其自带的 interrupt/resume、pending writes、并发语义覆盖 FR-07 与 P1-09。`plan_checkpoint` 降级为业务侧审计快照，不再承担编排恢复职责。

## 理由 / 权衡

- PRD §8 明确「编排用 LangGraph，不自研编排引擎」，自维护 checkpoint 表即变相自研；
- 官方 saver 的恢复 / 并发语义经过验证，自实现风险高（审查已点名）；
- 代价是新增一组官方表（checkpoints / writes / blobs），属标准做法。

## 被否决的备选

改造 `plan_checkpoint` 手动实现 saver 接口（省表但需自扛恢复与并发正确性，审查 §6.3-4 点名风险高）。

## 影响

- M2 任务底座以官方 saver 为地基；
- 需新增迁移建官方 saver 表，并明确 `plan_checkpoint` 的审计快照定位；
- 租约 / fencing epoch 等并发控制结合官方 saver 语义实现。
