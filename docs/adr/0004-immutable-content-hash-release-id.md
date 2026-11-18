# 0004 · release_id 采用内容哈希不可变发布表

- 状态：已接受（2026-11-18）
- 决策者：Francis
- 关联：`gap-analysis.md §6` 决策4、PRD R10 / FR-20、审查 P1-11、`migrations/00_schema.sql:409`

## 背景

现有 `data_version` 是可变 SERIAL int，审查 P1-11 指出它不是跨服务一致快照：并行读取端可能读到不同版本，缓存键用它也无法保证一致性。PRD R10 要求不可变 `release_id`。

## 决策

新建不可变 `release` 表，`release_id = 各数据源版本清单（招生计划 / 历史录取 / 一分一段 / 规则 / 文档索引 / 模型算法配置）的 SHA256 前缀`。run 创建时绑定当时的 release_id；缓存键 = `prompt_hash + release_id + model`；各读取端按 release_id 选数据，禁止原地覆盖。`data_version` 保留作迁移过渡，新逻辑一律走 release。

## 理由 / 权衡

- 内容哈希天然幂等、可审计，相同清单得相同 id，根治 P1-11 的一致快照问题；
- run 绑定 release 后，FR-06「每次 resume 校验时效」与 FR-20「ETL 发新版旧缓存自然失效」都能落地；
- 代价是 ETL 发布流程需生成清单哈希、新建 release 表。

## 被否决的备选

- 单调版本号 `rel_YYYYMMDD_seq`（人可读但不反映内容是否真变）；
- 沿用 `data_version` 加「只增不改」约束（SERIAL int 一致快照语义弱，P1-11 未根治）。

## 影响

- 需新增 release 表迁移 + ETL 发布生成清单哈希；
- llmgw 缓存键、orchestrator run 绑定、各读取端选数据均按 release_id；
- `data_version` 进入弃用过渡期。
