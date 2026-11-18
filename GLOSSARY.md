# GLOSSARY

本仓库的**单一上下文术语表**（single-context，约定见 `docs/agents/domain.md`）。输出中涉及领域概念时（issue 标题、重构提案、假设、测试名），用此处定义的术语，不要漂移到「避免」列点名的同义词。`docs/prd.md §11` 术语表是本表的子集，**以本表为准**。

术语缺口是信号：要么你在发明项目不用的语言（请重考虑），要么是真实缺口（记下来交给 `/domain-modeling`）。

## 高考志愿领域术语

| 术语 | 定义 | 避免 / 备注 |
| --- | --- | --- |
| 冲 / 稳 / 保 | 按录取概率分层的志愿策略：冲刺 / 稳妥 / 保底 | — |
| 院校专业组 | 湖南省新高考平行志愿的投档单位（一组选科要求相同的专业），候选生成的基本单位 | 不要退化为「院校 + 专业」二元 |
| 选科组合（3+1+2） | 首选科目（物理 / 历史）+ 再选 2 门 | — |
| 科类（category） | 物理类 / 历史类（新高考 3+1+2）；位次与概率计算的必需过滤条件 | **避免 track**（track 是废弃表 admission_score 的旧字段名，活跃表统一用 category） |
| 一分一段表 | 省考试院发布的分数 → 累计位次映射表（`score_rank_map`） | — |
| 位次（rank） | 考生在全省同科类的排名 | — |
| 百分位（percentile） | `rank / total_count` | — |
| 批次控制线（control_score） | 各批次录取最低控制分数 | — |
| 加权命中率法（weighted_hit） | 现行录取概率估计方法（详见 `docs/algorithms.md`） | **避免「分位数法 / 核密度」**（已废弃设计，非 V1 承诺） |
| 滑档率（slip_rate） | 平行志愿下全部未投档的解析式估算 Π(1−pᵢ) | 是近似估计，**非录取保证** |
| 期望满意度（expected_satisfaction） | 志愿表效用分均值 | — |

## 系统 / 身份模型术语

| 术语 | 定义 | 避免 / 备注 |
| --- | --- | --- |
| run / interrupt | run = 一次任务执行（每次生成 / 修订新建）；interrupt = run 内一次人工中断。恢复与幂等以 run/interrupt 为单位 | **避免以 session 为恢复单位** |
| release_id | run 绑定的不可变数据 / 规则 / 索引发布清单版本（内容哈希，见 ADR-0004）；各读取端按它选数据、缓存按它失效 | **避免 data_version**（可变 SERIAL int，已弃用过渡） |
| 三类证据 | 文档事实（绑原文片段）/ 结构化事实（绑行身份 + 字段 + 版本）/ 派生事实（绑输入快照 + 样本 + 算法参数版本，确定性重算） | — |
| Verifier（事实校验器） | 按三类证据将报告事实性陈述逐条比对的环节 | — |
| HITL | Human-in-the-loop，流程中的人工确认 / 补问暂停点（interrupt/resume） | — |
| Budget Guard | 会话级 token / 成本预算闸门，原子预占 + 结算 | — |
| logical_call_id / attempt_id | logical_call_id 标识一次语义调用；attempt_id 标识一次供应商尝试 | — |
| AuditBasis（审计依据） | algo 输出的可复现依据：用户位次 / 百分位、样本年份、method、province、category | — |
| RAG | 检索增强生成；本系统**仅用于非结构化章程文本** | 强结构化数据走 SQL，不走 RAG |
| RRF | 倒数排名融合，合并向量与关键词两路检索结果（Σ 1/(k+rank)，k=60） | — |
| policy_chunk / policy_doc | 招生章程 RAG 的**活表**（向量 + trgm） | **避免 charter_chunk / charter_doc**（孤儿空表，已废弃，见 ADR-0006） |
| golden set | 版本化管理的标注问答集，用于 CI 回归门禁（检索集 + 端到端集，见 ADR-0005） | — |
| SSE | Server-Sent Events，服务端流式推送 | — |

## 已拍板决策索引（详见 `docs/adr/`）

| ADR | 决策 |
| --- | --- |
| [0001](docs/adr/0001-defer-zhparser-keep-trgm.md) | 维持 pg_trgm，推迟 zhparser |
| [0002](docs/adr/0002-langgraph-official-postgres-saver.md) | 官方 langgraph-checkpoint-postgres |
| [0003](docs/adr/0003-deepseek-early-llm-experimental.md) | 早期 DeepSeek 实验链路，M5 收口网关 |
| [0004](docs/adr/0004-immutable-content-hash-release-id.md) | 内容哈希不可变 release_id |
| [0005](docs/adr/0005-golden-set-human-labeled.md) | golden set 由 domain owner 定标 |
| [0006](docs/adr/0006-consolidate-policy-tables-drop-charter.md) | 收口 policy_*，废弃 charter_* |
