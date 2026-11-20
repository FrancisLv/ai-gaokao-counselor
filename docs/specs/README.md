# 可执行规格索引

| 项 | 内容 |
| --- | --- |
| 状态 | M1 runtime 契约已确认，M5b 发布门禁已按契约接线 |
| 目标态来源 | [PRD v1.3](../prd.md) |
| 术语来源 | [GLOSSARY.md](../../GLOSSARY.md) |
| 决策来源 | [docs/adr/](../adr/) |

本目录把 PRD 的产品要求落成可实现、可测试的 runtime 契约。规格中的 `MUST / MUST NOT` 是接口不变量；示例、proto 草案和持久化建议都必须服从这些不变量。

## 已建立

| 规格 | 覆盖 | 状态 |
| --- | --- | --- |
| [Profile Contract](./profile-contract.md) | 画像字段、字段状态、版本、确认、算法映射 | 已确认 |
| [Run Lifecycle Contract](./run-lifecycle.md) | run/interrupt/event、接口、状态机、幂等、恢复与发布 | 已确认 |
| [Retrieval Contract](./retrieval-contract.md) | 适用域、release、双路召回、RRF、降级与索引发布 | 已确认 |
| [Eligibility Rules Contract](./eligibility-rules-contract.md) | 结构化规则、覆盖声明、三态判定、发布与回滚 | 已确认 |
| [Evidence Contract](./evidence-contract.md) | 文档/结构化/派生证据、claim 验证与发布门禁 | 已确认 |
| [LLM Gateway Contract](./llm-gateway-contract.md) | 角色路由、预算预占结算、缓存、重试与调用账本 | 已确认 |
| [Evaluation and SLO Contract](./evaluation-slo-contract.md) | golden set、指标公式、基线、CI 与发布门禁 | 已确认 |
| [Security and Retention Contract](./security-retention-contract.md) | 身份授权、同意、敏感数据、删除与保留策略 | 已确认 |
| [Proto and Database Change Plan](./proto-db-change-plan.md) | 迁移顺序、proto 演进和 GitHub issue 依赖图 | 执行中 |

## 后续

1. 按迁移计划完成 proto/DB 变更。
2. 按 issue 阻塞关系实施 M2–M5。

## 冲突裁决

1. 目标行为与范围以 PRD 为准。
2. 领域术语以 `GLOSSARY.md` 为准。
3. 已接受决策的理由以 ADR 为准。
4. 本目录负责把上述来源转成可执行细节；如果发现冲突，必须修订 PRD、ADR 或本规格，不能默默选择其中一方。
