# 开发指南：从系统目标到当前可运行链路

本文面向理解项目和接手开发。算法精确契约以 [task2 实施文档](task2-implementation-plan.md)、[proto](../api/algo/v1/algo.proto) 和当前代码为准；历史问题与验收实测集中在 [dev-log](dev-log.md)。不要把架构目标当作已完成功能。

## 1. 这个系统解决什么问题

用户往往只知道分数，不知道该如何权衡学校、专业、城市和风险。目标是通过对话澄清偏好，用招生数据和确定性算法生成方案，再用可引用的章程证据解释方案。

职责分开有两个原因：LLM 适合理解和解释，但不应承担录取概率计算；算法能稳定计算，但不能编造政策或替用户决定偏好。

当前 algo MVP 仅支持**湖南、物理类/历史类、本科批、3+1+2 选科**。年份由请求指定，不按系统日期推断；当前测试以报考 2025 年及其历史窗口为例，不表示已经具备以后年份的数据。

## 2. 目标架构与实际完成度

目标调用方向：

```text
客户端 → gateway → orchestrator（LangGraph）
                         ├─→ algo → PostgreSQL 招生数据
                         ├─→ llm-gateway → 模型厂商
                         └─→ RAG → 章程向量库 / 本地 Embedding
```

| 模块 | 已有实现 | 尚未形成的能力 |
|---|---|---|
| algo | Kratos 三层、四 RPC、配置/装配、真实库和跨语言测试 | 多省/多批次、专业级最终填报、更多硬约束与概率校准 |
| orchestrator | gRPC 入口、图状态及部分节点、导入与图构建测试 | 完整策略/评审/报告链、真实 HITL 挂起恢复、检查点、algo 业务接线 |
| RAG | 章程解析切分、元数据、Embedding 封装、导入/检索辅助脚本 | 本轮未执行真实模型推理、入库或检索质量评测；复杂嵌套编号仍有限制 |
| gateway | HTTP 契约和服务入口 | 接口仍返回 TODO，鉴权、会话、SSE 和编排调用未完成 |
| llm-gateway | 类型化 `Chat/StreamChat`、角色/模型/价格路由、注册 prompt/schema、六类数据分类、原子预算、主备 attempt、未知费用对账、release-bound 缓存、类型化流和真实 PG 生成客户端测试 | 生产 provider 固定 fail closed；真实 provider/账单 adapter、真实付费模型验证和 orchestrator 入口接线未完成；`DELTA` 是最终守卫后的完整内容块，不是供应商 token 实时转发 |

**本轮真正验证的链路**是 Go/Python 测试客户端 → 新构建的 algo 子进程 → 本地 PostgreSQL → 四 RPC。它不是浏览器到完整咨询报告的全系统闭环。图能够构建、返回 `DONE` 事件，也不代表咨询流程已完成。

## 3. algo 为什么分三层

| 层 | 入口 | 责任与禁止事项 |
|---|---|---|
| service | [algo.go](../internal/algo/service/algo.go)、[dto.go](../internal/algo/service/dto.go) | 解析入参、调一个 usecase、转换 DTO、映射错误；不写 SQL 或业务编排 |
| biz | [recommend.go](../internal/algo/biz/recommend.go)、rank/prob/constraint/utility/optimize | usecase 编排；纯函数实现规则与数学；声明 Repo 接口，不依赖 pb 或 Kratos 错误 |
| data | [admission.go](../internal/algo/data/admission.go)、[rank.go](../internal/algo/data/rank.go) | 实现 Repo，持连接池，唯一执行 SQL 的层 |

启动顺序：`main -conf` → 读取单个 YAML → 扫描到 `conf.Bootstrap` → `wireApp` → 校验配置、建立连接池并 Ping → 注册 gRPC 服务。只加载 algo 自己的配置，不能把整个 configs 目录合并后使用。

