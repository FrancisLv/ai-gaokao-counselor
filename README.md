# AI Gaokao Counselor · AI 高考志愿智能决策系统

> 面向「面向模糊用户」的高考志愿填报：**多 Agent 编排 + RAG + 领域算法**。
> Go（Kratos）负责「控制与治理」，Python（LangGraph）负责「智能与检索」，靠 proto/gRPC 解耦。

---

## 这是什么

一个把「模糊的人」问清楚、再给出**可解释、可溯源**志愿方案的决策支持系统。
传统填报工具假设「用户已经知道自己要什么」，只给筛选器；本项目解决的是
**「用户不知道该问什么」** —— 交互采集 → 检索 → 冲稳保 → 约束校验 → 概率估计 → 可解释报告。

核心原则：**LLM 负责语义与生成，确定性代码负责规则与数值**（LLM 不许算数，算法不许编话）。

---

## 服务拓扑（单向 DAG，无环）

```
客户端 → svc-gateway → svc-orchestrator → { svc-llm-gateway, svc-algo } → 模型 / DB
```

| 服务 | 目录 | 语言 / 框架 | 职责 |
|------|------|-------------|------|
| `svc-gateway` | `cmd/gateway` | Go · Kratos (HTTP/SSE) | 入口、鉴权、限流、会话、HITL 推送 |
| `svc-orchestrator` | `services/orchestrator` | Python · LangGraph | 编排图、检索、本地推理、评测 |
| `svc-llm-gateway` | `cmd/llm-gateway` | Go · Kratos (gRPC) | 模型路由 / 缓存 / 预算 / 记账（持有 key） |
| `svc-algo` | `cmd/algo` | Go · Kratos (gRPC) | 位次 / 概率 / 规则 / 优化 / 投档模拟 |

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
├── configs/                 # 每角色一份 Kratos 配置
├── deploy/                  # docker-compose / Caddyfile / k8s
├── migrations/              # SQL 迁移
├── data/golden/             # 标注问答集（评测用）
└── docs/                    # 架构、ADR、复盘
```

---

## 前置依赖

- Go 1.22+、Kratos CLI（`go install github.com/go-kratos/kratos/cmd/kratos/v2@latest`）
- Python 3.11+、`grpcio-tools`
- Docker + Docker Compose（本地用 OrbStack 即可）
- protoc 由 Kratos / grpcio-tools 间接提供

## 快速开始

```bash
make api        # 1) 由 api/*.proto 生成 Go + Python 代码（关键：单一事实来源）
make tidy       # 2) 拉取 Go 依赖
make build      # 3) 编译三个 Go 服务
make up         # 4) 起全栈（compose）
```

---

## 状态

本仓库为**骨架（skeleton）**：目录、契约、入口与核心桩函数已就位；
`internal/*` 内的业务逻辑与 LangGraph 图节点为待实现（标注 `TODO`）。

> ⚠️ 骨架在**未安装 Go / protoc / docker 的机器上生成**，尚未编译验证。
> 首次构建请按上面「快速开始」执行 `make api && make tidy && make build`。
