# 0005 · golden set 由 domain owner 定标、Agent 出草稿

- 状态：已接受（2026-11-18）
- 决策者：Francis
- 关联：`gap-analysis.md §6` 决策5、PRD FR-23、`data/golden/README.md`

## 背景

FR-23 的 CI 发布门禁依赖 golden set（recall 不降 / 幻觉率不升 / P99 不超），但 `data/golden/` 目前只有 README，集子未建。标注质量直接决定门禁有效性。

## 决策

- **Francis 是最终标注人（domain owner）**，Agent 负责生成候选草稿、跑评测、出 diff 报告；
- 首批规模：检索集 30 条（从真实章程抽「问题 → 应命中 chunk / 院校」）+ 端到端集 10 条（覆盖冲稳保 / 硬约束剔除 / 无可靠数据三类场景）；
- 起步不必大，30 + 10 足够让 CI 门禁跑起来，后续迭代扩充。

## 理由 / 权衡

- golden set 是门禁标尺，标尺本身若有错却无人兜底，门禁形同虚设，故须 domain owner 拍板；
- Agent 出草稿降低人工成本，人只做校验与定标；
- 首批小规模即可抓回归，避免一次性标注负担过重。

## 被否决的备选

- 全部 Agent 自动生成、人只抽检（标尺可能出错无人兜底）；
- 暂不建、M4 再说（FR-23 门禁无从判定）。

## 影响

- M4 前需产出首批 30 + 10 标注集并进 CI；
- Agent 先做好草稿生成与抽样脚本，Francis 主要投入在校验定标。