`wire.go` 保存依赖声明；`wire_gen.go` 当前为手工维护的装配结果（此前 Wire 工具链不兼容）。修改 provider 后必须同步两者，并跑 `make check` 的正常及 `wireinject` 构建边界；`make api` 不会生成 Wire 装配代码。

### data 层阅读顺序与 SQL 修改流程

数据访问采用 **sqlc + pgx/v5**。SQL、查询类型、仓储流程和领域映射分开维护：

| 想看什么 | 文件 | 职责 |
|---|---|---|
| 候选加载的步骤 | [admission.go](../internal/algo/data/admission.go) | 检查报考年计划 → 读上一年组骨架 → 读专业明细 → 组装；历史和计划均批量查询 |
| 具体查询条件 | [queries/](../internal/algo/data/queries/) | 手写 SQL；命名参数、字段别名、过滤、关联、聚合和排序 |
| 查询参数/结果结构体 | [dbgen/](../internal/algo/data/dbgen/) | sqlc 自动生成 `Params`、`Row` 和类型化查询方法；无需手写 `Scan`，不要手改 |
| 行怎样变成业务对象 | [admission_mapping.go](../internal/algo/data/admission_mapping.go) | 字段转换、完整组键合并、专业保序和选科去重；不访问数据库 |
| 一分一段处理 | [rank.go](../internal/algo/data/rank.go) | 调用查询 → 提取本科线 → 解析区间；保留所有批次的数据行 |
| 连接与生命周期 | [data.go](../internal/algo/data/data.go) | 建池、Ping、创建 `dbgen.Queries`、统一 cleanup |

六条查询分布如下：`candidates.sql` 保存 `ListCandidateGroups` / `ListCandidateMajors`，`plans.sql` 保存 `HasEnrollmentPlan` / `ListGroupPlans`，`history.sql` 保存 `ListGroupHistory`，`rank.sql` 保存 `ListScoreRanks`。例如要增加专业展示字段，先改 `candidates.sql` 的列及别名，再生成，最后更新 `majorFromRow` 和映射测试；无需维护一串位置对应的扫描变量。

`dbgen` 类型只存在于 data 层；biz 的 Repo 接口和领域类型保持独立。映射测试使用生成的行结构体构造夹具，覆盖字段、空值、同名院校/不同组、列表顺序和缺失年份。

从仓库根目录执行：

```bash
make sqlc-install     # 首次安装固定版本 v1.30.0 到 bin/tools，不加入服务 go.mod
# 下载源不可用时，仅为本次命令指定：GOPROXY=https://goproxy.cn,direct make sqlc-install
make sqlc-generate    # 修改 queries/*.sql 或 schema 后重新生成
make sqlc-check       # 检查 SQL/schema 与已保存生成物一致，不改文件
make check test      # check 已包含 sqlc-check；默认测试不需要数据库
```

[sqlc.yaml](../sqlc.yaml) 只离线解析现有 `migrations/00_schema.sql`，不连接数据库、不执行其中的初始化语句；生成不是迁移。SQL 源和 `dbgen/*.go` 需一起纳入版本管理，与被忽略的 protobuf 生成物不同。普通 `go build` / `make build` 和现有 Go Dockerfile 直接编译已保存的 `dbgen`，无需安装 sqlc 或在运行时读取 SQL/schema；`make check` 首次执行则可能安装工具。

本次重构的真实库前后结果对照和性能取舍见 [dev-log §20](dev-log.md)。快照测试只输出摘要，供同一数据快照前后比较，不把可变招生数据的摘要硬编码成永久断言。

### 四 RPC 如何配合

1. **RankConvert**：读取报考年一分一段表，将分数转换成位次和百分位。它可以独立使用；EstimateProb 内部也会完成同样换算，调用方不必先调一次。
2. **EstimateProb**：换算 → 生成专业组候选 → 选科过滤 → 批量查历史和计划 → 算概率 → 分冲/稳/保 → 返回审计依据。它不负责最终志愿排序。
3. **CheckConstraints**：对传入候选再次做选科裁决，返回保留项和带原因的拒绝项，保留原概率与历史。适用于上游修改候选后复核；不是重新计算概率。
4. **OptimizeWishlist**：消费已有概率的候选池 → 四维效用打分 → 按风险偏好配比选择 → 总排序 → 上限内补齐/替换稳拿保底 → 生成报告。它不重查数据库，也不重新估计概率。

