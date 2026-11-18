# AGENTS.md

编码代理（Codex 等）每次会话必读的施工约束。与系统提示 / `<rules>` 冲突时，以后者为准。

## 项目速览

- AI 高考志愿智能决策系统，**仅覆盖湖南省新高考 3+1+2**（其他省份不在范围）。
- 四服务单向 DAG：`客户端 → svc-gateway(Go) → svc-orchestrator(Python/LangGraph) → { svc-llm-gateway(Go), svc-algo(Go) }`，下游不回调上游。
- 核心原则：**LLM 管语义 / 生成，确定性代码管数值 / 规则**。
- 技术栈：Go（以 `go.mod` 为准，当前 1.25）+ Kratos v2 + Wire + sqlc + pgx/v5；Python 3.11 + LangGraph；PostgreSQL（关系表 + pgvector + pg_trgm）+ Redis。

## 施工事实来源（按用途分工，各为其域的唯一权威）

1. `docs/prd.md`（**v1.3**）—— **目标态唯一权威**：24 条 FR + §5.2 端到端 15 步流程 + §8 服务职责/接口契约/表映射/证据包/旁路流程 + §13 20 条验收场景（已吸收审查报告全部目标态结论）。
2. `docs/gap-analysis.md` —— **代码现状唯一权威**（活文档，随代码变）：FR × 现状矩阵、§4 阻塞缺口、**§6 已拍板 6 项决策**、§7 证据索引。
3. `docs/adr/`（`0001`~`0006`）—— **决策理由权威**：为何这么定、否决了什么。
4. `docs/algorithms.md` —— 算法口径；`GLOSSARY.md`（根）—— 领域术语单一来源，输出命名以它为准（约定见 `docs/agents/domain.md`）。
5. `docs/agent-architecture-review.md` —— **历史审查存档**：结论已吸收进 PRD v1.3，不再单独维护，仅供溯源。

冲突裁决：**目标态以 PRD 为准；现状以 gap-analysis 实测为准；决策理由以 ADR 为准**（三者 > 代码注释 > review 存档）。发现文档与代码脱节，先按「PRD 目标态 + gap-analysis 现状」施工，并在 `docs/dev-log.md` 记录；若输出与某条 ADR 冲突，须显式指出而非静默覆盖（见 `domain.md`）。

## 工程红线（不可逾越）

- **LLM 不许算数**：位次 / 概率 / 规则 / 配比一律由 `svc-algo` 给出；LLM 只写解释文案，不得生成或改写数值与候选身份。
- **强结构化数据只走 SQL**（pgx + sqlc），不走 RAG；RAG 仅用于非结构化章程文本。
- **SQL 全参数化**，禁止字符串拼接用户输入；RAG 内容视为数据，显式分隔转义。
- **禁止任何「保证录取」表述**；报告必带风险提示与免责声明。
- **宁报错不编数据**：无可靠数据 / 无规则覆盖时明确告知「不予推荐 / UNKNOWN」，不得伪造概率或放行。
- **可复现纪律（svc-algo）**：单线程、全序排序带 tie-break、禁 map 迭代产序；同输入同输出。
- **图控制权归 orchestrator**：下一步 / 条件补问 / Critic 回边由 orchestrator 裁决；gateway 只转交命令与事件，不判断图内业务。
- **密钥只放 `.env` / secrets，不入 Git**；模型 key 仅存在于 llmgw 侧。

## 已拍板决策（2026-11-18，正式记录见 `docs/adr/0001`~`0006`，摘要见 `gap-analysis.md §6`）

- **分词**（ADR-0001）：维持 `pg_trgm` + 向量主导，推迟 zhparser（勿现在自定义 PG 镜像）。
- **saver**（ADR-0002）：官方 `langgraph-checkpoint-postgres`；`plan_checkpoint` 仅作业务审计快照。
- **早期模型**（ADR-0003）：DeepSeek（`deepseek-v4-pro`），M5 收口 llmgw 网关。
- **release_id**（ADR-0004）：内容哈希不可变 `release` 表；缓存键 = `prompt_hash + release_id + model`。
- **golden set**（ADR-0005）：Francis 最终定标、Agent 出草稿 + 评测 + diff。
- **表名**（ADR-0006）：收口到 `policy_*`（活表），废弃 `charter_*`（孤儿空表）——**勿查 `charter_chunk`，检索走 `policy_chunk`**。

> 重新提议上述已否决方案前，先看 gap-analysis §6 的否决理由。

## 验证命令（提交前必须跑通）

日常开发默认入口（**不以 `make up` 为前提**）。非交互 shell 会解析到系统 Python，故 Python 相关目标须显式传 `PY`：

```
PY=/Users/francis/miniconda3/envs/ai-gaokao-counselor/bin/python3
```

- 改 proto 后：`make api PY=$PY` —— 生成 Go + Python，**勿手改 `*.pb.go` / `pb/**`**。
- Go：`make build && make check test`（`check` = sqlc 一致性 + `go build ./...` + `go vet` + wireinject 测试）。
- Python：`make py-check py-test PY=$PY`（离线，不下载模型、不入库）。
- 改 SQL / schema 后：`make sqlc-generate`（仅校验用 `make sqlc-check`）。
- 需真实库的测试（`test-integration` / `test-race` / `coverage` / `bench` / `tcp-smoke` / `perf`）必须显式设置**只读** `ALGO_TEST_DSN`。
- 网络：GitHub / 海外源经 Clash Verge 代理 `127.0.0.1:7897`（仅当前 shell 会话有效）；镜像构建用 `make docker-build IMAGE_MIRROR=m.daocloud.io/ BUILD_GOPROXY=https://goproxy.cn,direct`。

## 禁改清单（除非任务明确要求）

- `migrations/*.sql` 历史文件：**只增新迁移，不改已发布的**（尤其 `00_schema.sql`）。
- `docs/charters/` 数据资产：5700+ 院校章程原料，只读。
- `api/**/*.proto` 已有字段：**不删、不改编号**；新增字段用新编号，破坏性变更出 v2（PRD §7.5）。
- 生成物 `*.pb.go` / `services/orchestrator/pb/**`：由 `make api` 产出，勿手改。
- `bin/` 下构建产物与工具：勿提交本地产物。

## 记录义务

- 算法 / 关键逻辑变更须**同步更新技术文档**（`algorithms.md` / 架构图 / 实施说明），避免文档与实现脱节。
- 开发中遇到的问题与解决方案记入 `docs/dev-log.md`。
- 架构决策记入 `docs/adr/`（编号-kebab 命名，约定见 `docs/agents/domain.md`）；已有 `0001`~`0006` 记录 6 项已拍板决策，新决策续编号。

## Agent skills

### Issue tracker

Issues and specs live in GitHub Issues for FrancisLv/ai-gaokao-counselor, driven by the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

The five canonical triage roles use the default label strings: needs-triage, needs-info, ready-for-agent, ready-for-human, wontfix. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context layout: one `GLOSSARY.md` at the repo root plus `docs/adr/`. See `docs/agents/domain.md`.
