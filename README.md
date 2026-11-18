# AI Gaokao Counselor · AI 高考志愿智能决策系统

> 面向需求尚不清晰的用户，提供高考志愿填报决策支持：**多 Agent 编排 + RAG + 领域算法**。
> Go（Kratos）负责「控制与治理」，Python（LangGraph）负责「智能与检索」，靠 proto/gRPC 解耦。

---

## 这是什么

一个把「模糊的人」问清楚、再给出**可解释、可溯源**志愿方案的决策支持系统。
传统填报工具假设「用户已经知道自己要什么」，只给筛选器；本项目解决的是
**「用户不知道该问什么」** —— 交互采集 → 检索 → 冲稳保 → 约束校验 → 概率估计 → 可解释报告。

核心原则：**LLM 负责语义与生成，确定性代码负责规则与数值**（LLM 不许算数，算法不许编话）。

---

## 目标服务拓扑（并非全部已实现）

```
客户端 → svc-gateway → svc-orchestrator → { svc-llm-gateway, svc-algo } → 模型 / DB
```

| 服务 | 目录 | 语言 / 框架 | 职责 |
|------|------|-------------|------|
| `svc-gateway` | `cmd/gateway` | Go · Kratos (HTTP/SSE) | 入口、鉴权、限流、会话、HITL 推送 |
| `svc-orchestrator` | `services/orchestrator` | Python · LangGraph | 编排图、检索、本地推理、评测 |
| `svc-llm-gateway` | `cmd/llm-gateway` | Go · Kratos (gRPC) | 模型路由 / 缓存 / 预算 / 记账（持有 key） |
| `svc-algo` | `cmd/algo` | Go · Kratos (gRPC) | 位次 / 概率 / 选科约束 / 志愿优化 |

边界铁律：**服务之间只经 `api/` 生成的 client/server 通信；禁止跨服务 import 内部实现。**

---

## 目录结构

```
ai-gaokao-counselor/
├── api/                     # 契约区：proto 单一事实来源（Go/Python 共用）
│   ├── common/v1/           #   Trace / QueryFilters 等共享类型
│   ├── gateway/v1/          #   对外 HTTP API            —— Go 实现
│   ├── orchestrator/v1/     #   StartGraph / ResumeGraph —— Python 实现
│   ├── llmgw/v1/            #   Chat / Stream            —— Go 实现
│   └── algo/v1/             #   Rank / Prob / Constraint —— Go 实现
├── cmd/                     # Go 半区：3 个 Kratos 入口
├── internal/                # Go 半区：实现（gateway / llmgw / algo / pkg）
├── services/orchestrator/   # Python 半区：LangGraph 服务
├── conf/                    # algo 配置 proto
├── configs/                 # 各服务配置（algo 已接入加载）
├── deploy/                  # Dockerfile / docker-compose / Caddyfile
├── migrations/              # 00_schema 与编号数据初始化 SQL
├── data/golden/             # 标注问答集（评测用）
└── docs/                    # 架构、ADR、复盘
```

---

## 前置依赖

- Go ≥1.25（以 go.mod 为准）、Kratos CLI（`go install github.com/go-kratos/kratos/cmd/kratos/v2@latest`）及 protoc Go 插件
- 系统 `protoc`；Python 的 grpcio-tools 不会代替 Go 构建需要的系统编译器
- Python 3.11+，依赖见 `services/orchestrator/pyproject.toml`
- 本机 PostgreSQL；Docker/Compose 仅用于后续容器验证，当前不是本地开发前提

## 快速开始

在仓库根目录执行，使用项目 Conda 环境的解释器：

```bash
export PY="$(conda info --base)/envs/ai-gaokao-counselor/bin/python3"
"$PY" -m pip install -e ./services/orchestrator
make api PY="$PY"       # 生成所有 API 与配置代码
make build              # 编译三个 Go 服务
make check test         # SQL 生成一致性、构建、vet、默认单测
make py-check py-test PY="$PY"
```

algo 的 SQL 位于 `internal/algo/data/queries/`，参数/结果结构体由固定版本 sqlc v1.30.0 生成到 `dbgen/` 并随源码保存。改 SQL 后执行 `make sqlc-generate`；`make check` 自动检查生成一致性，首次可能安装工具。普通构建无需 sqlc，阅读顺序和修改示例见 [开发指南](docs/development-guide.md)。

真实库及跨语言验证（DSN 替换为自己的只读连接；不会迁移或重新导入数据）：

```bash
export ALGO_TEST_DSN='postgres://USER@HOST:PORT/DATABASE?sslmode=disable&default_transaction_read_only=on'
make test-integration test-race coverage
make tcp-smoke PY="$PY"
make perf PY="$PY"
```

运行 algo：`./bin/algo -conf configs/algo.yaml`，连接可用 `ALGO_DATA_DSN` 覆盖。确认端口空闲后再启动，不要替换正在使用的旧进程。更多配置、工具安装和调试说明见 [开发指南](docs/development-guide.md)。

可选 Docker 镜像构建（仅构建，不启动全栈、不修改全局代理；默认不传参数时使用官方源）：

```bash
make docker-build IMAGE_MIRROR=m.daocloud.io/ BUILD_GOPROXY=https://goproxy.cn,direct
```

上述命令经用户同意使用 DaoCloud 镜像服务，Go 依赖使用 goproxy.cn；Python 镜像默认安装官方 CPU 版 torch。详见开发指南中的 Docker 验证边界。

---

## 状态

- **algo 已完成 Phase 1–6**：湖南物理类/历史类本科批，四 RPC、Kratos 三层、真实库/Go-Python TCP、并发竞态与性能验收已验证。
- **RAG 已有基础实现**：章程切分、Embedding、pgvector ‖ pg_trgm 双路混合检索（RRF 融合）和辅助脚本；检索质量尚未经 golden set 量化验收，zhparser 全文一路与重排待后续阶段。
- **全系统尚未闭环**：gateway/llm-gateway 业务为 TODO，LangGraph 后续节点、HITL 恢复及服务业务接线待完成。
- **本地容器验证已完成**：Linux/arm64 下 Compose 配置、四镜像构建、algo 四 RPC、Python 12 项离线测试及其余服务启动检查通过。全栈互联、amd64/GPU 和生产部署未验证；未修改现有数据库。

文档入口：[开发指南](docs/development-guide.md) · [算法通俗说明](docs/algorithms.md) · [权威实现文档](docs/task2-implementation-plan.md) · [开发与验收记录](docs/dev-log.md) · [完整导航](docs/README.md)。