组主键是 `GroupKey{UniversityCode, MajorGroup}`，不能按院校名称去重。输入重复组会报错；没有概率的新候选应先回到概率计算链，不能直接混入最终优化。

### DTO 与错误模型

Go 领域类型使用嵌入结构，例如 `cwp.Probability`；protobuf 使用嵌套消息，例如 `cwp.prob.probability`。转换集中在 service，两者不是可直接替换的同一种结构。测试覆盖双向 round-trip、嵌套列表、效用 map、历史审计与最终排名。

上游应按稳定 `ErrorInfo.reason` 分支，而不是匹配中文文案。低于本科线、候选缺概率属于 `FailedPrecondition`，同时保留 Kratos HTTP code 400；其他具体映射见 [实施文档 §4.4](task2-implementation-plan.md)。未知内部错误和数据库连接错误不向客户端暴露 DSN 或原始底层异常。

`SafeGuardOK=false` 是**成功结果中的风险告警**，不是 RPC 失败。调用方必须展示它，不能因为响应成功就宣称保底达标。

## 4. 数据与算法：关键设计动机

### 数据从哪里来

| 数据源 | 用途 |
|---|---|
| `score_rank_map` | 报考年分数换位次、本科控制线、各年全科类人数分母 |
| `enrollment_plan` | 报考年可招生的专业组、报考年与前一年的计划变化 |
| `major_admission` | 上年组线/专业明细、选科文本、窗口内历史组线 |
| `university` / `major` | 院校层次、地域、实力字段及专业附属信息 |
| `school_admission` | 已有导入数据；当前候选/概率主链不依赖它 |
| `policy_doc` / `policy_chunk` | RAG 章程及向量；不参与 algo 概率计算 |

`migrations/00_schema.sql` 与后续编号 SQL 是结构/数据初始化材料，不是可以随意对现有数据库重放的升级命令。本轮测试没有执行迁移、更新数据或重新灌入章程。

### 为什么比较百分位

位次越小越靠前，百分位 `rank / totalCount` 也越小越好。不同年份考生人数不同，先归一化再比较，比直接把两年的原始位次相减更合理。

这里的分母是**同省、同科类、同年的完整一分一段表最大累计人数**，不是本科批人数。表按批次分数段互斥存储，查询分母不能只留下本科批；本科线另从本科批记录读取。

### 为什么不能使用报考年的录取结果

报考年为 Y、窗口为 W 时，只取 `[Y-W, Y-1]` 的历史。候选基础取上一年且须在报考年有计划，不读取报考年录取线来预测自身。缺报考年一分一段表或整批招生计划直接报错，不偷偷回退到库内最大年份。

概率采用**加权历史命中率**：在历史某年，用户百分位达到组线就记一次命中，较近记录权重更高；计划变化修正的是计算副本。返回的 `history_data` 保留原始位次/人数及其百分位，`data_years` 与实际有效历史一致。`basis.sample_years` 是配置窗口，不表示每个候选每年都有数据。

当前缺年份时按有效记录下标衰减，而非自然年份差；缺历史、不可靠位次/人数的记录不能用于估算。同组院校 ID/名称冲突或主键缺失的候选保守排除，等待离线数据治理，不任取一个院校身份。

### 为什么概率不是录取承诺

历史年数少且采用二元命中，输出是离散估计，同档会出现很多并列；组内专业变化、跨年组编码、竞争人数和政策变化也不被完整建模。效用分解决的是偏好排序，不能提高概率本身的校准程度。

滑档率采用各候选未录取概率的乘积，这是独立性近似。实际志愿之间高度相关，极小的乘积值**不能解释为几乎必然录取**。组级 match-any 只表示组内有匹配规则，不保证该组每个专业都能报。

