# docs

架构与决策记录。完整架构文档见 `/Users/francis/agent-architecture.html`（可拷入本目录）。

建议往这里放：

- `architecture.md`：服务划分、调用拓扑（单向 DAG）、数据流
- `adr/`：关键决策记录
  - 0001 用 monorepo 而非多仓
  - 0002 编排用 LangGraph，不写通用状态机
  - 0003 数值走 SQL，语义走 RAG
  - 0004 算法放 Go、用独立 svc-algo
- `postmortem/`：复盘