### 为什么要强调确定性

同一数据快照、配置、请求应得到相同字段和列表顺序：SQL 有唯一并列排序键，概率按输入顺序计算，效用按分数再按组键全序排序，map 不用于决定输出顺序。跨请求不共享可变历史；当前仅有请求内选科解析缓存，没有 Redis 或跨请求历史缓存。

完整 protobuf 相等不等于原始序列化字节永远相同，尤其是 map 字段；本轮按字段和值、repeated 次序比较，不作字节级承诺。

## 5. 配置与本地启动

依赖：Go 版本至少满足 [go.mod](../go.mod)，系统 `protoc`、Kratos CLI 和 Go protoc 插件；Python 3.11+ 及 [pyproject.toml](../services/orchestrator/pyproject.toml) 中依赖。Docker 不是本地 algo 测试的前提。

从仓库根目录执行，`PY` 换成自己的 Conda 环境解释器绝对路径：

```bash
export PY="$(conda info --base)/envs/ai-gaokao-counselor/bin/python3"
"$PY" -m pip install -e ./services/orchestrator
make api PY="$PY"
make build
make test
make py-check py-test PY="$PY"
```

- `make api` 生成所有 Go/Python API 与 conf；不手改生成文件。Makefile 自动加入 GOPATH/bin，并从 Kratos 模块目录提供 `google/api/annotations.proto` include。
- Python 生成树在 `services/orchestrator/pb/api/...`，统一导入 `api.<服务>.v1`。Makefile 设置 orchestrator 与 pb 两个根路径；生成后打出的 wheel 也包含 `api` 命名空间。
- 首次安装没有 Go CLI/插件时，按 [Go Dockerfile](../deploy/go.Dockerfile) 的固定插件版本和 Kratos CLI 安装方式准备工具；不要把 Python 自带 protoc 与系统 `protoc` 混为一谈。

手动运行 algo（先确认端口空闲，不替换他人进程）：

```bash
export ALGO_DATA_DSN='postgres://USER@HOST:PORT/DATABASE?sslmode=disable'
./bin/algo -conf configs/algo.yaml
```

DSN 环境变量优先于 YAML；实际连接凭据只放本地环境，生产须按环境配置 TLS。`-conf` 缺省仍使用原路径，需要旁路启动时另备配置修改监听地址。

算法参数及合法范围见 [configs/algo.yaml](../configs/algo.yaml)、[实施文档 §4.1/§4.4](task2-implementation-plan.md)。关键语义：

- 配置在启动时合并默认值并校验，不是热更新；改 YAML 后需由操作者安排重启。
- proto3 标量没有 presence，多数字段的显式零与缺省等价，不能用零关闭计划修正/最少保底。
- 最大志愿数和最少保底是 config-only，不会按用户风险偏好自动重新推导；调整两者须保持合法关系。
- 风险偏好影响冲稳保配比，不降低保底红线；请求内 `per_category_limit` 当前不生效，MVP 保持全量候选。

`make run-orchestrator PY="$PY"` 只启动已有编排骨架，不代表补齐了 TODO 节点。llm-gateway 的 fake-provider 真实 PG 套件通过 `LLMGW_TEST_DSN` 运行；生产装配保持 fail-closed，不读取 API key、不调用付费模型。普通与流式主行为测试都从生成的 `LLMGatewayClient` 进入，强结构化账本查询走独立 `internal/llmgw/data/queries/` 与 sqlc 生成包。

## 6. 可重复测试与性能验收

真实库测试使用**显式只读** DSN，测试会隔离生产连接变量覆盖；不要用生产可写账号做验收：

```bash
export ALGO_TEST_DSN='postgres://USER@HOST:PORT/DATABASE?sslmode=disable&default_transaction_read_only=on'
make check test
make test-integration test-race coverage
make test-fuzz
make py-test py-check PY="$PY"
make tcp-smoke PY="$PY"
make perf PY="$PY"
make bench
```

| 入口 | 验证内容 |
|---|---|
| `check` | sqlc 生成一致性、全仓构建、默认/integration/wireinject vet、wireinject 构建边界 |
| `test` | 无库默认单测；全仓其余骨架不等于有业务测试 |
| `test-integration` | 真库来源、窗口、分母、NULL、两科类、四 RPC、TCP 装配 |
| `test-race` | 真实库三轮 shuffle、并发客户端、完整结果对照、取消/超时 |
| `coverage` | 分别统计 biz/service；data 使用真库覆盖，不以全仓平均代替 |
| `test-fuzz` | 选科语法和数字区间解析；固定种子加随机输入 |
| `py-test` | 离线合成夹具、已有本地章程不变量、模型替身、导入/图构建；不下载模型、不入库 |
| `tcp-smoke` | 临时 YAML、空闲回环端口、新构建进程、Go/Python 四 RPC 和错误详情 |
| `perf` / `bench` | TCP 尾延迟与固定夹具/真实库 benchmark；不要与 race 同时跑 |

`tcp-smoke` 只清理自己启动的子进程，不触碰已有服务；Python 接口导入、历史审计、错误状态、嵌套 DTO 均参与断言。未设 DSN 的跳过不算真库验收。

Phase 6 验收（2026-10-30）实测：biz **93.8%**、service **81.0%**、data **71.2%**；默认构建/测试、三轮 race、两项 fuzz、12 项 Python 离线测试、wheel 构建/隔离目录导入均通过。完整性能环境与数据只保留在 [dev-log §17](dev-log.md)：单并发/8 并发各 100 次零错误，单并发最大延迟低于 2 秒。

### Docker 验证边界

OrbStack Linux/arm64 上已完成：原生 Compose 配置校验、四个业务镜像构建、algo 容器四 RPC、Python 镜像离线测试及其余服务启动检查。最初的镜像仓库网络阻塞见 [dev-log §18](dev-log.md)，经用户同意使用 DaoCloud 后的修复与实测见 §19。

```bash
make compose-check         # 只展开和校验配置，不启动服务
make docker-build          # 默认官方基础镜像和 Go 依赖源
# 无代理环境：本次构建使用 DaoCloud 基础镜像与国内 Go 模块源
make docker-build IMAGE_MIRROR=m.daocloud.io/ BUILD_GOPROXY=https://goproxy.cn,direct
# 可加 DOCKER_SERVICES=algo 或 DOCKER_SERVICES=orchestrator 只构建单服务
```

- `IMAGE_MIRROR` 是带尾部 `/` 的可选前缀；只作用于 Makefile 业务镜像构建，不改 Docker/OrbStack 全局配置，也不替换 Compose 的 PostgreSQL 等基础设施镜像。
- `GO_IMAGE`、`GO_RUNTIME_IMAGE`、`PYTHON_IMAGE` 可分别覆盖完整镜像引用，包括 `@sha256:...`；默认仍是官方镜像。DaoCloud 是第三方镜像服务，拉取时的 digest 记录在 §19，不等于已独立完成官方签名验证。
- `BUILD_GOPROXY` 只传给 Go 构建阶段，默认 `https://proxy.golang.org,direct`；保留校验和验证，不修改宿主 `go env`。
- Python 镜像默认从官方 `https://download.pytorch.org/whl/cpu` 预装 CPU 版 torch，避免无 GPU 环境拉取 CUDA 运行库。可通过 `TORCH_INDEX_URL` 显式覆盖，GPU 部署需另行验证；安装依赖不等于下载模型权重。
- 构建入口使用无效占位 API key 仅满足 Compose 变量展开，不调用模型、不启动服务。根 `.dockerignore` 排除本地环境、数据、缓存和宿主 protobuf 生成物，镜像从 proto 自行生成。

镜像内离线测试（不挂载源码，使用镜像内代码，语料只读）：

```bash
docker run --rm --network none --read-only --tmpfs /tmp \
  -e PYTHONDONTWRITEBYTECODE=1 -e HF_HUB_OFFLINE=1 -e TRANSFORMERS_OFFLINE=1 \
  -e CHARTER_CORPUS_ROOT=/charters \
  -v "$PWD/docs/charters/2025:/charters:ro" \
  ai-gaokao-counselor-orchestrator:latest \
  python -m unittest discover -s tests -p 'test_*.py' -v
```

上述 12 项测试全通过；不提供语料挂载和 `CHARTER_CORPUS_ROOT` 时仅跳过语料用例，其余 11 项通过。显式指定不存在的语料目录会失败，避免误报验收通过。另在清空 `PYTHONPATH`、工作目录为 `/tmp` 的容器中验证已安装包导入，`pip check` 通过。

algo 冒烟采用临时 YAML（志愿上限12、保底数3）、`-p 127.0.0.1::9002` 随机回环端口、`-conf` 挂载配置，以及经 `host.docker.internal` 访问本机 PostgreSQL 的只读 DSN。宿主和 Python 镜像分别执行 `tests/smoke_algo.py`，四 RPC/审计/错误详情均通过。测试容器和临时配置已清理；没有重启旧服务或修改数据库。

Compose 含独立数据库初始化和基础设施，开发默认复用本机 PostgreSQL，不需要 `make up`。**未验证**整套 Compose 服务互联与初始化、Linux/amd64、GPU、真实模型、完整咨询闭环；gateway/orchestrator 的端口就绪不等于业务完成。llm-gateway 的治理 seam 已由真实 PG fake-provider 测试覆盖，但生产 provider 仍 fail closed，真实计费、账单拉取与实时 token 流均未接线。

## 7. 常见问题与修复证据

| 现象 | 根因 | 解决方案及回归证据 |
|---|---|---|
| 相同查询专业顺序变化 | SQL 并列无唯一排序键 | 专业代码+主键排序；重复完整响应对照，dev-log §15 |
| 两类错误不是 FailedPrecondition | Kratos 默认 HTTP 400 映射 | 本地状态包装保留 reason；Go/Python 客户端验证 |
| 保底补齐后超出志愿上限 | 无界 append | 上限内补入/替换；不同池大小与风险偏好的不变量测试 |
| 重复候选/未知分档/非法概率进入优化 | 入口缺边界校验 | 组唯一性与有限值校验；最小失败用例转绿，§16 |
| 历史审计为空或被计划修正污染 | 仅在计算临时对象回填 | 原始有效历史独立副本；两科类和跨语言审计断言 |
| Python 生成失败或导入报错 | 缺 google/api include；生成路径与导入不一致 | Makefile include、统一 api 命名空间、wheel 构建导入 |
| 章程正文消失/切块超长 | 无章节入口遗漏、前言未保留、长句及尾条无上限 | 正文保留、硬切兜底、前缀预算、完整范围路径与元数据；12 项离线测试 |
| Embedding 到入库才发现维度错误 | 只打 warning 继续执行 | 构造时 ValueError；空列表返回稳定二维数组，替身验证 |

详细复现、修改文件和历史性能差异见 [dev-log §14–20](dev-log.md)。更新切分代码不会自动更新已有向量库；如需重新切分和入库，应单独确认、备份并执行。

## 8. 后续开发顺序

1. 本地 Linux/arm64 Docker 验证已完成；部署前再做目标架构与整栈验收。另行治理同组院校身份冲突，保留数据来源与版本。
2. 将 algo 类型化客户端接入 orchestrator，完善追问、画像确认、真实中断恢复与候选编辑。
3. 完成 RAG 检索器和离线评测，增加上下文扩展；不以复杂编号切分的有限覆盖替代检索质量评估。
4. 实现 gateway 会话/SSE，并把已完成的 llm-gateway 治理 seam 接入 orchestrator；之后才验收全系统咨询闭环。
5. 用独立留出数据评估概率校准和真实滑档风险，再决定是否升级模型；本轮的防泄漏测试不等于已经完成预测准确率评估。
