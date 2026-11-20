# 开发问题记录

项目开发过程中遇到的决策、踩坑与解决方案。

---

## 1. 招生章程切分粒度：按章 vs 按条

**问题**：Task 1 章程 ETL 中，文档切分应该按「章」还是按「条」？

**分析**：

| 维度 | 按章切分 | 按条切分 |
|------|---------|---------|
| chunk 大小 | 通常 1000-3000+ 字，偏大 | 通常 50-300 字，偏小 |
| 检索精度 | 低：一个 chunk 里混杂多个主题 | 高：每条聚焦一个规则 |
| 上下文完整性 | 好：章内信息不丢 | 差：单条可能缺上下文 |
| 命中噪声 | 高：召回一整章，大部分内容无关 | 低：精准命中具体条款 |

**结论**：两种都不完美。采用**条为原子 + 智能合并**策略。

**方案**：

1. **原子单位是「条」**：每条招生章程的 `第X条` 是语义最小单元
2. **短条合并**：连续短条（累计 < 300 字）合并为一个 chunk，直到落入 300-600 字区间
3. **长条拆分**：超过 600 字的条，按段落/语义边界拆分
4. **每条都带父级路径**：`第三章 录取规则 > 第3.2条 平行志愿`，检索命中时 LLM 知道上下文
5. **重叠用「前一条尾部」实现**：不是字符级 15% 重叠（会破坏条款边界），而是每个 chunk 带上它所在章的概述句作为前缀

**示例**：

```
第X章 录取规则          ← 作为 section_path 保留
  ├── 第1条  ...50字    ← 太短，和下一条合并
  ├── 第2条  ...80字    ← 合并后 130字，仍短
  ├── 第3条  ...200字   ← 合并后 330字 ✅ 落入 300-600 区间
  ├── 第4条  ...500字   ← 独立成 chunk ✅
  └── 第5条  ...1500字  ← 按段落拆分，保留完整语义
```

**真实场景验证**：用户问「清华大学转专业政策是什么」——
- 按章切分：命中「第三章 录取规则」，3000 字里只有 200 字讲转专业，其余都是噪声
- 按条切分：命中「第三章 > 第15条 转专业」，精准 150 字，LLM 直接引用

**参考**：架构文档第 7.2 节「切分策略（chunk）」——按「条款」切分，保留父级标题路径，块大小 300-600 字，重叠 15%。

---

## 2. RAG 检索硬过滤：学校名称 + 年份如何高效过滤

**问题**：RAG 检索时需要按学校名称和年份过滤，但 `policy_chunk` 表只有 `doc_id` 关联 `policy_doc`，本身没有 `school_name` 和 `year` 字段。如果 JOIN `policy_doc`，对 pgvector 的 ANN 查询很不友好（过滤应在向量搜索之前完成）。

**分析**：

| 方案 | 做法 | 优点 | 缺点 |
|------|------|------|------|
| A. JOIN 查询 | `policy_chunk JOIN policy_doc ON doc_id` | 无冗余，数据一致 | ANN 前无法高效过滤，查询慢 |
| B. meta JSONB 冗余 | chunk 写入时冗余 `school_name`、`year` 到 `meta` 字段 | 过滤在 chunk 表内完成，无需 JOIN | 少量存储冗余 |
| C. 独立列 | 在 chunk 表加 `school_name`、`year` 列 | 查询最快 | 表结构膨胀，且这些字段只用于过滤 |

**结论**：采用**方案 B（meta JSONB 冗余 + GIN 索引）**。

**方案**：

1. ETL 入库时，每个 chunk 的 `meta` 字段写入：
   ```json
   {"school_name": "清华大学", "year": 2025, "province": "北京"}
   ```
2. 建 GIN 索引：`CREATE INDEX idx_chunk_meta ON policy_chunk USING gin (meta);`
3. 检索时直接过滤：
   ```sql
   WHERE meta @> '{"school_name":"清华大学","year":2025}'
   ```
4. pgvector ANN 查询在同一表内完成，无需 JOIN

**为什么这样更好**：
- 架构文档第 7.3 节明确「硬过滤优先」——省份/年份/科类必须作为 filter 而非排序因素
- pgvector 支持「先按 WHERE 过滤再 ANN」，过滤条件在同一张表上效率最高
- `meta` JSONB 可扩展，后续加 `track`（科类）等字段无需改表结构

**参考**：架构文档第 7.5 节「混合查询（filter + ANN）的坑」——给常见过滤维度做部分索引或先过滤再在子集里搜。

---

## 3. Chunk 重叠策略：章概述前缀 vs 字符级重叠

**问题**：切分后的 chunk 需要上下文重叠，但传统字符级 15% 重叠会切断条款边界，破坏语义完整性。

**分析**：

| 方案 | 做法 | 优点 | 缺点 |
|------|------|------|------|
| A. 字符级重叠（15%） | 上一 chunk 尾部 15% 字符重复到下一 chunk 头部 | 简单通用 | 可能切断条款，语义破碎 |
| B. 章概述前缀（采用） | 每个 chunk 带上所在章的概述句作为前缀 | 条款完整，上下文清晰 | 需要提取章概述 |

**结论**：采用**方案 B（章概述前缀）**。

**示例**：

原文：
```
第三章 录取规则

3.1 学校采用平行志愿录取方式，投档比例不超过1:1.05。
3.2 进档考生按分数优先原则分配专业，不设专业级差。
3.3 同等条件下，优先录取相关科目成绩较高的考生。
```

切分后：
```
Chunk A:
  前缀: "【第三章 录取规则】本章规定学校招生录取的基本原则和程序。"
  内容: "3.1 学校采用平行志愿录取方式，投档比例不超过1:1.05。"

Chunk B:
  前缀: "【第三章 录取规则】本章规定学校招生录取的基本原则和程序。"  ← 同一前缀
  内容: "3.2 进档考生按分数优先原则分配专业，不设专业级差。"

Chunk C:
  前缀: "【第三章 录取规则】本章规定学校招生录取的基本原则和程序。"  ← 同一前缀
  内容: "3.3 同等条件下，优先录取相关科目成绩较高的考生。"
```

**为什么这样更好**：
- 重叠的目的不是「重复文字」，而是「让每个 chunk 被单独检索命中时，LLM 仍能知道它在文档中的位置和上下文」
- 章概述前缀用一句话就做到了，不需要重复上一 chunk 的尾部文字
- 条款边界保持完整，embedding 质量更高（前缀是稳定语义锚点，不是噪声）

---

## 4. 全文检索（tsv）分阶段演进策略

**问题**：`policy_chunk` 表有 `tsv tsvector` 字段用于全文检索，但中文分词是坑（PostgreSQL 默认分词对中文无效）。是否一开始就启用 tsvector？

**分析**：

| 阶段 | 方案 | 理由 |
|------|------|------|
| M1 起步 | 只用 pgvector，tsv 留空 | 最快跑通，验证 embedding 质量 |
| M2 评测后 | 加 pg_trgm 关键词匹配 | 利用已有扩展，无需中文分词器，用 `%` 操作符做子串匹配 |
| M3+ 生产前 | 装 zhparser，启用真正的 tsvector | 中文分词质量上去后，做 pgvector ‖ tsvector → RRF 融合 |

**结论**：分阶段演进，不一开始就启用 tsvector。

**后续 TODO**：

- [ ] **M2 阶段**：实现 pg_trgm 关键词匹配
  - 在 `retriever.py` 中加 `keyword_search()` 方法
  - 用 `WHERE text % '关键词'` 或 `WHERE text LIKE '%关键词%'` 做匹配
  - 与 pgvector 结果做简单加权融合

- [ ] **M3+ 阶段**：启用 zhparser + tsvector
  - 安装 zhparser 扩展：`CREATE EXTENSION zhparser;`
  - 创建中文分词配置：`CREATE TEXT SEARCH CONFIGURATION chinese (PARSER = zhparser);`
  - 回填 tsv 字段：`UPDATE policy_chunk SET tsv = to_tsvector('chinese', text);`
  - 实现 RRF 融合：pgvector 结果 ‖ tsvector 结果 → 倒数排名融合
  - 建 GIN 索引：`CREATE INDEX idx_chunk_tsv ON policy_chunk USING gin (tsv);`

**参考**：架构文档第 7.5 节「中文全文检索的坑」——三种解法：zhparser/pg_jieba、pg_bigm/pg_trgm、以向量为主关键词为辅。

---

## 5. 国内环境 HuggingFace 模型下载失败

**问题**：运行 `ingest_charters.py` 时，加载 `BAAI/bge-large-zh-v1.5` 模型失败，报错 `[Errno 54] Connection reset by peer`。

**原因**：国内网络无法访问 `huggingface.co`。

**解决方案**：在 `embedder.py` 模块顶层设置 `HF_ENDPOINT` 环境变量，使用国内镜像 `hf-mirror.com`。

**关键代码**（`services/orchestrator/rag/embedder.py`）：
```python
# 在导入 sentence_transformers 之前设置镜像
if "HF_ENDPOINT" not in os.environ:
    os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
```

**踩坑点**：
- 环境变量必须在 `import sentence_transformers` **之前**设置，否则无效
- 不能放在 `Embedder.__init__()` 里，因为此时库已经导入
- 放在模块顶层，确保 import 时就设置好

**参考**：https://hf-mirror.com/

---

## 6. PostgreSQL 数据库配置：复用本地数据库 vs Docker

**问题**：项目 `docker-compose.yml` 中定义了 PostgreSQL 容器（用户 `app`，数据库 `gaokao`），但本地开发是否需要 Docker？

**结论**：不需要，直接复用本地 PostgreSQL。

**配置**：
- 数据库：`ai_gaokao_counselor`（本地已存在）
- 用户：`francis`（本地管理员）
- DSN：`postgresql://francis@localhost:5432/ai_gaokao_counselor`
- 已启用扩展：`vector`（pgvector）、`pg_trgm`

**初始化步骤**：
```bash
# 1. 启用 pgvector 扩展
psql -U francis -d ai_gaokao_counselor -c "CREATE EXTENSION IF NOT EXISTS vector;"

# 2. 运行 schema + 导入数据（migrations/ 按字母序：00_schema 建表 → 01~06 灌数）
psql -U francis -d ai_gaokao_counselor -f migrations/00_schema.sql
for f in migrations/0[1-6]_*.sql; do psql -U francis -d ai_gaokao_counselor -f "$f"; done

# 3. 运行导入脚本
python scripts/ingest_charters.py --dsn "postgresql://francis@localhost:5432/ai_gaokao_counselor"
```

**优势**：
- 无需启动 Docker 容器，减少资源占用
- 复用现有数据库，数据持久化更简单
- 本地开发调试更方便

---

## 7. Chunk 合并逻辑 Bug：尾部短条未合并导致平均长度偏低

**问题**：质检报告显示 chunk 平均长度 283 字符，低于目标区间 300-600 字。

**原因**：`_merge_short_articles()` 函数在章结束时，会无条件 `_flush()` 输出缓冲区，即使缓冲区只有 100-200 字的短条。

**示例**：
```
第三章 录取规则
  第1条  50字  → 缓冲
  第2条  80字  → 缓冲（累计130字，< 300）
  ← 章结束，_flush() 输出 130字的 chunk ❌
```

**修复方案**：尾部短条应合并入上一个 chunk，而非独立输出。

**关键代码**（`services/orchestrator/rag/etl.py`）：
```python
# 处理尾部短条：如果 < MIN_CHUNK_LEN，合并到上一个 chunk
if buf_text and len(buf_text) < MIN_CHUNK_LEN:
    if chunks:
        # 合并到最后一个 chunk
        last_chunk = chunks[-1]
        last_chunk.text = f"{last_chunk.text}\n{buf_text}".strip()
        # 更新 section_path 以包含新合并的条
        if len(buf_articles) == 1:
            last_chunk.section_path = f"{last_chunk.section_path}~{buf_articles[0]}"
    else:
        # 没有前一个 chunk，直接输出（即使短）
        _flush()
elif buf_text:
    _flush()
```

**修复效果**：
| 指标 | 修复前 | 修复后 | 改善 |
|------|--------|--------|------|
| Chunk 总数 | 103 | 84 | -18.4% |
| 平均长度 | 283 字符 | 345 字符 | +21.9% |
| 中位数 | 262 字符 | 318.5 字符 | +21.6% |

**结论**：平均长度进入 300-600 目标区间，合并逻辑符合预期。

---

## 8. ETL 多格式编号解析：提升数据覆盖率从 78% 到 99.4%

**问题**：全量导入后，628 个文件（22%）切分结果为空，数据严重缺失。

**原因分析**：
1. **中文数字格式**：上海市学校常用「一、二、三...」编号，而非「第X章」
2. **前导全角空格**：部分文件有 `  第X章` 格式，正则未匹配前导空白
3. **嵌套结构**：中文数字编号下有子编号（七、下面又有 一、二、三...）

**修复方案**：

1. **增强正则表达式**：
   ```python
   # 支持前导全角/半角空格
   _RE_CHAPTER = re.compile(r"^[\s  ]*(第[一二三四五六七八九十百千\d]+章)\s*(.*)")
   _RE_ARTICLE = re.compile(r"^[\s  ]*(第[一二三四五六七八九十百千\d]+条)\s*(.*)")
   
   # 新增中文数字格式（只匹配行首无缩进的）
   _RE_CN_NUM_SECTION = re.compile(r"^([一二三四五六七八九十百]+)[、.．]\s*(.*)")
   ```

2. **改进合并逻辑**：
   - 连续短章合并至 300-600 字区间
   - 超长章按段落拆分
   - 章概述前缀保留上下文

3. **处理嵌套结构**：
   - 只将行首无缩进的中文数字视为顶层章
   - 缩进的中文数字视为内容

**修复效果**：

| 指标 | 修复前 | 修复后 | 改善 |
|------|--------|--------|------|
| 文档总数 | 2,249 | 2,859 | +27% |
| Chunk 总数 | 21,895 | 25,546 | +17% |
| 数据覆盖率 | 78% | 99.4% | +21.4% |
| 平均长度 | 344 字符 | 356 字符 | ✅ |

**结论**：数据覆盖率从 78% 提升至 99.4%，仅剩 18 个文件为空（特殊格式或内容过短）。

**后续优化**：
- 对于剩余 18 个空文件，可考虑人工检查或特殊解析逻辑
- 中文数字格式的 section_path 不含 `>` 分隔符（如 `一、院校全称~六、院校招生管理机构`），属于正常现象

---

## 9. svc-algo Phase 1：Kratos 三层骨架 + rank 垂直切片落地

**目标**（task2 §5 里程碑 Phase 1）：建立 svc-algo 的 kratos 正统三层（biz/data/service）+ conf + wire 骨架，完整实现 **rank 垂直切片**（RankConvert RPC 全链路可用），其余 3 个 usecase 为可编译 stub（返回 ErrNotImplemented）。

**落地内容**：

- **conf**：新建 `conf/conf.proto`（Bootstrap{Server,Data,Algo}，Algo 段 12 字段）+ protoc 生成 `conf/conf.pb.go`；`configs/algo.yaml` 补 `algo:` 段。
- **biz**（`package biz`，不碰 SQL/不 import pb/不 import kratos errors）：`types.go`（§2 全部领域 struct + config struct）、`repo.go`（AdmissionRepo/RankRepo 接口）、`rank.go`（ScoreRankEntry + ConvertByScore 纯函数 + RankToPercentile + RankUsecase）、`config.go`（conf.Algo→config struct，proto3 0 值兜底）、`recommend.go`/`optimize.go`（stub）、`provider.go`（ProviderSet）。
- **data**（pgx/v5）：`data.go`（Data{pgxpool} + NewData 启动即 Ping，dsn 不可达则启动 fail-fast）、`rank.go`（LoadScoreRankMap 真实 SQL + parseRange）、`admission.go`（stub）。
- **service**（薄）：`algo.go`（Service + 4 RPC，RankConvert 已实现，余 3 个透传 stub err）、`dto.go`（toPbRankConvertReply）、`error.go`（toKratosErr：sentinel→Kratos reason 枚举）。
- **cmd/algo**：`main.go` + `wire.go`（wireinject tag）+ 手写 `wire_gen.go`。
- **删旧骨架**：`internal/algo/{service.go,rank,prob,optimize,constraint}`。

**验证**：`go build ./...` exit=0、`go vet`（含 wireinject tag）通过、`go test ./internal/algo/biz` ok（11 测试）、`make conf` regen 成功、`make build-algo` 成功、冒烟测试 gRPC 监听 :9002 ✓。

**端到端真实 RPC 验证（C 方案）**：用生成的 algov1 client stub 写临时 Go 客户端拨号 localhost:9002 调 RankConvert（临时把 dsn 指本地库，验完还原），跑通 client stub → TCP → service handler → biz → data → 真实 PostgreSQL 全链路：
- 2025/690 → rank=53, percentile=0.000166；2025/450 → rank=153634（与 data 层直测一致）。
- 错误映射（toKratosErr → gRPC status）均正确：本科线下 404→InvalidArgument、非法年份 abc→InvalidArgument、缺数据年 2030→Internal。

**✅ dsn 处理（已解决）**：`configs/algo.yaml` 的 dsn 已改为本地库 `postgres://francis@localhost:5432/ai_gaokao_counselor?sslmode=disable`（项目约定：本地开发直连本机 PG、不走 Docker，见 #6）；docker-compose 部署时由容器侧覆盖为 `app:app@postgres:5432/gaokao`。同时 `NewData` 改为**启动即 Ping**（5s 超时）：dsn 不可达/配错在启动阶段就 panic fail-fast，不再拖到首次 RPC。因此本地 `make run-algo` 启动即连本地 PG，可直接用 APIfox（gRPC 模式）连 `localhost:9002` 测（Kratos v2.7.3 内置 gRPC reflection，APIfox 免导入 proto 自动发现服务）。

**docker-compose 部署配置（algo 可在容器内跑）**：为让 `make up` 的 algo 服务能真正启动，协同改了三处：
1. **dsn 环境覆盖**：`data.NewData` 优先读环境变量 `ALGO_DATA_DSN`（若设）> yaml dsn；Kratos env source 对嵌套 key（data.database.dsn）映射不可靠，故用显式 `os.Getenv` 覆盖。实测：`ALGO_DATA_DSN=<不可达库> ./bin/algo` 启动 fail-fast 并报出该 dsn，证明 env 生效。
2. **compose algo 服务**：补 `ports:["9002:9002"]`（原来无映射，宿主机够不着）、`working_dir:/` + `volumes: ../configs:/configs:ro`（distroless 运行时镜像**不含 configs**，不挂载则 `file.NewSource("configs/algo.yaml")` 读不到→启动失败）、`environment: ALGO_DATA_DSN=postgres://app:app@postgres:5432/gaokao`、`depends_on: postgres(condition: service_healthy)`；postgres 补 healthcheck（pg_isready）。
3. **go.Dockerfile 构建镜像升级**：`FROM golang:1.22` → `golang:1.25`——go.mod 声明 `go 1.25.0`，1.22 镜像 `go build` 会直接报 "go.mod requires go >= 1.25.0"，**三个 Go 服务的 docker 构建原本全是坏的**。

> compose 的 postgres 用 `../migrations:/docker-entrypoint-initdb.d`，里面是真实 schema+数据（00_schema.sql + 01~06，~78MB，按字母序初始化），score_rank_map 会被正确灌入。
>
> **未尽事项/注意**：①本机无 docker，上述 compose 改动只做了 YAML 结构确认，未 `docker compose up` 运行时验证；②initdb 只在 pgdata 卷为空时执行一次。
>
> **端口冲突已解决**：compose 的 postgres 宿主机映射由 `5432:5432` 改为 `5433:5432`，避免与本机 PG（5432）撞车（容器间仍走内部网络 postgres:5432，algo 的 ALGO_DATA_DSN 不受影响）。连带把从宿主机连 compose PG 的两个脚本默认 DSN 同步为 5433：`scripts/qa_check.py`、`scripts/ingest_charters.py`（原 `app:app@localhost:5432/gaokao`）。本机 PG 类 DSN（`francis@localhost:5432/ai_gaokao_counselor`，如 retrieve_test.py、dev-log #6）与 compose 无关，不变。

**实现期踩坑（3 个，均已修复并回写 task2 §4.1③ 文档）**：

1. **wire CLI 装不上**：`go install github.com/google/wire/cmd/wire@v0.6.0` 失败——依赖 golang.org/x/tools v0.17.0 在 Go 1.27 下编译报 `invalid array length -delta*delta`（tokeninternal）。**解法**：手写 `wire_gen.go`（plain Go，按 wire.Build 语义），头注说明原因；未来 wire CLI 可用可覆盖。
2. **config 目录合并 bug（关键）**：文档 §4.1③ 写 `file.NewSource("configs/")`，但 configs/ 下并存 gateway.yaml/llm-gateway.yaml，Kratos file source 会**合并全部 yaml**，llm-gateway.yaml 的 `server.grpc.addr=0.0.0.0:9001` 覆盖了 algo.yaml 的 9002，冒烟测试发现服务错误监听 9001。**解法**：改为 `file.NewSource("configs/algo.yaml")` 只读本服务配置。
3. **grpc.Timeout 类型 bug**：文档 §4.1③ newApp 写 `grpc.Timeout(bc.Server.GetGrpc().GetTimeout())` 传的是 string，但 grpc.Timeout 需 time.Duration。**解法**：`time.ParseDuration` 解析 + 兜底 30s。

> 坑 2、坑 3 属 task2 文档的 spec bug，代码修正后已同步回写 §4.1③（main.go / newApp 两处片段附 ⚠️ 注）。

---

## 10. svc-algo Phase 2：概率估计 + data 层 AdmissionRepo 落地

**目标**（task2 §5 里程碑 Phase 2）：实现 data 层 `AdmissionRepo` 三段批量 SQL + biz `prob.go` 概率纯函数（加权命中率法）+ `RecommendUsecase.EstimateProb` 编排（rank→取数→算概率→分档冲/稳/保）。选科硬约束（constraint）属 Phase 3，本期编排里留占位直通。

**落地内容**：

- **biz/prob.go**（纯函数，不碰 SQL）：`DeriveSchoolTier`（985>211>双一流>普通，导出供 data 行映射调用）、`calculateProbability`（加权命中率法：权重 decay^i、二元命中 用户百分位≤录取百分位、概率封顶 [0.01,0.99]）、`batchEstimateProb`（注入 historyMap/planMap，RankToPercentile 算百分位 + 计划修正 ×(1+change×PlanAdjust)、拷贝 history 防污染、无历史跳过）、`categorize`（阈值分档 + 回填 *Total）、`RecommendCandidates`（= batchEstimateProb + categorize 组合）。
- **data/admission.go**（pgx/v5 真实 SQL）：`LoadCandidates`（SQL1 组骨架聚合 MIN(min_rank)+JOIN university 属性、SQL2 组内专业明细 LEFT JOIN major，按 GroupKey 内存合并、DISTINCT subject_req 保序去重、COALESCE NULL 容错、ORDER BY 保序）、`BatchGroupHistory`（窗口 [报考年-W,报考年-1]、JOIN score_rank_map 子查询 MAX(cumulative_count) 取各年 total_count、ORDER BY year DESC）、`BatchGroupPlan`（enrollment_plan SUM(plan_count) 两年一次拉、组装 PlanChange）。
- **biz/recommend.go**：`EstimateProb` 编排落地（a. rank.Convert→用户百分位；b. LoadCandidates；c. 选科过滤占位 TODO(Phase 3)；d. BatchGroupHistory/Plan→RecommendCandidates）；batch 空则默认本科批；错误透传（ErrBelowControlLine/ErrMissingScoreRankMap 等）。

**验证**：
- 单测 `internal/algo/biz`：新增 `prob_test.go`（17 个：DeriveSchoolTier、calculateProbability 覆盖 0年/1年/2年/3年全命中·部分·0命中·边界、batchEstimateProb 百分位计算·计划修正翻转·PrevPlan=0 跳过·不污染入参·无历史跳过、categorize 阈值分档·边界·Total 回填、RecommendCandidates 组合）+ `recommend_test.go`（4 个：EstimateProb happy path 三档各 1、本科线报错透传、LoadCandidates 报错透传、无历史空分档）。合计 28 个测试全通过（含 Phase 1 的 rank 11 个）。
- **data 层集成测试**（`admission_integration_test.go`，`//go:build integration` + env `ALGO_TEST_DSN`，默认 `go test ./...` 排除、CI 无库自动 skip）：连本地库跑真实 SQL——LoadCandidates=2165 组（样本北京大学 1101(306) tier=985/北京市/5 专业/subjectReq=[首选物理，再选化学]/groupMinRank=161、保序校验通过）、BatchGroupHistory=6740 组（**验证无 2025 泄漏**、窗口 [2022,2024]、年降序、2024 total_count=313685）、BatchGroupPlan=8797 组（3248 组含两年计划）、端到端 RecommendCandidates 分档 冲39/稳18/保1051/淘汰1057。
- `go build ./...` exit=0、`go vet ./...` exit=0、`go vet -tags integration ./internal/algo/data/` exit=0。

**观察与遗留**：
- **候选数 2165 < 2024 组数 3543**：EXISTS enrollment_plan(2025) 把候选池限定在报考年实际招生的组（DY4，剔除停招/新增无 2024 历史的组），符合设计；比例偏低主因是部分 2024 组无 2025 计划或 major_group 编码跨年不完全对齐，属数据现实，非 bug。
- **分档两极化（保/淘汰多、冲/稳少）**：二元命中 + 3 年窗口下概率取离散值（全命中→0.99、全不命中→0.01），大量候选堆在两端，正是 §6.1「已知局限」；档内区分度由 Phase 4 utility 兜底，连续性修正为后续优化。
- **AuditBasis 管道 seam（Phase 5 处理）**：§4.2② 要求 EstimateProbReply 带 basis（用户位次/百分位、SampleYears），但 `EstimateProb` 按文档签名只返 `RecommendResult`（无用户 rank/percentile 字段）。Phase 5 wire RPC 时需解决 basis 来源（扩 RecommendResult 或调整返回），本期未动。
- **service 层 EstimateProb RPC 仍是 Phase 5 stub**（传空 UserQuery→透传 err）；Phase 2 只到 usecase 编排 + biz/data 实现，未做 pb DTO 映射。

---

## 11. svc-algo Phase 3：选科硬约束（constraint）+ 效用打分（utility）

**目标**（task2 §5 里程碑 Phase 3）：实现 `biz/constraint.go`（选科匹配纯函数）+ `RecommendUsecase.CheckConstraints` 方法（承接独立 RPC，委托同一 biz fn）+ `biz/utility.go`（四维效用打分），并把 Phase 2 在 `EstimateProb` step c 留的选科过滤占位接上。

**落地内容**：

- **biz/constraint.go**（纯函数，不碰 SQL/不 import pb）：
  - `SubjectReq{FirstChoice, ReselectMode(none/atLeastOne/all), Reselect[]}` + `parseSubjectReq`：按**全角逗号 `，`** 切首选/再选，去 `首选`/`再选` 前缀；再选四类分派——`不限`→none、单科→all(1门)、`X/Y(2选1)`→**半角括号**标记 + `/` 拆→atLeastOne、`X、Y(2科必选)`→**全角顿号 `、`** 拆→all；空串→不限/none；未知格式→error（保守剔除）；防御性把全角 `（）` 归一为半角。
  - `normalizeSubject`（政治→思想政治）、`matchSubjects`（首选 req=不限→过否则须==用户首选；再选 none→过/atLeastOne→交集非空/all→子集）、`matchCandidateSubjects`（**组级 match-any**：空列表当不限放过、任一条命中即过、任一条解析失败即保守剔除）。
  - `CheckConstraints(query, []Candidate) → (kept, rejected, err)`：按输入序 append（DH1/DH3 保序），rejected 填 `Rule=subject_req` + 人话 Reason，ProbEstimate 留零值（纯函数处于概率前）。Rule 常量 subject_req/single_score/physical/gender/batch 齐备（后四者 M1 TODO）。
- **biz/utility.go**（纯函数）：`CalculateUtilityBatch(profile, candidates, config)`——① `computeNormStats` 扫批算 min-max；② 四维得分 `tierScore`(985=1.0/211=0.8/双一流=0.6/普通=0.4) + `majorScore`(interest 子串命中 MajorName→1.0) + `locationScore`(偏好城市双向子串=1.0/同省湖南=0.8/其他=0.5) + `strengthScore`(0.4×minmax(NumMaster)+0.3×minmax(NumDoctor)+0.3×(1−RuankeRank/RuankeMax)，缺失软科按 0)；③ `deriveWeights`(基准各 0.25→PriorityDims 命中×2→PreferredTiers 非空则 tier×2 封顶不叠加→归一化 Σ=1)。维度遍历走固定 `utilityDims` slice（DH3 禁 map 迭代产序）。
- **biz/recommend.go**：① `EstimateProb` step c 占位 `kept := candidates` 换成真 `CheckConstraints(UserQuery{Subjects,Category}, candidates)`（过滤先于概率）；② 实现 `RecommendUsecase.CheckConstraints`——拆 []Candidate + 建 GroupKey→ProbEstimate 索引 → 委托纯函数 → 按 GroupKey 回填 kept/rejected 的 prob（契约形状完整）；③ 移除不再使用的 errs import。

**验证**：
- 单测 `internal/algo/biz` 新增 18 个顶层测试（含子测试共 47 PASS）：`constraint_test.go`（normalizeSubject、parseSubjectReq 覆盖真实 27 种 distinct 代表格式 + 全角括号防御、7 种未知格式必报错、matchSubjects 12 例含政治归一化、CheckConstraints match-any/空列表/无法解析剔除/保序/空批/全过）+ `utility_test.go`（tierScore/majorScore/locationScore/minmax/strengthScore/computeNormStats/deriveWeights 含文档例 0.33·0.33·0.17·0.17 与 tier 封顶×2、CalculateUtilityBatch 端到端 + 确定性 + 空批）+ `recommend_test.go` 补 2（EstimateProb 选科过滤生效、usecase CheckConstraints 回填 prob）。
- **集成测试补 `TestIntegration_CheckConstraints_RealSubjectReq`**（真实库）：核心断言——候选池 **7 种 distinct subject_req 零「无法解析」**（parser 100% 覆盖真实格式）；选科[物理,化学,生物]过滤 kept=2120/rejected=45/total=2165（拒绝的是要求思想政治/地理的组），拒绝均为 subject_req 规则。原 4 个集成测试无回归（LoadCandidates=2165、History=6740 无泄漏、Plan=8797、端到端 冲39/稳18/保1051/淘汰1057）。
- `go build ./...`、`go vet ./...`、`go vet -tags wireinject ./cmd/algo/`、`go vet -tags integration ./internal/algo/data/` 均 exit=0；默认 `go test ./...` 正确排除 integration tag。

**观察与遗留**：
- **写解析器前先 psql 实测真实 distinct subject_req**（27 种）才定下分隔符口径：括号是**半角** `(2选1)`/`(2科必选)`、首选/再选间是**全角逗号**、2科必选分隔是**全角顿号**、2选1 分隔是**半角斜杠**——凭记忆易写错全/半角，实测是硬前提。
- **utility 的「同省」硬编码 `userProvinceMVP="湖南"`**：UserProfile 无 province 字段，而系统仅覆盖湖南考生（项目边界），故候选院校 province==湖南 即「同省」。若未来扩省份，需把用户省份入参化。
- **CalculateUtilityBatch 本期只实现纯函数 + 单测**，其消费方 `OptimizeUsecase.OptimizeWishlist`（含 RiskAppetite→RatioFromRisk→配比排序 + checkSafeGuard 保底）属 **Phase 4**，尚未接线。
- **service 层 CheckConstraints/EstimateProb RPC 仍是 Phase 5 stub**（未做 pb DTO 映射）；Phase 3 只到 biz 纯函数 + usecase 编排，真实库选科过滤已由集成测试验证。

---

## 12. svc-algo Phase 4：志愿表优化（optimize）

**目标**（task2 §5 里程碑 Phase 4）：实现 `biz/optimize.go`（配比选择 + 全序排序 + 保底健全性检查纯函数）+ `OptimizeUsecase.OptimizeWishlist` 接线（utility 批量 + optimize + 保底）+ 集成测试。消费 Phase 3 的 `CalculateUtilityBatch`，产出成品志愿表。

**落地内容**：

- **biz/types.go**：新增 Category 常量单一真相源 `CategoryRush="冲"/CategoryStable="稳"/CategorySafe="保"`；重构 `prob.go` categorize 的 3 处字面量引用为常量（prob/optimize 共用同一枚举，防漂移）。
- **biz/optimize.go**（纯函数，不碰 SQL/不 import pb/不 import Kratos errors）：
  - `RatioFromRisk(risk)`：conservative{0.2,0.4,0.4}、aggressive{0.45,0.35,0.2}、default balanced{0.3,0.4,0.3}（空/未知防御走 balanced，不报错）。
  - `OptimizeWishlist(candidates []CandidateWithScore, params) (WishlistResult, error)`：Step1 `splitByCategory` → Step2/3 `selectByRatio`（按配比定向配额）→ Step4 全序排序赋 FinalRank → Step5 `checkSafeGuard`；SafeGuardOK=false 是告警非 error（DE3），err 恒 nil。
  - `selectByRatio`：**合并文档 Step2 贪心 + Step3 比例调整**为按配比定向配额——各档配额 `round(L×ratio)`（保档兜底 `L−rushT−stableT` 吸收取整误差），各档内按 `lessByUtilityThenKey` 取配额最优；某档不足配额时空位按全局 utility 降序从剩余候选补齐。确定性更强、直接命中目标配比。
  - `lessByUtilityThenKey` 全序比较器（Score 降序 → UniversityCode 升序 → MajorGroup 升序，DH2-A 无并列残留）；`sortByUtility`/`groupKeyOf`/`countByCategory` 便捷封装。
  - `buildSafePool`：筛 p≥threshold 且未入选的候选，按 Probability 降序（并列走比较器）供追加。
  - `checkSafeGuard(wishlist *[]CandidateWithScore, safePool, minSafe, threshold)`：**指针签名修正文档 spec bug**——文档初稿返回值切片，但内部 append 的保底无法传播回调用方（Go 值切片 append 不改调用方底层数组）；threshold 作参数传入（非硬编码 0.9），空表防除零。滑档率解析式 `Π(1−pᵢ)`，追加项 FinalRank 续在表尾。
  - **OptimizeUsecase.OptimizeWishlist 编排**：a. 边界检查 `Probability<=0`→`ErrCandidateWithoutProb`（合法概率 [0.01,0.99]，零值即未经 EstimateProb，DA4）；b. `CalculateUtilityBatch`；c. 组装 []CandidateWithScore；d. params=defaultProps，`profile.RiskAppetite` 非空→`RatioFromRisk` 覆盖 TargetRatio（Q20/DR3）；e. 调纯函数。兜底 L<=0→45、threshold<=0→0.9。

**验证**：
- 单测 `internal/algo/biz` 新增 13 个顶层测试（合计 **60 PASS**）：`optimize_test.go`（RatioFromRisk 5 例、lessByUtilityThenKey 全序、splitByCategory 忽略非三档、selectByRatio 配额分配 + 空位回填、buildSafePool 排除已选/p 降序、checkSafeGuard 指针传播/minSafe=0 恒真/池不足告警、OptimizeWishlist 端到端 6 项断言含 FinalRank 连续·utility 降序·追加保底、DefaultsFallback 零值 params 不 panic、usecase WithoutProb 报错 + HappyPath RiskAppetite 覆盖）。
- **集成测试补 `TestIntegration_EndToEnd_Optimize`**（真实库，串 data 三段 SQL + RecommendCandidates + OptimizeUsecase）：百分位 0.30 → 分档 冲39/稳18/保1051；合并三档为池（模拟 orchestrator 回传）→ 志愿表 **len=45（冲14/稳18/稳拿保底13）SafeGuardOK=true 滑档率=6.877e-37 期望满意度=0.3988**；FinalRank 连续、效用分已注入。原 5 个集成测试无回归。
- `go build ./...`、`go vet ./...`、`go vet -tags integration ./internal/algo/...` 均 exit=0；默认 `go test ./...` 正确排除 integration tag。

**观察与遗留**：
- **文档 §3.5 checkSafeGuard 签名 spec bug 已修正**：值切片 append 不传播是 Go 常见陷阱，改指针 `*[]CandidateWithScore` + threshold 参数化；同步更新 task2 §3.5 权威代码块 + 加「Phase 4 落地差异」注记（selectByRatio 合并 Step2/3、指针签名、RushCount/StableCount 由 OptimizeWishlist 统计）。
- **真实数据 L=45 恰好用满配额**（rushT=14/stableT=18/safeT=13，stable 只有 18 全取），无空位回填；13 个选中保档全 p≥0.9 → 稳拿保底 13≥minSafe 9，无需追加，滑档率 e-37 印证「有保底则滑档几乎不可能」的量化依据。
- **service 层 4 个 RPC 仍是 Phase 5 stub**：pb DTO 映射（fromPb/toPb，含 CandidateWithProb↔嵌套、CandidateWithScore 三层嵌入）+ AuditBasis basis 管道 + toKratosErr（ErrCandidateWithoutProb→CANDIDATE_WITHOUT_PROB/FailedPrecondition）+ wire 跑通 main.go 加载 conf 启动，均属 Phase 5。

---

## 13. svc-algo Phase 5：Service 集成（4 RPC 落地 + AuditBasis 管道 + 端到端）

**目标**（task2 §5 里程碑 Phase 5）：service/algo.go 实现余下 3 个 RPC（EstimateProb/CheckConstraints/OptimizeWishlist，RankConvert 已於 Phase 1）——只做 DTO 映射 + 调对应 usecase；dto.go 补齐 biz↔pb 全量映射；解决 AuditBasis basis 管道 seam；端到端测试。至此 algo 服务 4 RPC 全通，task2 实现阶段（Phase 1-5）收口。

**落地内容**：

- **AuditBasis basis 管道（解决 Phase 2 遗留 seam）**：`RecommendResult` 新增 `Basis AuditBasis` 字段（纯函数 RecommendCandidates 不知道用户 rank/percentile，故由 `RecommendUsecase.EstimateProb` 编排末尾回填）；新增 `sampleYears(year, w)` 生成概率窗口 [year-w, year-1]（与 BatchGroupHistory 同口径、不含报考年）；回填 UserRank/UserPercentile（取自 rank.Convert 结果，与 RankConvert RPC 口径一致）+ Method=weighted_hit + Province/Category。
- **service/dto.go**（biz↔pb 全量映射，集中一处，H5/Q35）：双向对 toPb/fromPb——MajorDetail/AdmissionRecord/Candidate（含 Majors/SubjectReq/HistoryData 子 slice）/ProbEstimate/CandidateWithProb（扁平↔嵌套）/UtilityScore（含 Breakdown/Weights map）/CandidateWithScore（三层嵌入↔三级嵌套）/UserProfile；输出专用 toPb——OptimizationReport/WishlistResult/AuditBasis/RejectedCandidate + 3 个 Reply 组装（EstimateProb/CheckConstraints/RankConvert）。**nil/空一致约定**：slice/map 仅非空时分配、空输入→nil 输出，保证 round-trip 保真。
- **service/algo.go**：EstimateProb（parseYear → biz.UserQuery{Province/Year/Category/Batch/Score/Subjects} + fromPbUserProfile → recommend.EstimateProb → toPbEstimateProbReply；req.per_category_limit MVP 不消费，用 conf 默认 0=全量）、CheckConstraints（fromPbCandidateWithProbSlice → recommend.CheckConstraints → toPbCheckConstraintsReply）、OptimizeWishlist（fromPb 候选/profile → optimize.OptimizeWishlist → toPbWishlistResult）。三 RPC 均经 toKratosErr 映射错误（error.go 已备全部 sentinel）。
- **wire/main.go 无需改动**：Phase 1 已手写 wire_gen.go 完整装配依赖图（data→biz 3 usecase→service→newApp），main.go 已 config.New(file.NewSource("configs/algo.yaml"))→Scan→wireApp(&bc)。

**验证**：
- 新增 **service 层 18 个单测全绿**：dto_test.go（12 round-trip 保真 fromPb(toPb(x))==x + nil slice 保持 + AuditBasis/OptimizationReport/WishlistResult/EstimateProbReply 输出字段断言）+ algo_test.go（4 个：CheckConstraints 选科过滤/prob 回填、OptimizeWishlist happy path/FinalRank 连续、CandidateWithoutProb→reason=CANDIDATE_WITHOUT_PROB、保底不足 SafeGuardOK=false 不返 error（DE3））。CheckConstraints/OptimizeWishlist usecase 是纯函数，无需 DB 即可测。
- **新增 service 层集成测试 `TestIntegration_Service_FourRPCs`**（真实库，用真 data Repo 组装等价 wire 依赖图，串 4 RPC）：① RankConvert 600→rank=15860/percentile=0.0495；② EstimateProb 分档 冲38/稳17/保1864，**basis.user_rank==RankConvert rank（口径一致）**、sample_years=3 年、method=weighted_hit；③ CheckConstraints kept=1919/rejected=0（候选已过滤、幂等）、kept prob 全回填；④ OptimizeWishlist len=45（冲14/稳17/稳拿保底14）SafeGuardOK=true 滑档率1.67e-38 满意度0.5212。
- **服务启动冒烟**：`go build -o bin/algo ./cmd/algo` → 启动日志 `config loaded: algo.yaml` + wireApp 建图 + DB Ping 通过（仅因端口 :9002 被上一会话遗留旧二进制 PID 61982 占用而 bind 失败，非代码问题）——证明 main.go+wire+conf 加载+DB 连通均正常。
- `go build ./...`、`go vet ./...`、`go vet -tags integration ./internal/algo/...`、`go vet -tags wireinject ./cmd/algo/` 均 exit=0；默认 `go test ./...` 全绿（biz+service），正确排除 integration；全量集成测试（biz+data+service）全过。

**观察与遗留**：
- **basis 管道方案**：选择扩 `RecommendResult.Basis`（而非让 service 重跑 rank）——符合「service 薄、编排在 usecase」与「basis.user_rank 与 RankConvert 同源口径」；纯函数 RecommendCandidates 保持无用户上下文（可单测），回填责任在 usecase。
- **per_category_limit MVP 不消费**：req 有此字段但 usecase 签名无槽（UserQuery 无该字段），MVP PerCategoryLimit=0 全量不截断（Q1），故 service 忽略入参、用 conf 默认，已注释说明。
- **旧进程占用 :9002**：PID 61982 是上一会话 Phase 1 起的旧二进制（不含新 RPC），未强杀（属用户进程）；验收时如需真实 TCP 端到端，可先释放 :9002 再起 bin/algo。
- **task2 实现阶段（Phase 1-5）已收口**：仅剩 Phase 6（文档与优化：更新 algorithms.md 代码对应关系、性能优化如缓存历史查询、代码审查）。

---

## 14. svc-algo 自测补充：bufconn 传输覆盖与两项未修复问题

> 后续状态：用户授权修复后，两项问题均已解决并回归通过，详见 §15。以下保留自测发现时的记录。

**范围**：按用户选择的方案 A，在现有 `service/algo_integration_test.go` 补内存 gRPC 测试。使用 Kratos gRPC server、生成的注册函数与客户端、真实 PostgreSQL；不修改生产代码、配置、数据库或既有进程，不占用 TCP 端口。

**新增测试**（均受 `integration` tag 与 `ALGO_TEST_DSN` 控制）：
- `TestIntegration_Bufconn_FourRPCs`：RankConvert → EstimateProb → CheckConstraints → OptimizeWishlist；每个 RPC 重复 3 次，用 `proto.Equal` 比较完整响应与直调结果，覆盖嵌套候选、专业列表、概率、AuditBasis、效用 map、FinalRank 与报告。比较发现差异时保留失败，但继续检查后续 RPC。
- `TestIntegration_Bufconn_ErrorMapping`：13 个错误子用例，按 task2 §4.4 断言 gRPC status，并检查 `ErrorInfo.reason` 与 `kerrors.Reason` 的客户端解析；包含空/非法年份、低于本科线、超最高分、报考年数据缺失、概率消息缺失/零/负值。
- `TestIntegration_Bufconn_Boundaries`：3 个子用例覆盖选科拒绝后的嵌套候选及历史数据回填、保底不足仍成功、空候选与缺省画像。
- `TestIntegration_Service_EstimateProb_Reproducible`：绕过 gRPC 重复直调，定位完整响应不一致是否来自传输层。
- 客户端请求有超时；清理按客户端 → server/listener → 数据库池执行，并等待 Serve goroutine 退出。

**发现 1：组内专业列表不确定（尚未修复）**
- `data/admission.go` 的 `LoadCandidates` 明细查询仅 `ORDER BY university_code, major_group, min_rank`，同组同位次没有唯一 tie-break，随后直接按 SQL 行序 append 到 `Candidate.Majors`。
- 实测中国石油大学（北京）`1120/（104）`：位次均为 11037 的专业 21（石油工程）与 24（人工智能）会交换顺序，位次 12788 的多个专业也会交换；直接重复调用同样复现，排除了 gRPC 序列化作为该差异的根因。
- 数量、分档及概率等统计值可以相同，但完整 protobuf 的 repeated 列表不同。前次只比对统计日志，不能据此宣称“字节级一致”或完全可复现。修复方向：明细查询增加稳定、唯一的并列排序键，保留完整响应比较作为回归断言。

**发现 2：两类错误的 gRPC status 不符合文档（尚未修复）**
- `SCORE_BELOW_CONTROL_LINE` 与 `CANDIDATE_WITHOUT_PROB` 的目标是 `FailedPrecondition`，当前 `toKratosErr` 返回 Kratos HTTP 400，经框架默认映射得到 `InvalidArgument`。
- 13 个错误用例的 `reason` 均能跨 gRPC 保留；其中 5 个子用例因上述 status 差异失败。未将期望值放宽为当前错误行为，也未擅自改动公共错误映射。

**验证结果**：
- `go build ./...`、`go vet ./...`、`go vet -tags integration ./internal/algo/...`、`go test ./... -count=1` 均通过；未设置 DSN 时新增 4 个测试均正确跳过。
- 真实库 `go test -race -tags integration ./internal/algo/... -count=1 -timeout=180s` **exit=1**：原有 biz/data 测试通过；新直调确定性测试、bufconn 四 RPC 完整比较、错误码契约测试失败，原因如上。无 race 报告，但不能将该命令称为通过。
- RankConvert、CheckConstraints、OptimizeWishlist 的三次完整响应比较通过；CheckConstraints 全量响应约 1.25 MB、OptimizeWishlist 响应约 43 KB；3 个边界子用例通过。
- 覆盖边界：bufconn 已经过 protobuf 编解码、gRPC 调度与 Kratos interceptor，但不等于真实 TCP、main/wire/YAML、外部 Python 客户端或部署验收。当前保留失败测试等待后续修复授权，不能宣称全部自测通过。

---

## 15. svc-algo 自测问题修复：并列专业确定排序与前置条件错误码

**范围**：用户明确要求继续修复 §14 的两项问题；只修改对应 data/service 逻辑、回归测试和权威文档，不修改概率算法、数据库、配置或现有服务进程。

**修复内容**：
- `data/admission.go`：保留原位次排序优先级，明细 SQL 增加 `major_code` 和主键 `id` 并列键。已对照 `migrations/00_schema.sql` 与真实库 `pg_constraint` 核实 `major_admission.id` 为主键、`major.id` 亦为主键，LEFT JOIN 不会扩增同一明细行。末尾主键保证专业代码为空或重复时仍有确定次序；候选/历史窗口和组投档线不变。
- `service/error.go`：仅 `SCORE_BELOW_CONTROL_LINE`、`CANDIDATE_WITHOUT_PROB` 使用 `preconditionError` 包装底层 Kratos HTTP 400 错误，`GRPCStatus()` 在复制的状态上改为 `FailedPrecondition`，完整复用 message 与 ErrorInfo；`Unwrap()` 保持 `errors.As/Is`、`kerrors.Code/Reason` 兼容。其余错误仍按框架默认转换，不改全局转换器。
- `data/admission_integration_test.go`：新增原始候选完整内容重复查询回归，明确断言 `1120/（104）` 位次 11037 的专业代码次序为 `[21 24]`，防止仅重复查询偶然同序造成假通过。
- `service/algo_test.go`：新增两项默认单测，覆盖全部已有 sentinel/未知错误/nil、包装输入与输出、gRPC 状态还原、HTTP code/reason、错误详情 metadata 保留及底层错误不被修改；确保无库环境也能防止错误码回归。
- 既有 bufconn 断言不放宽，补客户端解析 HTTP code 检查；task2 §3.2 SQL/组装说明、§4.4 包装实现说明同步。

**验证结果**：
- 2165 个原始候选完整内容重复查询 3 次一致；EstimateProb 的直接重复调用与 bufconn 完整响应比较全部通过。
- 四 RPC 每次各做 3 次完整 protobuf 比较，全通过；EstimateProb 响应约 1.25 MB，专业列表顺序、审计依据、效用 map 均包含在断言中。
- 13 个错误子用例全通过：两类前置条件错误现为 `FailedPrecondition`，HTTP 400 与稳定 reason 保持；非法年份/超分仍为 `InvalidArgument`，缺数据仍为 `Internal`。3 个边界子用例也通过。
- `go build ./...`、`go vet ./...`、`go vet -tags integration ./internal/algo/...`、`go vet -tags wireinject ./cmd/algo`、默认 `go test ./... -count=1` 均通过。
- 设置真实库 `ALGO_TEST_DSN` 后，`go test -race -tags integration ./internal/algo/... -count=3 -shuffle=on -timeout=180s`：biz/data/service 三包全通过，exit=0，无 race 报告。原失败测试已转绿，不靠删除、跳过或弱化断言。
- 验证范围仍为真实库 + 内存 gRPC；未重启或替换 :9002 现有进程，运行中的旧二进制不会自动获得本次源码修复。

---

## 16. Phase 6：性能基线、完整审查与边界修复

**执行范围**：一次独立 CodeReview 覆盖当前 algo 完整工作树、proto/config/装配及测试；不提交、不改现有数据库、不重启旧服务。首次审查未通过，以下问题先补最小失败用例，再修复并回归；没有派发第二次审查，也不把回归通过称为独立复审通过。

| 问题 | 根因与修复 | 回归证据 |
|---|---|---|
| 启动错误泄露 DSN | 移除连接串与底层错误链，使用固定安全文案 | TestNewData_RedactsDSN 两类失败连接 |
| RPC 暴露内部错误 | INTERNAL 使用固定文案 | TestRegression_InternalErrorRedaction |
| 保底突破 L | 无限 append 改为限额内补入/替换最低效用非稳拿项，再全表排序 | L=2 原返回4，现返回2；旧 L=5 返回6错误断言改为5 |
| 重复组、非法概率/分档 | 两入口唯一键校验，拒绝 NaN/Inf/越界及未知分档 | TestRegression_RejectInvalidCandidates |
| 科类/选科冲突 | 严格 3+1+2、别名归一化、禁止重复及双首选 | TestRegression_InvalidSubjects |
| 未知规则被提前匹配掩盖 | 严格语法、数量及科目检查，解析全组后匹配 | TestRegression_StrictSubjectGrammar |
| 历史审计缺失 | 回填实际历史原始百分位副本，计算副本单独计划修正 | TestRegression_HistoryAudit |
| 缺计划静默空结果 | 先检查报考年计划存在性 | TestIntegration_MissingEnrollmentPlan |
| 非法算法配置 | 默认合并后校验，连接前 fail-fast；usecase 保存配置错误 | TestRegression_InvalidConfig / TestNewData_InvalidConfigBeforeConnect |
| 真实库重复候选（回归额外发现） | 历史同组 ID/名称冲突导致按多身份分组、索引覆盖；保守排除身份冲突及空主键，不任取院校 | TestIntegration_LoadCandidates 固定 1412/（105）冲突样本＋全池唯一性 |

**数值与性能**：darwin/arm64、Apple M5 Pro、Go 1.25 系列；固定纯函数夹具 2165 个候选；数据库只读，报考2025年湖南物理类本科600分。`go test -tags integration ./internal/algo/... -run '^$' -bench . -benchmem -benchtime=1s -count=3`，未启用 race。

| benchmark | 优化前中位耗时 | 两项低风险优化后 | 审查修复后中位耗时 | 修复后 B/op；allocs/op |
|---|---:|---:|---:|---|
| CheckConstraints | 0.744 ms | 0.168 ms | 0.169 ms | 931788；2190 |
| RecommendCandidates | 0.356 ms | 0.333 ms | 0.383 ms | 2190859；4359 |
| OptimizeWishlist | 0.734 ms | 0.709 ms | 0.851 ms | 4179685；3212 |
| LoadCandidates | 47.5 ms | 46.3 ms | 127.3 ms | 10485855；212753 |
| BatchGroupHistory | 30.1 ms | 30.5 ms | 31.3 ms | 2719253；61156 |
| Service.EstimateProb | 89.2 ms | 87.3 ms | 162.1 ms | 21951453；378919 |

- 选科在请求内复用用户归一化与文本解析：原约1052KB/6506次分配，现约932KB/2190次，明确收益。
- 移除临时权重 slice 保留 `math.Pow` 与累加顺序；穷举命中组合的逐位浮点对照通过。原概率分配次数2194未因该项下降，不作不实归因。
- 审查修复后增加历史审计复制、候选校验与身份可靠性 SQL，完整推荐约162ms，较优化前更慢；接受正确性成本。这里是 benchmark 平均，后续另测100请求 p50/p95 与并发，不冒充尾延迟验收。
- 未引入 Redis、跨请求历史缓存或并行概率计算。

**本阶段验证**：默认 algo 全包测试、显式只读真实库 integration（含四 RPC 完整 protobuf 对照、原13类错误与新增缺计划/主键断言）及全仓 vet 通过。码表覆盖新增 sentinel、取消/超时、包装/还原。文档校正百分位、湖南范围、组粒度、历史下标衰减、有界保底与乘积风险局限。TODO 桩仍归属后续业务，不用删标签掩盖未完成能力。

**历史纠正**：§12 等早期记录中的“滑档几乎不可能”仅是独立模型乘积数值，不代表真实录取保证；旧 L=5 返回6的测试原本认可了错误行为，本节修正。系统测试与跨语言验收在下一阶段继续。

---

## 17. 系统测试收尾：跨语言、离线 ETL、性能与开发指南

**日期与范围**：2026-10-30 接续暂停任务，按既定顺序完成系统测试/修复及开发文档。保留已有改动，未提交、未部署、未重启现有服务、未更改现有数据库、未下载模型或调用付费模型。Phase 6 独立审查已在上一阶段执行，本阶段未再派发审查。

### 已实现的测试与运行入口

- Go 默认回归补充顶端分数区间/非法值、脏历史过滤、配置边界、不同池规模和风险偏好的志愿表不变量；不变量独立检查数量上限、唯一组键、全序、FinalRank 和报告，不能只与同一实现的直调结果互比。
- 真实库覆盖湖南物理类及历史类、窗口不含报考年、历史原始百分位、输入/返回对象不污染；保留原13类错误和完整 protobuf 对照。并发客户端使用同一 service，检查响应一致、取消/超时传播及资源清理。
- `cmd/algo/main.go` 增加兼容的 `-conf`；`cmd/algo/main_integration_test.go` 构建独立二进制，用临时 YAML 和回环空闲端口验证 main→config→wire→DB→四 RPC。夹具仅清理自己启动的进程，测试 DSN 通过环境传递，不写入临时配置或日志。
- Python `tests/smoke_algo.py` 使用生成客户端串四 RPC，断言用户位次、历史窗口、概率/历史条数、嵌套 DTO、效用 map 和错误详情；负载模式逐次比较完整响应。
- Makefile 提供 `test`、`check`、`test-integration`、`test-race`、`coverage`、`test-fuzz`、`bench`、`py-check`、`py-test`、`tcp-smoke`、`perf`、`compose-check`。真实库命令要求显式 ALGO_TEST_DSN；本轮传入 `default_transaction_read_only=on`。

### 生成链路与包装修复

- `make api-py` 增加 Kratos third_party include，解决 gateway 依赖的 `google/api/annotations.proto` 缺失；全 API 同次生成通过，不再只生成 algo 绕过错误。
- Python 统一 `api.<服务>.v1` 导入，Makefile 设置 orchestrator/pb 两个根路径；pyproject 包发现包含生成 API 命名空间，依赖版本与生成工具匹配。
- Go/Python Dockerfile 从 proto 生成，不依赖被忽略的宿主生成文件；Compose 引用重命名后的 Dockerfile，algo 挂载自己的配置。此次只静态核对，镜像未经构建。
- wheel 使用 `pip wheel --no-deps --no-build-isolation --no-index` 构建，安装到工作区 `bin/py-package-check` 隔离目录后导入 app/graph/rag 与 algo 生成客户端；输出路径确认来自该目录。未替换 Conda 环境中的项目安装。`pip check` 无依赖冲突。
- 生成物忽略规则涵盖 conf 和 Python stub；已有受 Git 跟踪的 egg-info 仍可能因包构建变动，未为了清理状态删除既有文件。

### Python 最小失败用例与修复

首次9项离线测试中5项失败，随后修复，扩为12项全通过：

| 失败现象 | 原因 | 修复/断言 |
|---|---|---|
| 无编号正文产生空列表 | 章节 flush 未考虑只有正文的情况 | 保留纯正文；无正文的单行编号标题也保留 |
| 多行章前言丢失、直接切分缺元数据 | 仅取第一行做概述；元数据只在文件包装层填 | 完整前言作为正文，split_charter 统一填元数据 |
| 阿拉伯编号无法生成路径 | 正则存在但未参与解析 | 接入顶层编号，条内子项保持正文；小数不当作编号 |
| 超长无标点句仍超过上限 | 分句后缺硬切兜底 | 优先段落/句边界，无边界按长度硬切；拼接还原原文 |
| 短条/尾条合并超过600字 | 只查合并下限，未查上限 | 合并前检查预算；尾条不合适则独立输出，路径覆盖最后一条 |
| 加概述后 chunk 再次超长 | 前缀未计入预算 | 重复章名/概述限长并预留预算；完整章名在路径，完整前言在正文 |
| 模型维度不匹配仍继续 | 只打印 warning | 构造时 ValueError；空输入不调用模型，返回 `(0, dim)` float32 |

`tests/test_offline.py` 同时遍历已提供的本地章程 JSON/TXT，检查切分非空、路径/元数据齐全及最终长度上限。本轮语料测试实际运行且未 skip。这里只证明长度/保留等不变量，不代表复杂嵌套编号或语义检索质量全部正确；未引入复杂层级识别，未重新切分入库。

### 实测验收结果

环境：darwin/arm64，`go version go1.27.2`、Python 3.11.17、libprotoc 36.2；使用既有 Conda 环境和只读本地 PostgreSQL。

| 验证项 | 结果 |
|---|---|
| 全 API/配置重新生成后全仓 build/vet/test | 通过；包含 integration/wireinject vet 与 wireinject 构建边界 |
| 真库 integration（含 TCP 装配） | 通过，未使用缺 DSN 的 skip 代替验收 |
| 三轮 `-race -shuffle=on` | biz/data/service 通过，无 race 报告 |
| biz 默认覆盖率 | 93.8% |
| service 默认覆盖率 | 81.0% |
| data 集成覆盖率 | 71.2%，未伪装成全仓平均值 |
| FuzzSubjectReq / FuzzParseRange | 各10秒通过，约343万/189万次执行 |
| Python 离线测试、语法/导入、pip check | 12项通过；不加载真实模型 |
| Python wheel 构建及隔离目录导入 | 通过 |
| Go/Python 真实 TCP 四 RPC | 通过，候选1915，YAML 志愿上限12生效 |
| Docker 原生 Compose 校验/镜像构建 | **未完成**：`docker: command not found`；常见安装目录也未找到 CLI |

TCP 性能使用临时配置 `max_wishlist_size=12`、`min_safe_count=3`（其余参数缺省），湖南物理类2025年本科600分、物理/化学/生物，EstimateProb 响应 **1,305,655 bytes**。没有并行跑 race/benchmark，没有清空数据库缓存。

| 场景 | 请求数 | p50 | p95 | max | 错误率 |
|---|---:|---:|---:|---:|---:|
| 单并发 | 100 | 179.239 ms | 182.451 ms | 186.579 ms | 0% |
| 8并发 | 100 | 223.153 ms | 237.271 ms | 258.174 ms | 0% |

新进程首次应用 EstimateProb 217.855 ms；它不代表数据库冷缓存。计时包含 Python 调用、回环 TCP 和响应解码；测的是固定本地数据，不推导生产容量承诺。单并发全部请求低于2秒。Phase 6 前后 B/op/allocs/op 与性能取舍继续以 §16 历史记录为准。

### 文档交付与未验证边界

- 新增 `docs/development-guide.md`：系统目标/当前状态、三层/四 RPC、数据/算法动机、配置与错误、调试/验收、问题复盘及后续顺序。
- 根 README 去除“全仓尚未编译”的旧状态，不再把 `make up` 作为本地首步；docs/README 更新可用导航；task2 §4.1/§4.3/§8 对齐单文件配置、Python 路径和实际验收状态。
- 修正 conf 注释中的“保底滑档率上限”：safe_guard_threshold 实为计入稳拿保底的最低录取概率。
- Docker 恢复后需执行 `make compose-check` 并另行镜像验证；当前没有容器环境验证结论。gateway、LLM gateway、完整 LangGraph/HITL、真实 RAG 检索/模型与录取准确率评测仍在后续范围。
- 本阶段完成的是 algo 系统验收和全仓已有实现检查，不是最终产品业务闭环。

---

## 18. Docker 环境恢复：配置通过，镜像仓库网络阻塞

**2026-10-30**：用户准备好 Docker 后继续验证。Docker Client/Server 29.8.2、Compose 5.5.0；context=orbstack，服务端 Linux/arm64（18 CPU、约16 GB内存）。`make compose-check` 原生配置校验通过，§17 的“缺 Docker CLI”阻塞已解除。

**构建准备**：新增根 `.dockerignore`，排除 Git/IDE、本地环境与密钥文件、语料/数据库初始化数据、配置、缓存和宿主 protobuf 生成物。运行时配置继续由挂载提供；镜像必须从源码与 proto 自行构建。

**实际执行**：
- 使用无效占位 OPENAI_API_KEY 执行 `docker compose -f deploy/docker-compose.yml --progress plain build algo gateway llm-gateway orchestrator`，仅构建，不启动 Compose。
- golang:1.25、python:3.11-slim、gcr.io/distroless/static-debian12 均在请求 registry 元数据时返回 `Bad Gateway`，构建尚未进入项目编译/依赖安装阶段。
- 单独重试 `docker pull python:3.11-slim` 同样失败；宿主通过 urllib 访问 Docker Hub 与 gcr.io 的 `/v2/` 入口均超时。
- `orb config get network_proxy` 为 auto；系统 HTTP/HTTPS/SOCKS 代理均未启用。Docker 显示 OrbStack 内部代理 `proxy.orb.internal:8305`，该地址本身不证明代理配置错误。当前证据支持网络/代理链路阻塞，尚不能定位到具体上游节点。
- 结束检查无容器、无镜像；未启动任何测试服务、未触碰数据库或旧进程，无测试容器需要清理。

**待继续**：用户恢复可访问官方镜像仓库的网络/代理后重试构建，再做隔离端口的 algo 容器四 RPC 及 Python 镜像导入/离线测试。没有自动修改系统代理、Docker 全局配置或切换第三方镜像来源。当前只可声明 Docker 引擎与 Compose 配置通过，不能声明镜像或容器业务验证完成。

---

## 19. DaoCloud 可选构建入口与 Linux/arm64 容器验收收口

**2026-10-30**：用户明确同意使用 DaoCloud。保持 §18 的失败历史，新增本节记录网络绕行、构建修复与最终证据。平台仍为 OrbStack Linux/arm64，未部署或启动整套 Compose。

### 构建入口与依赖问题

- 两个 Dockerfile 增加 `GO_IMAGE` / `GO_RUNTIME_IMAGE` / `PYTHON_IMAGE` 参数，默认保留官方镜像；Compose 传递可选值，Makefile 新增 `docker-build` 与 `IMAGE_MIRROR`。不改全局 registry-mirrors 或 OrbStack 代理。
- 首次通过 DaoCloud 下载基础镜像成功；Go 随后在 `proxy.golang.org/.../fsnotify/...mod` 超时。宿主已有 `GOPROXY=https://goproxy.cn,direct`，且探测返回200，因此增加仅构建期的 `BUILD_GOPROXY` 参数复用该源，保留默认校验和验证。
- Python 首次 pip 解析选中了带 CUDA 依赖的 torch；构建因 Go 失败被取消。改为先从官方 CPU 索引安装 torch，再安装项目依赖，避免当前无 GPU 环境下载 CUDA 运行库。索引由 `TORCH_INDEX_URL` 可选覆盖；运行实测为 `torch==2.15.0+cpu`、`torch.version.cuda is None`，没有下载模型权重。
- Go `SERVICE` 参数移到最终编译步骤，使依赖下载/代码生成层可被三个服务共用；宿主生成物仍由 `.dockerignore` 排除。

可重复命令（仅构建业务镜像）：

```bash
make docker-build IMAGE_MIRROR=m.daocloud.io/ BUILD_GOPROXY=https://goproxy.cn,direct
```

构建通过：algo、gateway、llm-gateway、orchestrator；Go 全 API/config proto 生成和编译、Python 全 API 生成和包安装均在镜像内完成。Docker 显示镜像大小约42.9 MB、34.4 MB、33.9 MB、2.08 GB（按以上服务顺序），不是生产资源容量结论。

本次收到的基础镜像 index digest（DaoCloud 为第三方镜像服务，未独立核验上游签名）：

| 镜像（均加 `m.daocloud.io/` 前缀） | SHA-256 |
|---|---|
| `docker.io/library/golang:1.25` | `699337d620559a59b4a2bb298ad59611e535d2ee755a34cf2d2a98f37578dc80` |
| `docker.io/library/python:3.11-slim` | `673719e071ecf27434fd2635bbd8d00f49f4fdd81f243143438a42ced8bedf60` |
| `gcr.io/distroless/static-debian12:latest` | `d75cdd72874d4790092fcb1b058493ecf6bb5bf2b2b897045b00ff01d91843f2` |

### 运行验证与失败用例修复

- **algo**：临时 YAML 设置 `max_wishlist_size=12`、`min_safe_count=3`、gRPC timeout=30s。只读配置挂载、只读容器根文件系统、随机回环端口（本次32768），经 `host.docker.internal:5432` 复用本机数据库；pgx DSN 包含 `default_transaction_read_only=on`。
- **四 RPC**：宿主 Conda 客户端和 Python 镜像分别执行已有 `smoke_algo.py`，均通过 RankConvert、EstimateProb、CheckConstraints、OptimizeWishlist，含历史审计、唯一性、嵌套 DTO 和三类错误详情断言。候选1915、响应1,305,655 bytes、志愿12项。Python 镜像通过宿主回环映射访问 algo，未验证 Compose 内部 DNS/服务接线。
- **Python 离线失败**：初次12项中仅语料用例报 `IndexError: 3`，原因是 `/app/tests` 下 `Path.parents[3]` 越界。改为安全的相对路径解析，并支持 `CHARTER_CORPUS_ROOT`；显式目录不存在时报错，不把配置错误当跳过。
- **Python 回归**：重新构建镜像；`--network none --read-only --tmpfs /tmp`、禁用字节码写入及 HuggingFace 联网、只读挂载本地章程后12项全通过。无语料挂载时11项通过、语料1项按约定跳过。宿主 `make py-test` 也12项全通过。
- **已安装包**：容器工作目录改为 `/tmp`、清空 `PYTHONPATH`，成功导入 `sentence_transformers`、`app.server`、`api.algo.v1.algo_pb2_grpc`；应用和生成客户端实际来自 site-packages。`pip check` 无依赖冲突。
- **只读连接探测**：Python/libpq 不接受 pgx 风格的 `default_transaction_read_only` URI 参数，初次在连接前报错；改用 `PGOPTIONS=-c default_transaction_read_only=on` 后 `SHOW transaction_read_only` 返回 `on`。未修改服务 DSN 或数据库配置。
- **其余服务**：gateway HTTP 对测试用不存在路径返回预期404；llm-gateway 和 orchestrator gRPC channel ready。只证明入口/导入/监听正常，业务 TODO 未实现，未调用真实模型。
- **最终本地回归**：`make check test` 通过全仓构建、默认/integration/wireinject vet、wireinject 标签测试与默认单测。此次不重复统计 §17 的 race/fuzz/性能数据。

### 资源与验收边界

测试四容器已停止并自动清理，临时 YAML 已删除；最终 `docker ps -a` 为空。保留构建镜像和缓存以便后续使用，没有执行全局清理。`RegistryMirrors=[]`、OrbStack `network_proxy=auto` 保持不变；未修改本机数据库/权限、旧进程或模型缓存，未提交、推送或部署。

本地 Docker 验收待办已收口，task2 §八及开发指南同步。仍未验证整套 Compose 的基础设施初始化/互联、Linux/amd64、GPU、真实模型或完整产品闭环；这些不计为本次通过范围。

---

## 20. data 层可维护性重构：独立 SQL、sqlc 类型与仓储拆分

**2026-10-30**：用户明确要求 SQL 从 Go 中分离，同时把 Go 逻辑拆细，并接受引入 sqlc。原实现是 pgx 参数化查询，不是直接拼接用户输入；核心问题是长 SQL、扫描变量和业务映射交织。GORM 也可以做结构体映射和复杂查询，本次选择 sqlc 是为了保留现有 SQL 口径并生成清晰的查询类型，不是因为 GORM 无法表达这些查询。

### 结构与维护入口

- `sqlc.yaml` 固定 PostgreSQL / pgx/v5，直接离线解析已有 `migrations/00_schema.sql`；没有复制 schema，也没有执行其中的初始化或导入语句。
- `queries/candidates.sql`、`history.sql`、`plans.sql`、`rank.sql` 共六条命名查询，包括招生计划存在性检查。命名参数和列别名使生成的参数、结果字段可直接阅读。
- `dbgen/` 由 sqlc v1.30.0 生成类型化查询及 `Params` / `Row`；扫描、关闭和迭代错误处理由生成代码承担。生成物随源码保存，不手工修改；不向 biz/service 暴露其类型。
- `admission.go` 从约289行收敛为80行，候选加载明确为检查计划、读取骨架、读取明细、组装四步。`admission_mapping.go` 专注字段映射和保序归组；`rank.go` 拆分完整表读取、本科线提取和区间映射。
- `Data` 持有绑定 pgxpool 的 `dbgen.Queries`；原 provider 签名、5秒 Ping、DSN 优先级、错误脱敏及 cleanup 不变。未修改领域算法、四 RPC 契约或 Wire 装配。
- `make sqlc-install` 将固定版本工具安装到 `bin/tools/sqlc-v1.30.0/`，不增加服务 go.mod 依赖；`make sqlc-generate` 生成，`make sqlc-check` 只校验差异。`make check` 已依赖 `sqlc-check`；普通 Go 构建及现有 Dockerfile 使用保存的生成物，不运行 sqlc。
- README 与开发指南补充阅读路线及“修改 SQL → 生成 → 映射 → 测试”流程。SQL/schema 改动和生成物应一起纳入版本管理；本轮没有执行 Git 提交。

### 改造前后真实库对照

先在本轮重构前的工作树上记录基线，再以相同只读连接运行重构后的 `TestIntegration_RepositorySnapshot`。完整候选/一分一段列表按原序序列化，历史/计划 map 按完整组键排序后序列化；记录 SHA-256。以下八个摘要逐一相等，不只是数量相同：

| 科类 | 内容 | 改造前 = 改造后 SHA-256 |
|---|---|---|
| 物理类 | 候选 | `dc88515ddaa14ed1ef4273a02f1bc2a5dd1e2bf07bb91b303f5ebe8a14f455d8` |
| 物理类 | 一分一段 | `078eeb0ef5b61d297185d8e7da599dbbeff2b84ca5e82e60981fcfff01aa74aa` |
| 物理类 | 历史 | `cc5dd372d6802146de2f0548f7ee36ee9f107ae3a71d5b5d25a5f05947fff4a3` |
| 物理类 | 计划 | `7ad40241aa751adda7e170470bde25bbcb4b2e707d427ce7074b6ca50b83ae2a` |
| 历史类 | 候选 | `4d013a4b75252109b9922e3a76397cf91a5b3ede79737b67bc9a9ac3be7bc5ae` |
| 历史类 | 一分一段 | `96fa1a6f3dc4b886ff6d9d427076cf6f032ad9f156c76355eb1e3b51cd337470` |
| 历史类 | 历史 | `5a75d1aa47e4c51f50e9a6073cb8099b0814dd48419813c977cff4e54b90501b` |
| 历史类 | 计划 | `0cd11bb9cddbceae328377d9c6e5f7667ee3d1c57abe5ad120789f1ac9b2cdef` |

数量也一致：物理类候选2161、一分一段592行、本科线405、历史组6740、计划组8797；历史类依次为1101、560、446、3205、4583。这里候选数是 data 原始池，选科/概率后的物理类 RPC 返回1915，并不矛盾。快照测试输出日志供人工前后对照，不把可变数据库摘要硬编码成永久断言，也不保证任意数据变更后的摘要仍相同。

### 本轮验证结果

- 新增六个离线映射测试：院校/专业全字段、零值、同名院校不同组、孤立明细排除、专业和选科保序、历史原值/年序、缺计划年份、同年参数优先级、完整批次及区间回退。
- 新增真实库空结果和取消测试：缺一分一段表、空历史/计划、`context.Canceled` 透传；全部通过。
- `make sqlc-generate sqlc-check`、`make check test` 通过：生成一致性、全仓 build/vet、integration/wireinject 构建边界及默认单测。
- `make test-integration` 通过，包含 data/service 真实查询、两科类、完整响应对照和独立 TCP 装配；`make test-race` 三轮 shuffle 全部通过，无 race 报告。
- `make tcp-smoke` 用独立新构建的子进程和临时12/3配置执行 Go/Python 四 RPC，均通过；候选1915，响应1,305,655 bytes。单次首次应用 EstimateProb 220.634ms，不视为冷数据库或尾延迟测量。
- `git diff --check` 通过。编辑器语言服务未就绪，无法提供 IDE 诊断；以实际构建、vet 和测试结果为验证依据。

### 性能取舍与边界

darwin/arm64、Apple M5 Pro、Go1.27.2，同一只读本地库；前后均使用 `-benchtime=5x -benchmem -count=1`，不与 race 并行。短样本仅用于发现明显变化，不作为稳定延迟或生产容量结论：

| benchmark | 改造前耗时 | 改造后耗时 | 前 → 后 B/op | 前 → 后 allocs/op |
|---|---:|---:|---:|---:|
| LoadCandidates | 124.493ms | 126.924ms | 10,489,326 → 18,261,056 | 212,763 → 114,239 |
| BatchGroupHistory | 21.931ms | 22.758ms | 2,668,729 → 4,351,446 | 61,163 → 38,263 |

sqlc 的 `:many` 先收集类型化结果 slice，再映射领域对象，相较原来的边扫描边组装增加中间存储和 slice 扩容分配。本次候选加载累计分配字节增加约7.77MB/次，历史查询增加约1.68MB/次；分配次数下降，不能因此宣称内存下降。B/op 是每次操作累计分配量，不是进程驻留或峰值内存。本次以可维护性为目标，未引入缓存、改变 SQL 数据口径或增加逐组查询；大规模并发下仍需另行测内存与容量。

没有修改数据库、权限或运行配置，没有重启用户正在自测的9002服务。TCP 测试仅清理自己启动的临时进程。未重新构建 Docker 镜像，§19 的容器通过记录不代表现有镜像已包含本次重构；未部署、提交或推送。

## 21. RAG 混合检索阶段一：pgvector ‖ pg_trgm 双路 + RRF 融合

**日期与范围**：2026-11-17。落地 PRD FR-08 的关键词一路（阶段一），不引入新基础设施；zhparser/tsvector 全文一路按分阶段策略继续推迟，待 golden set 评测后决策。本轮修改了本地活库（新建一个索引），未提交、未部署、未重建 Docker 镜像。

### 背景与决策

- 现状核查发现：`tsv` 列入库时用 `to_tsvector('simple', ...)`，simple 分词不切中文，全文一路对中文查询基本不可用；真实数据在 `policy_doc/policy_chunk`（2859 docs / 25546 chunks），而 migrations 只定义了同用途的 `charter_doc/charter_chunk`（活库中为空表），存在 schema 漂移。
- 决策：关键词一路用 pg_trgm（扩展已启用，GIN trgm 索引对中文子串 ILIKE 天然可用，无需自定义 PG 镜像）；融合用 RRF（k=60，Cormack 2009 推荐值）；cross-encoder 重排不在本阶段。
- 硬过滤口径沿用既有决策：meta JSONB 冗余 + `@>` 包含匹配（GIN(meta) 可用），禁止 JOIN 主表过滤。实测活库 `meta.year` 全部为 JSON number，过滤入参统一强转 int，避免 `@>` 类型不一致静默零命中。

### 变更

- `migrations/07_policy_chunk_trgm.sql`：按活库 pg_dump 原样补录 `policy_doc/policy_chunk` DDL 与存量索引（IF NOT EXISTS 幂等，收口 schema 漂移），新增 `idx_chunk_text_trgm`（GIN gin_trgm_ops）。已在本地活库执行，建索引约 2.7s。charter_* 与 policy_* 的表名最终收口未在本轮处理。
- `services/orchestrator/rag/retrieve.py`（新增）：`HybridRetriever`（vector_search / keyword_search / search 三模式 hybrid|vector|keyword）、`rrf_fuse`（融合分 = Σ 1/(k+rank)，同分按 id 升序 tie-break，确定性）、`extract_terms`（按空白/标点切词、去短 token、去重保序、截断 max_terms=4）。关键词路为「任一 term ILIKE 命中即召回，similarity() 排序」；SQL 全参数化，ILIKE 模式串转义 % _ \。
- `scripts/retrieve_test.py`：重写为 HybridRetriever 驱动，新增 `--mode/--pool`，结果同时展示两路各自名次与分数；keyword 模式不加载 embedding 模型。
- `tests/test_offline.py`：新增 HybridRetrievalTests 6 项（切词、RRF 打分/排序/tie-break 确定性、空输入、三模式路由、SQL 转义与硬过滤参数）。`make py-test` 18 项全绿（12 存量 + 6 新增）。

### 真实库实测（本地 ai_gaokao_counselor，25546 chunks）

- `--school 湘潭大学 -q 转专业`（hybrid）：Top-1 为湘潭大学章程「第六章 附则 > 第二十五条~第二十八条」转专业条款，向量路 rank1（相似度 0.9247）与关键词路 rank1 双路一致命中，RRF 0.03279。
- `-q 色弱考生有哪些专业不能报`（hybrid，无过滤）：Top-3 均为体检限报条款（石家庄学院/通化师范学院/延边大学），语义召回质量良好。
- 已知限制（如实记录）：① 整句无空格中文查询切不出多 term，关键词路退化为整句 ILIKE（难命中），此时实际由向量路主导——属预期行为，真正的中文分词留给 zhparser 阶段；② 不带 --school 过滤时，校名 token 会被大量含该校名/同名词的 chunk 淹没（如「湘潭大学 转专业」不带过滤时 Top-3 均为其他学校的转专业条款），说明**校名必须作为硬过滤条件由上游（画像/意图解析）提供**，检索层不做校名实体识别；③ similarity() 对长 chunk 打分整体偏低（0.0x 量级），只作路内排序信号，不作跨路可比分数——RRF 恰好只用名次不用分数，规避了该问题；④ 实测暴露源数据存在「东莞理工学院」与「东莞理工学院夏季高考」重复文档、部分 chunk 正文与 section_path 不符的 ETL 数据质量问题，属 FR-11 质检防线范畴，未在本轮处理。
- 单次 hybrid 查询 DB 侧毫秒级；整体耗时主要来自 embedding 模型加载（约 25s，常驻服务中不存在）。

### 未做与后续

- 未接入 LangGraph `retrieve` 节点（该节点仍为 stub，接入属 M2 编排接线）；未建 golden set，recall@k/MRR 量化对比（vector vs hybrid）留待评测体系落地；cross-encoder 重排、zhparser 分词、charter_*/policy_* 表名收口、ETL 质检防线均未动。

## 22. M2 ticket #2：RunLifecycle 持久受理与事件读侧

**日期与范围**：2026-11-18。落地 issue #2 的 SubmitRun / GetRun / WatchRun / CancelRun 最小闭环；ResumeRun、interrupt、官方 saver、worker 租约与算法预览仍由后续 tickets 承担。

### 实现要点

- 新增 additive 迁移：`release`、`run`、`run_command_receipt`、`run_event`，并为 `chat_session` 增加服务端 principal 归属；未修改任何历史迁移。
- `RunLifecycle` 成为唯一公开 seam，Profile/DB/transport 均不是调用方可见状态机。Submit 在单事务中绑定当前 PUBLISHED release、创建 session/run/首事件与 receipt；没有可用 release 时不创建 run。
- Receipt 以 `principal + operation + scope + idempotency_key` 为幂等域，同键同内容重放、同键异内容冲突；并发同键通过事务级 advisory lock 串行化。
- 事件按 run 内连续 `event_seq` 持久化；WatchRun 只轮询持久事件，不推进图，重连可重放，超过 7 天游标返回 `EVENT_CURSOR_EXPIRED`。
- 查询、订阅和取消都校验 principal；未完成 run 的结果引用为 null。QUEUED run 取消产生唯一 `CANCELLED / CANCELLED_BY_USER` 终态，后续取消不回改终态。
- gRPC 增加四个 RPC，仅做 metadata 身份提取、类型映射和稳定错误转换；`x-principal-id` 必须由服务间可信边界注入，不接受正文自称。

### 测试与运行入口

- 新增可写测试库集成夹具：`ORCHESTRATOR_TEST_DSN` 必须指向库名含 `test` 的隔离 PostgreSQL；测试会拒绝清理非测试库。生产运行 DSN 使用 `ORCHESTRATOR_DSN`，Compose 已接入。
- fresh schema 验证覆盖 `00_schema.sql → 08_release_and_run_foundation.sql`；行为测试覆盖提交/重放/冲突、无 release、session 所有权、查询/订阅隔离、事件补发/保留窗口、取消幂等与并发裁决。
- in-process aio gRPC 测试从真实传输调用 Submit/Get/Watch/Cancel，并验证缺失 principal 返回 `UNAUTHENTICATED / PRINCIPAL_REQUIRED`。
- `make build`、`make test`、`make check`、`make py-check`、`make py-test` 通过；有 DSN 的 Python 全量测试 34 项通过（1 个既有语料测试跳过）。

### 边界

- 当前只支持 `GENERATE_PLAN` 的结构化画像输入；自然语言解析、ResumeRun、interrupt、官方 LangGraph saver、租约接管、四算法 RPC 和 ALGORITHM_PREVIEW 不做提前实现。
- WatchRun 使用数据库短轮询；Redis/发布订阅仅可作为后续可重建通知投影，不是权威事件源。

## 23. M2 ticket #3：画像 revision、interrupt 与官方 saver 恢复

**日期与范围**：2026-11-18。落地 issue #3 的结构化画像 revision、画像确认/补问 interrupt、ResumeRun、官方 LangGraph PostgreSQL saver 与崩溃恢复；worker 租约接管留给 issue #4。

### 实现要点

- 新增 additive 迁移 `09_profile_run_lifecycle.sql`：`release_scope`、不可变 `profile_revision`、`profile_confirmation`、`run_interrupt`；历史迁移未修改。
- `ProfileRuntime` 作为 RunLifecycle 内部深模块，集中 CreateDraft / ApplyPatch / Validate / Confirm / ToAlgoInput；外部仍只使用 RunLifecycle 五操作。
- 结构化画像使用 `category`，主输入只接受分数；缺批次写 `DEFAULT=本科批` 并在确认卡展示。缺字段、非法选科、rank-only、release 范围不支持均回到 INTAKE_ASK，不进入算法。
- 接入官方 `langgraph-checkpoint-postgres==3.2.1`（并约束 `langgraph>=1.2.15,<2`）。画像确认图通过官方 AsyncPostgresSaver 编译；中断在 checkpoint 持久化后才写业务 interrupt。`plan_checkpoint` 保持审计表定位，不承担恢复。
- ResumeRun 校验 principal、WAITING_HITL、state_version、interrupt 状态/有效期、profile_version 和 release 时效；receipt、interrupt 消费、回答命令和转 QUEUED 原子提交。
- 回答图恢复与业务 revision 应用可重入：崩溃发生在 checkpoint 后或 Resume receipt 后，重试可识别既有 pending checkpoint/已存回答并只应用一次。
- gRPC additive 增加 ResumeRun、强类型回答 oneof、画像字段/finding/interrupt 事件；身份仍只来自可信 metadata。

### 测试

- PostgreSQL 行为测试覆盖：确认卡与默认 batch、精确 revision 确认、补问后新 revision、卡片编辑、DECLINED 保真、非法选科、rank-only、unsupported scope、旧 profile_version、release 撤销、双击重放、双标签并发、checkpoint 前后崩溃、事件连续补发。
- fresh schema 验证 `00 → 09`，并确认官方 saver 四张表由 `.setup()` 安装、`plan_checkpoint` 未被用作恢复。
- 全部 run lifecycle PostgreSQL 测试 33 项通过；`make py-check py-test`、`make build check test` 通过。

### 边界

- 当前自动画像推进是 RunLifecycle 内部同步处理；跨进程 worker 扫描、租约与 fencing epoch 由 issue #4 完成。
- 确认后 run 保持 QUEUED / `PROFILE_CONFIRMED`，四算法 RPC 与 ALGORITHM_PREVIEW 由后续 tickets 接线。
- 旧 `StartGraph/ResumeGraph` 仍保留为兼容入口，但新的画像确认状态只以 RunLifecycle 为权威。

## 24. M2 ticket #4：worker 租约、崩溃接管与超时竞态

**日期与范围**：2026-11-18。落地 issue #4 的 PostgreSQL 队列 worker、lease epoch fencing、主动执行预算、HITL 超时和执行中取消。唯一外部 seam 仍是 RunLifecycle 五操作。

### 实现要点

- 新增 additive 迁移 `10_run_leases.sql`：run 增加 lease owner / epoch / expiry、主动执行剩余额度与起算时间；补充 worker 扫描索引。历史迁移未修改。
- worker claim 原子执行 `QUEUED → RUNNING`，写 owner、递增 epoch、设置租约和连续 STATE_CHANGED 事件；租约未过期的其他 owner 不能抢占。
- 租约过期后新 worker 可递增 epoch 接管；状态、事件、interrupt 和终态提交均校验 owner + epoch + state_version，旧 worker 的迟到提交被 fencing 拒绝。
- 主动执行预算只在实际 RUNNING 段扣减；超过 600000ms 进入 `FAILED / EXECUTION_TIMEOUT`，不创建 interrupt。
- WAITING_HITL 不计入主动执行时间；超过 30 分钟关闭 OPEN interrupt，run 进入 `EXPIRED / HITL_TIMEOUT`。
- RUNNING 取消先写 cancel_requested_at，当前合法 worker 在安全检查点转 `CANCELLED / CANCELLED_BY_USER`；QUEUED / WAITING_HITL 取消立即形成稳定终态。
- 新增内部 `RunLifecycleWorker` 短轮询 adapter，从 PostgreSQL 权威 run 表领取 queued / expired lease / expired HITL；Redis 不参与执行权。
- RunView additive proto 字段暴露 active execution 剩余额度；gRPC 仍只做 transport mapping，不形成第二状态机。

### 测试

- 新增 PostgreSQL 行为测试覆盖：claim 事件顺序、租约过期接管、旧 epoch fencing、checkpoint 后崩溃恢复、主动执行超时、HITL 计时/超时、RUNNING 与 WAITING_HITL 取消、旧 interrupt 阻断、无订阅连接的后台 worker 领取。
- 原 ticket #2/#3 行为测试同步更新为 RUNNING 状态语义；fresh schema 验证推进到 `00 → 10`。
- Python 全量测试、RunLifecycle gRPC 与迁移测试均通过；`make py-check py-test`、`make build check test` 由合并阶段复验。

### 边界

- 本票只实现画像阶段 worker；确认画像后的四算法调用与非空 ALGORITHM_PREVIEW 由 issue #5 接线。
- 单机 PostgreSQL 短轮询满足 M2 可靠性底座；更大吞吐量和多区域调度不在本票范围。

## 25. M3 ticket #5：四 RPC 与非空 ALGORITHM_PREVIEW

**日期与范围**：2026-11-18。落地 issue #5：确认画像后按固定顺序调用 `RankConvert → EstimateProb → CheckConstraints → OptimizeWishlist`，把算法事实原样发布为不可变 `ALGORITHM_PREVIEW`。唯一行为 seam 仍是 `RunLifecycle`。

### 实现要点

- 四个 algo 请求 additive 增加 `release_id`；orchestrator 通过内部 `AlgoClient` 协议调用，生产 adapter 使用生成的 typed gRPC stub，不形成第二状态机。
- `ProfileRuntime.ToAlgoInput` 仍是唯一画像映射入口；确认画像字段映射到 Province/Year/Category/Batch/Score/Subjects 与偏好 DTO。
- 候选池只来自 `EstimateProb`；`CheckConstraints` 接收该服务端池，`OptimizeWishlist` 只接收 kept 候选。orchestrator 校验候选身份、概率和顺序保真，不改写算法事实。
- RankConvert 位次必须与 EstimateProb `AuditBasis.user_rank` 一致；缺少 AuditBasis、空候选或关键算法输出缺失均 fail closed，不发布 result version。
- 新增 `run_result` 迁移保存不可变预览、验证摘要和未核验条件；状态与 RESULT 事件在同事务发布。`GetRun` 与 `WatchRun` 返回同一 `result_version` 和相同 typed payload。
- 预览明确列出完整当年资格、条件事实、在线 RAG/证据、三类证据校验、LLM Gateway、Strategy、Critic、正式报告均未核验；带风险提示、免责声明和“不得作为正式志愿方案”标记。
- 删除旧 `StartGraph` 收尾的空 `DONE {}` 事件，避免无结果时伪造完成。
- svc-orchestrator 新增 `ALGO_ADDR`（本地默认 `localhost:9002`，Compose 使用 `algo:9002`）；包含 `algo` 启动依赖。

### 测试

- 新增 RunLifecycle PostgreSQL 行为测试覆盖：四 RPC 顺序与同 release、候选/概率/AuditBasis 保真、非空预览、GET/WATCH 一致、完整未核验条件、缺 AuditBasis 与空候选 fail closed。
- 新增 generated protobuf gRPC 边界测试，经 `GrpcAlgoClient` 从 RunLifecycle 发起完整流程；另以 `ORCHESTRATOR_ALGO_ADDR` 显式运行真实 svc-algo 进程的四 RPC 闭环。
- `make py-check py-test`、RunLifecycle PostgreSQL/迁移/gRPC 测试、`make build check test` 通过；默认离线测试不访问算法数据或模型。

### 边界

- 本票只发布 `ALGORITHM_PREVIEW`，不宣称完整资格、证据或正式报告已核验；ALGORITHM_PREVIEW 的完整失败矩阵、复现性回归与发布竞态由 issue #6 加固。
- 当前 release_id 已贯穿四个请求；在线算法数据表按 release_id 实际选行仍依赖后续发布数据迁移，不在本票伪造完成。

## 26. M3 ticket #6：预览失败门禁与可复现性

**日期与范围**：2026-11-18。落地 issue #6：加固 `ALGORITHM_PREVIEW` 的完整 fail-closed 门禁、旧 epoch/取消/执行超时发布竞态和不可变 result 重放。唯一行为 seam 仍是 `RunLifecycle` 五操作。

### 实现要点

- 算法输出白名单收紧：概率必须处于 `[0.01, 0.99]`；EstimateProb 返回空候选时不再调用下游 RPC。
- CheckConstraints 的 kept / rejected 必须来自 EstimateProb 原候选池，身份不可注入、不可重复、概率不可改写；全部候选被过滤时不得以空 kept 池调用 OptimizeWishlist。
- OptimizeWishlist 结果必须来自 kept 候选，禁止新候选、重复候选和概率改写；空优化结果进入 `NO_RELIABLE_DATA`。
- 算法调用后、结果发布前复核当前 owner/epoch、state_version、确认画像、cancel 请求、release 有效性和剩余主动执行时间。执行时间耗尽的迟到结果进入 `FAILED / EXECUTION_TIMEOUT`。
- 发布事务遇到 cancel 请求时由当前 worker 在安全点提交 `CANCELLED`；旧 lease epoch 的迟到结果不能覆盖新 worker 已发布的 result。
- `AlgorithmPreview` additive 增加 `internal_controlled_only`，与 `formal_recommendation=false`、风险提示和免责声明一起构成机器可读的受控预览边界。

### 测试

- 新增 14 项 RunLifecycle PostgreSQL 行为测试：非法概率、候选/拒绝候选注入、空候选池、全过滤、空优化、缺审计依据、unsupported scope、未确认画像、release 中途撤销、执行超时、取消/发布竞争、旧 epoch 迟到发布、同输入可复现、GET/重连 Watch 单版本重放和受控文案门禁。
- gRPC 契约测试同步断言 `internal_controlled_only=true` 且 `formal_recommendation=false`。
- 全部行为断言只通过 RunLifecycle 五操作观察；数据库仅用于隔离夹具和不可变 release 准备。

### 边界

- 本票只加固 `ALGORITHM_PREVIEW` 的内部安全出口，不实现在线 RAG、完整资格规则、真实 LLM Gateway、Strategy、Critic 或正式报告。
- 预览成功仍不代表当年资格已完整核验，也不得转换为正式志愿方案。

## 27. Code review 收口：RunLifecycle 端到端与 release 数据维度

**日期与范围**：2026-11-18。按 `code-review` 的 Standards / Spec 双轴结果收口 integration branch，不扩大 M2/M3 业务范围。

### 实现要点

- gateway 三个旧入口不再 TODO：CreatePlan 的 JSON 结构化输入转 `SubmitRun`，ConfirmProfile / BackfillProfile 转同一 `ResumeRun`；可信 principal 通过 gRPC metadata 向下游传播。StartGraph / ResumeGraph 也改为 RunLifecycle 兼容适配器。
- LangGraph 不再只是单节点 pause：图包含画像准备、人工 interrupt、内部执行闸门和算法预览节点；确认写回业务库后由 worker 恢复内部闸门，算法结果写入 checkpoint 后再发布。
- 新增 `13_release_data_dimensions.sql`：`release_component`、公开数据表 `release_id`、backfill/默认 legacy release、含 release 的唯一键和查询索引。RankConvert / EstimateProb / CheckConstraints / OptimizeWishlist 均携带 release，结构化数据 SQL 实际按 release 过滤。
- RunEvent additive 增加 typed Progress / Error payload；RunView additive 增加 expires_at；interrupt/profile field wire mapping 集中复用。
- 已确认画像不再允许原 run 生成新 draft；画像修改必须创建新 run。
- worker 通过 RunLifecycle 的公开内部调度方法工作；事件插入集中到 event adapter；构造器测试专用崩溃回调移除，改为测试子类覆写受保护钩子。

### 测试

- gateway：结构化 CreatePlan → SubmitRun、ConfirmProfile → ResumeRun、principal metadata 传播均通过。
- RunLifecycle PostgreSQL/gRPC：62 项通过，1 项真实算法测试按未设置地址跳过；fresh schema 推进到 `00 → 13`。
- 真实 svc-algo：以新二进制和只读数据副本运行，四 RPC + ALGORITHM_PREVIEW 闭环通过。
- `make py-check py-test`、`make build check test`、`git diff --check` 通过。

### 边界

- release 数据维度迁移不重新发布业务数据；legacy 数据回填到 `rel_legacy_v1`，后续 ETL 仍需为正式 release 显式写版本。
- 在线 RAG、完整资格规则、真实 LLM Gateway、Strategy/Critic 与正式报告继续保持 out of scope。

## 28. M2/M3 前端验收前置：fresh release scope 与 gateway HTTP/SSE

**日期与范围**：2026-11-19。补齐 M2/M3 自动化验收后续发现的两个前端验收缺口，不扩大到 M4 业务规则。

### 实现要点

- 新增 `14_default_release_scope.sql`，为 `rel_legacy_v1` 幂等补齐湖南、2025、本科批的物理类与历史类 scope；fresh schema 不再依赖测试夹具手工写 scope。
- gateway additive 返回 `runId`，并新增 HTTP `GetRun`、`WatchRun`、`CancelRun` 契约。客户端仍只通过 gateway 访问，状态与终态继续由 orchestrator.RunLifecycle 裁决。
- `/v1/runs/{run_id}/events` 输出标准 SSE：`id` 使用业务 `event_seq`，`event` 为 typed RunEvent 类型，`data` 为完整 typed payload；支持 `after_event_seq` 与 `Last-Event-ID`，query 游标优先。
- SSE 15 秒 heartbeat 仅写注释帧，不占 `event_seq`，并设置禁用代理缓冲的响应头。
- gateway gRPC client 通过内部 adapter 适配 server-streaming；HTTP 测试使用真实 Kratos handler 和可注入下游 client。

### 测试

- fresh schema 从 `00 → 14` 无需测试手工 seed 即可让物理类、历史类画像进入 `PROFILE_CONFIRM`；重复执行默认 scope 迁移保持幂等。
- HTTP：CreatePlan/Confirm/Backfill 返回 `runId`；GetRun 返回当前 interrupt、preview 与 `expiresAt`；CancelRun 透传 `RUN_ALREADY_TERMINAL`。
- SSE：Last-Event-ID 补发、query 游标优先、heartbeat 不占用事件序号；缺失可信 principal 返回未认证。
- 双击确认通过 HTTP 保持同一幂等键，唯一推进仍由 RunLifecycle/interrupt receipt 保证。

### 边界

- 开发期仍可用 `x-principal-id` 注入 principal；公网部署必须替换为真实认证上下文。
- 本次只提供前端验收所需传输契约，不实现 M4 检索、资格规则或正式报告。

## 29. M2/M3 Vue 3 验收控制台

**日期与范围**：2026-11-19。新增独立 Vue 3 + Vite + TypeScript 前端验收页，只通过 gateway 验证 M2/M3。

### 实现要点

- 结构化画像表单提交到 `POST /v1/plans`，页面展示 runId、sessionId、state、stage、releaseId、stateVersion 和 expiresAt。
- `GET /v1/runs/{run_id}` 用于 URL/sessionStorage 刷新恢复；SSE 使用 fetch ReadableStream，以便携带 `x-principal-id`，不使用无法设置自定义 header 的原生 EventSource。
- SSE 事件按 eventSeq 去重排序，忽略 heartbeat，断线后使用最后消费游标作为 `after_event_seq` 重连。
- PROFILE_CONFIRM interrupt 展示画像字段、DEFAULT 批次、profileVersion、interruptId 和 stateVersion；确认按钮在请求期间禁用并复用同一幂等键。
- SUCCEEDED 展示 ALGORITHM_PREVIEW、位次/百分位、冲稳保候选、AuditBasis、剔除原因、优化志愿表、验证摘要、未核验条件、风险提示和免责声明。
- 前端显式标注“仅用于内部受控验证”和“不得作为正式志愿方案”；失败、过期、取消和未认证均展示稳定状态。
- 候选组各展示前 12 条并标出完整总数，避免数千候选导致页面不可用；完整数据仍保存在不可变 result version。

### 测试

- Vitest：10 项页面行为，覆盖提交、URL 恢复、画像确认、双击保护、SSE 去重/heartbeat/重连、preview、失败、取消终态和未认证。
- Playwright：真实 Vue 页面路由验收画像确认到 preview。
- 使用真实 gateway + orchestrator + svc-algo + 临时 PostgreSQL 数据副本完成浏览器 happy path：`WAITING_HITL → SUCCEEDED`，浏览器无 console/page error。
- `npm test`、`npm run build`、`npm run e2e` 通过。

### 边界

- 开发 principal 来自 `VITE_PRINCIPAL_ID`；公网部署必须换为真实认证。
- 前端不直连 orchestrator，不实现 M4 检索、资格规则或正式报告。

## 30. M4a-01：release-bound 文档适用域与证据主路径

**日期与范围**：2026-11-19。实现 issue #8 的最小可用 `RetrievePolicy` 路径；只接入 `mode=keyword`，不提前实现向量、RRF 或 rerank。

### 实现要点

- 新增 `migrations/15_policy_retrieval_release_scope.sql`：为 `policy_doc` 补齐类型、发布者、学校、抓取时间、内容 hash 与原件存储 URI；为 `policy_chunk` 补齐文本 hash、字符定位与 embedding/index 身份；新增 `policy_chunk_set`、`release_policy_doc` 和 `policy_applicability`。
- `release_policy_doc` 以 `(release_id, doc_id)` 唯一映射一个 `chunk_set_version`，chunk 通过复合外键绑定所属文档与 chunk set，避免仅按共享 `doc_id` 混入旧分块。
- 新增唯一在线检索 seam `rag/policy_retrieval.py::RetrievePolicy`：先校验 release 为 `PUBLISHED` 且未撤销，再拒绝缺失/越界 scope；适用域与学校、文档类型、有效区间均在相似度计算前执行参数化 SQL 硬过滤。
- 关键词路最多保留 4 个去重且长度不少于 2 的 term，并转义 `%`、`_`、反斜杠；无候选或候选无法形成完整证据时返回 `NO_EVIDENCE`。
- 每个结果投影为不可变 `DocumentEvidence` 与 `EvidenceRef`，绑定同一 `release_id`，校验正文 hash、完整来源字段、适用域和字符 locator；`evidence_id` 由文档类型、release、chunk、excerpt hash 与 locator 规范化生成。
- `vector` / `hybrid` 在本片显式返回 `MODE_UNSUPPORTED`，不以空列表伪装成功；图节点/API 接线、向量路、RRF、rerank 和 golden set 不在本票范围。

### 测试

- 离线行为测试覆盖 release 状态、scope、`NO_EVIDENCE`、模式未支持、稳定证据映射。
- PostgreSQL 隔离 schema 测试从 `00` 应用到 `15`，覆盖 chunk set 隔离、省份/科类/批次/学校/年份/文档类型硬过滤、全国与长期有效政策、通配符转义、不可证据化候选和重复查询确定性。
- `ORCHESTRATOR_TEST_DSN` 缺省时数据库用例跳过；显式连接测试库时使用随机 schema，不读取生产数据或下载模型。

## 31. M4a-02：pgvector 向量召回与 release 索引身份

**日期与范围**：2026-11-19。实现 issue #9 的 `mode=vector` 主路径；RRF、hybrid 与 rerank 不在本票范围。

### 实现要点

- 新增 `migrations/16_policy_vector_release_identity.sql` 的 `release_policy_index`，声明每个 release 唯一的 embedding 模型、向量维度与 index version。
- `RetrievePolicy` 在生成查询向量前校验 release 身份、`policy_chunk_set` 与 chunk 身份；任一不一致返回 `INDEX_MODEL_MISMATCH`，不混读不同模型、维度或索引版本。
- 向量候选与关键词候选复用同一 release、文档类型、学校、地区、科类、批次、有效区间与证据完整性硬过滤；过滤完成后才按 `embedding <=> query` 余弦距离排序，并以 `chunk_id` 升序稳定 tie-break、截取候选池。
- `mode=vector` 的成功零候选返回 `NO_EVIDENCE`；未配置模型、embedding 失败或向量查询技术失败返回 `VECTOR_UNAVAILABLE`，不伪装为无资料。
- 向量结果沿用关键词路的 `DocumentEvidence`/`EvidenceRef` 投影，`release_id`、excerpt hash、locator 与 `evidence_id` 语义保持不变。

### 测试

- `PolicyRetrievalTests` 使用确定性 embedder 和数据库边界替身覆盖 vector 证据映射、模型/维度与 chunk set 身份不匹配、模型不可用、查询失败及错误分类。
- `PolicyRetrievalPostgresTests` 在随机隔离 schema 中从 `00` 应用至 `16`，覆盖高相似度跨省/跨科类/错误学校/过期干扰排除、chunk 身份混读、稳定顺序、重复证据 ID、成功零候选和不可证据化候选池过滤。
- 上述数据库用例显式设置 `ORCHESTRATOR_TEST_DSN`；常规 `make py-test` 不下载真实模型、不访问生产数据。

## 32. M4a-02 修复：hard-filtered ANN 与 HNSW 计划验收

**日期与范围**：2026-11-19。修复 issue #9 中 pgvector 查询虽语义硬过滤但无法使用 `idx_chunk_embedding` 的验收缺口。

### 问题

- 原 vector SQL 先用 `MATERIALIZED scope_candidates` 物化全部适用域候选，再在外层计算 `embedding <=> query`。真实 `EXPLAIN` 因此只看到连接与 `Sort`，HNSW 索引没有参与 `ORDER BY`。
- 直接改成 `ORDER BY c.embedding <=> query, c.id` 会使 HNSW 失效，因为 HNSW 只能提供单键距离顺序；复合稳定排序迫使规划器全量精确排序。
- HNSW 的过滤总发生在图遍历之后。若仅去掉 `MATERIALIZED` 并保持默认 `hnsw.iterative_scan=off`，大量高相似度域外 chunk 可耗尽 `ef_search`，造成范围内结果被错误漏掉。

### 实现

- 向量查询改为有界 `ann_candidates` CTE：release、chunk set、文档、学校、文档类型、适用域和证据完整性过滤仍全部位于 CTE 内；CTE 以 `policy_chunk.embedding <=> query_embedding` 直接排序并最多截取 `pool_k`。
- 外层只对被索引返回的小候选池按 `(vector_distance, chunk_id)` 稳定排序，兼顾 HNSW 单键访问与确定性输出。
- 向量查询在事务内设置 `hnsw.iterative_scan=strict_order`、`hnsw.ef_search=max(40,pool_k)` 和 `hnsw.max_scan_tuples=2147483647`。严格迭代扫描在过滤后继续访问索引，最大扫描上限避免默认 20000 条约束静默截断结果。
- 显式访问 vector 运算符以加载扩展，再设置 pgvector 自定义 GUC；向量查询仍全参数化，并对执行失败继续返回 `VECTOR_UNAVAILABLE`。

### 测试与计划证据

- 新增真实 PostgreSQL `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` 回归：通过 `RetrievePolicy` 捕获实际 vector SQL 与参数，在关闭小表精确路径成本干扰后断言 `policy_chunk` 使用 `idx_chunk_embedding`，索引节点 `Order By` 为 `embedding <=> query`。
- 新增 60 个域外距离 0 干扰与 1 个范围内候选竞争 `pool_k=1` 的回归，证明严格迭代过滤不会把结果错误判为缺失。
- 既有适用域、release/chunk 身份、不可证据化候选、零候选和稳定 tie-break 回归继续通过。

### 风险与边界

- `strict_order` 且无 20000 条上限意味着：当适用域命中极少或为零时，为证明“确实没有范围内候选”，pgvector 可能遍历大部分甚至全部 HNSW 图，延迟随不匹配比例上升。该成本用于换取硬过滤下不漏候选；后续应结合真实数据基准调整 API 超时、索引隔离或按适用域分区。
- 该查询依赖 pgvector `0.8+` 的迭代扫描 GUC；低版本会明确失败为 `VECTOR_UNAVAILABLE`，不得静默退回可能漏候选的旧计划。
- HNSW 仍是近似索引；本次保证的是同一硬过滤候选域内的索引化距离访问与稳定小池输出，不等同于对全库向量做精确穷举。

## 33. M4a-03：pgvector + pg_trgm 双路 RRF 与降级

**日期与范围**：2026-11-19。实现 issue #10 的在线 `mode=hybrid`；rerank 与 golden set 不在本票范围。

### 实现

- 把 release、文档类型、学校、地区、科类、批次、有效区间与证据完整性条件收口为两路共用的参数化 SQL 基底；向量路只追加 embedding/index 身份与距离排序，关键词路只追加 `pg_trgm` 匹配与相似度排序。
- `hybrid` 在同一 scope 内执行 vector 和 keyword 召回，使用 1-based `RRF(k=60)`：缺一路时不获得该路分数，双路命中按 `Σ 1/(60+rank)` 累加，最终按融合分降序、`chunk_id` 升序稳定输出。
- 单路 `VECTOR_UNAVAILABLE` / `KEYWORD_UNAVAILABLE` 时保留另一路结果并返回稳定 `degraded_reason`；两路均为技术故障返回 `RETRIEVAL_UNAVAILABLE`。一路故障且另一路成功零命中时仍返回 `NO_EVIDENCE`。
- 证据投影失败改为 `EVIDENCE_PROJECTION_FAILED`，不再跳过候选或伪装为 `NO_EVIDENCE`；release 查询时数据库不可达同样返回明确技术失败。
- 诊断仅暴露两路候选数、`rerank_enabled=false` 和稳定降级原因，不暴露 SQL 行、距离或私有结构。

### 测试

- `PolicyRetrievalTests` 新增 hybrid 的 RRF 可复算顺序、单路/双路命中、同分 `chunk_id` tie-break、单路零命中、双向降级、双路故障、一路故障加另一路零命中、数据库故障和投影失败行为测试。
- `PolicyRetrievalPostgresTests` 新增真实库 hybrid 测试：两路共同排除跨省、跨科类、跨批次、错误学校、错误文档类型、过期和无适用域文档；重复查询得到相同证据 ID 顺序；向量模型缺失时同范围降级为关键词结果。
- `test_policy_retrieval.py` 全量 38 项通过（20 项离线、18 项本机隔离 PostgreSQL），既有 release/chunk set 隔离、关键词转义、向量索引身份、HNSW 计划和 filtered ANN 回归保持通过。

### 风险与边界

- 单路故障降级可能降低召回，但不会删除或放宽任一适用域过滤；调用方必须读取 `degraded_reason`，不能把降级结果视作双路完整召回。
- `strict_order` 高不匹配比场景的 HNSW 延迟风险继续存在；真实容量和超时仍需 golden set/压测量化。
- 本文未接入 rerank、图节点/API 与 golden set；这些仍由后续 ticket 完成。

## 34. M4a-04：融合池内可选 rerank 与故障回退

**日期与范围**：2026-11-19。实现 issue #11 的 hybrid 可选 rerank；不训练、下载真实模型，也不改变适用域、release 或证据身份。

### 实现

- 新增 `migrations/17_policy_rerank_release_identity.sql`：`release_policy_rerank` 按 release 保存显式启用状态、模型、配置版本和超时；启用时三项配置必须完整，无配置行表示关闭。
- `RetrievePolicy` 在释放状态校验后读取 release 级 rerank 配置；只有 `mode=hybrid` 且显式启用时才调用注入的 rerank 适配器。适配器身份与 release 的模型/配置版本不一致时返回 `RERANK_UNAVAILABLE`，不发起模型调用。
- rerank 输入严格限制为 RRF 融合池的不可变 `(chunk_id, text)` 序列，不传 SQL 行、距离、适用域外候选或跨 release 数据。输出必须是该池的完整排列；重复、未知、遗漏或非字符串 ID 均拒绝整次重排。
- 超时通过 release 配置的截止时间执行，返回 `RERANK_TIMEOUT`；异常返回 `RERANK_FAILED`；无效输出返回 `RERANK_INVALID_OUTPUT`。三类故障都完整保留原 RRF 顺序和 tie-break。
- 成功重排只改变候选行顺序；证据投影仍从原始 RRF 行读取，因此 excerpt、hash、locator、适用域与 `EvidenceRef.evidence_id` 均不变。诊断新增 rerank 模型、配置版本和超时，不暴露适配器私有输出。
- 空 RRF 池返回 `NO_EVIDENCE` 且不调用 rerank；hard filtering、单路降级和 `EVIDENCE_PROJECTION_FAILED` 语义保持前序实现不变。

### 测试

- `PolicyRetrievalTests` 从 `RetrievePolicy` 覆盖：默认/显式关闭不调用模型；正常重排与诊断身份；超时；适配器异常或缺失调用能力；重复、未知、遗漏输出；模型身份不匹配；空候选。
- 重排成功与故障用例比对 RRF 基线证据 ID、excerpt、hash、locator 和适用域，确认只改变顺序，不改变证据身份。
- `PolicyRetrievalPostgresTests` 在 `00`~`17` 的随机隔离 schema 中验证 release 配置读取、跨省候选不进入 rerank 输入、正常重排和重复查询稳定性。
- `test_policy_retrieval.py` 共 47 项通过（29 项离线、18 项本机隔离 PostgreSQL）；项目 Python 全量离线发现 129 项通过，未设置测试库时 83 项按既有规则跳过。

### 风险与边界

- Python 同步适配器通过有界等待实现超时；若底层模型忽略取消并长期阻塞，工作线程可能继续存活。真实本地 rerank 模型仍需在接入层实现可取消调用或进程隔离。
- rerank 成功会改变 top-k 选择，因此评测与缓存身份必须同时记录 release、模型和配置版本；本次只提供运行时诊断，不实现 golden set 基线与质量阈值。

## 35. M4a-05：在线检索节点接入 RetrievePolicy

**日期与范围**：2026-11-19。实现 issue #12：在线检索节点只通过唯一 `RetrievePolicy` seam 读取政策证据，不生成报告、不执行资格规则，也不接 LLM Gateway。

### 实现

- `RetrievalRunBinding` 由 RunLifecycle 权威记录构造，冻结 principal、run、不可变 `release_id`、已确认画像范围或 POLICY_QA 的必要任务范围，以及学校/文档类型、检索模式和 top/pool 配置。
- 在线检索节点会把可信 binding 的 principal/run/release/task 回写到图状态，避免客户端正文伪造值流入后续节点；已确认画像范围直接来自可信 `ConfirmedProfileScope`，图状态中的画像副本不能覆盖省、报考年、科类或批次。
- 节点只构造 `PolicyRetrievalQuery` 并调用 `RetrievePolicy`，不构造 SQL、不访问 `policy_*` 表，也不调用 `HybridRetriever`。同步数据库/模型 adapter 通过线程执行，避免阻塞事件循环。
- `FOUND` 原样交接稳定 `EvidenceRef` tuple 与诊断；`NO_EVIDENCE`、`UNSUPPORTED_SCOPE`、`RELEASE_UNAVAILABLE` 保持各自状态。单路降级同时设置 `degraded` 和 `retrieval_diagnostics.degraded_reason`。
- `PolicyRetrievalError` 写回稳定 `retrieval_status` / `retrieval_error`，两路均不可用仍为 `RETRIEVAL_UNAVAILABLE`，不伪装成 `NO_EVIDENCE`。返回结果或任一证据的 release 与 run binding 不一致时拒绝交接并返回 `EVIDENCE_RELEASE_MISMATCH`。
- 文档正文只作为 `DocumentEvidence.excerpt` 数据交接；节点不解析其中的系统指令、工具调用或流程控制语句。`build_graph` 支持注入 `RetrievePolicy` 与 binding，使节点从空桩变为可执行接线。

### 测试

- `test_retrieval_node.py` 从节点/图外部行为覆盖：可信 principal 与 release、已确认画像范围防覆盖、POLICY_QA 任务范围、`FOUND` 证据交接、`NO_EVIDENCE`/`UNSUPPORTED_SCOPE`/`RELEASE_UNAVAILABLE` 状态保真、单路降级、两路技术失败、跨 release 拒绝、文档指令不改变图控制。
- 图级测试通过 `build_graph(..., retrieve_policy=..., retrieval_binding=...)` 调用完整图，确认注入的 seam 实际被在线节点执行。

### 风险与边界

- binding 的组装代码必须从可信认证上下文、run 记录和已确认 `profile_revision` 取值；节点已拒绝状态覆盖，但生产装配尚未暴露 POLICY_QA API/子图。
- 当前 legacy `graph.build` 无 checkpointer；`EvidenceRef` / 诊断 dataclass 若进入官方 PostgreSQL saver，仍需后续验证序列化形制。
- 本票不实现查询改写、结构化事实 SQL、资格规则、报告生成、LLM Gateway 或真实模型质量门禁。

## 36. M4a-06：检索 golden set 草稿与可复现评测

**日期与范围**：2026-11-19。实现 issue #13 的检索评测 harness、首批 30 条 Agent
草稿和冻结 fixture baseline；不宣称 Francis 已完成 domain owner 定标，也不验收真实
rerank 模型质量。

### 实现

- 新增不可变版本目录 `data/golden/retrieval-hunan-2025-v0.1.0-draft/`。30 条 Case
  覆盖院校章程、全国/省级长期政策占位、学校+专业组合、`NO_EVIDENCE`、跨省、跨年、
  跨科类与错误学校干扰；院校章程用例按现有 ETL 规则绑定 2025 本地原料生成的稳定
  `doc_id/chunk_id`。
- manifest 固定 `dataset_version`、Case 文件 SHA256、case count、release、owner、
  `owner_status=PENDING_SIGNOFF` 与创建时间。loader 在构造查询前校验 manifest/Case
  hash、必填字段、湖南 scope 值域、criticality、预期证据和 must-exclude 冲突。
- `eval/retrieval_eval.py` 的 runner 只调用注入对象的 `retrieve` seam，生产 CLI 传入
  实际 `RetrievePolicy`。查询固定 `top_k=pool_k=20`，可同时计算 recall@5/20 与 MRR。
- `NO_EVIDENCE` Case 只断言结果为精确 `NO_EVIDENCE`，不进入 recall/MRR 分母；普通
  Case 未命中进入分母并计 0。
- 每条实际证据逐项检查省、有效年份、科类、批次、学校和 must-exclude 来源；
  `scope_leak_rate > 0` 绝对使门禁失败。失败 Case 保留实际证据和 first diff，安全
  泄漏优先于质量差异展示。
- baseline 记录 dataset hash、release、Git/源码树身份、Python/平台、embedder、
  rerank、RRF、index version、`as_of`、top/pool 与逐例结果。运行身份不含生成时间，
  相同输入可复现同一 `result_sha256/eval_run_id`。
- `scripts/run_retrieval_eval.py` 支持实际 `RetrievePolicy` 与确定性 boundary
  fixture。CI fixture 不连数据库、不下载模型；其
  `authoritative_quality=false`，只用于验证 harness，不作为真实检索质量结论。

### 测试与 baseline

- 新增 11 项离线测试和 1 项 DSN gated 隔离 PostgreSQL 测试，覆盖 manifest hash、
  字段/scope/冲突校验、30 条草稿覆盖、recall/MRR 分母、`NO_EVIDENCE`、技术故障、
  rerank=false 与 release 配置不一致、逐例 scope/must-exclude leak、first diff、
  真实 `RetrievePolicy` seam 与 baseline 身份复现。
- 冻结 30 条 fixture 运行通过：recall@5=1.0、recall@20=1.0、MRR=1.0、
  scope leak=0、28 条有效检索 Case、2 条 `NO_EVIDENCE` 通过。上述数值仅为 harness
  冒烟，不代表真实 release 上的检索质量。
- baseline 写入
  `data/golden/baselines/retrieval-hunan-2025-v0.1.0-draft.rerank-off.fixture.json`。

### 风险与边界

- 全国/省级长期政策两条仍标记 `PENDING_RELEASE_BINDING`，没有伪造正式 chunk ID；
  真实运行前必须绑定不可变 release 与真实政策文档。
- 当前真实政策索引、真实 embedding 模型与正式 release 尚未在本票内运行；真实
  baseline 仍需显式 DSN、release 和 index version。
- `rerank_enabled=false` 基线可运行；rerank 打开的真实模型质量留待后续 ticket。
- Francis 的 `PENDING_SIGNOFF` 必须保留。签字前不得把 fixture PASS、草稿标注或
  baseline 当作正式质量门禁。合并审查补充：真实 `RetrievePolicy` 运行的
  `authoritative_quality` 也只在 `owner_status=SIGNED_OFF` 时为 true，避免未签字
  草稿借由真实库执行被误当作权威质量结论。

## 37. M4a code review 修复

**日期与范围**：2026-11-19。针对 M4a #7–#13 的审查 findings 修复检索正确性、评测
身份与生产装配缺口；不扩展资格规则、最终报告或 LLM Gateway。

### 实现

- `RetrievePolicy` 的 release 校验、索引/rerank 配置读取、候选查询和证据投影统一在
  一个只读 `REPEATABLE READ` 事务快照内执行；单通道故障使用 savepoint 隔离，保持
  hybrid 同范围降级语义。
- 学校筛选改成“空筛选或不限学校；否则校级文档精确匹配或非校级 `school_id=NULL`
  放行”，避免全国/省级政策被学校条件误删。
- 候选基底前置过滤完整文档类型、issuer 类型、hash 格式、文本长度、locator 和适用域；
  不可投影候选统一不计入证据，全部不可投影时稳定返回 `NO_EVIDENCE`，技术错误仍为
  技术错误。
- 融合内部候选保留 vector rank、keyword rank、RRF 分；迁移 18 在 release 上持久化
  `rrf_k=60` 并由 CHECK 拒绝非契约值，检索读取 release 身份而非隐式常量。
- 新增 `RunLifecycle.GetRetrievalRunBinding(...)`：GENERATE_PLAN 等任务从已确认
  `profile_revision` 构造 scope，POLICY_QA 从 task input 构造 scope；principal、
  release、run 和 scope 均不向调用方开放覆盖。`build_run_bound_graph(...)` 提供生产
  图工厂，启动在线图的独立 run API 仍待后续接线。
- golden set 新增不可变 `v0.2.0-draft`，30 条 Case 均带 `interference`；跨省/跨年/
  跨科类/错误学校标签有结构化冲突值，scope leak 额外检查 release、文档类型、学校
  筛选和缺失校级身份。第二轮 review 删除未发布且不可加载的 v0.1 草稿。
- EvalRun/baseline 记录 dataset SHA、model route、price table、verifier、算法配置、
  environment、hardware profile 与 baseline identity。recall@5 继续作质量门禁；
  MRR 只在真实且 `SIGNED_OFF` 的权威运行中参与门禁。安全泄漏、Case 技术错误和
  `NO_EVIDENCE` 误判始终阻断。

### 测试

- `test_policy_retrieval.py` 覆盖全国/省级政策在学校筛选下仍可命中、全候选不可投影
  返回 `NO_EVIDENCE`、迁移 18 CHECK、release 快照并发场景，以及 storage-contract
  查询计划；行为断言仍从 `RetrievePolicy` 进入。
- `test_retrieval_eval.py` 覆盖 interference 必填与独立 leak 复现、release/文档类型/
  缺失学校身份泄漏、权威 MRR 门禁和完整 EvalRun 身份字段。
- `test_run_lifecycle_retrieval.py` 使用真实隔离数据库覆盖已确认 plan profile 和
  POLICY_QA task scope 到 `build_run_bound_graph(...)` 的在线装配。

### 风险与边界

- 真实政策索引仍未绑定正式 release；`v0.2.0-draft` 的 owner 仍为
  `PENDING_SIGNOFF`，fixture 基线不构成真实 recall/MRR 结论。
- `release.rrf_k` 当前契约固定 60；若未来需要可调融合参数，必须先更新 runtime
  contract 并以新迁移放宽 CHECK，不能逐请求覆盖。
- 生产启动在线检索图的 API/子图仍需在后续编排票接线；本轮已移除“只能由测试构造
  binding”的阻塞，但未把完整的 planner 流程接到 worker。

## 38. M4a 第二轮 code review 修复

**日期与范围**：2026-11-19。修复第二轮双轴审查发现的 evaluator、可信 binding、
内容身份和 golden set 结构问题；不扩展资格规则、最终报告或 LLM Gateway。

### 修复

- `GetRetrievalRunBinding` 不再接受调用方传入 `school_ids/document_types`；
  POLICY_QA 只从持久化 task input 读取，GENERATE_PLAN 从已确认 Profile 构造 scope，
  school/document 筛选默认为空。`BuildRetrievalGraph` 与 `build_run_bound_graph`
  同步移除覆盖参数。
- servicer 不再在初始化时编译未绑定的默认图，也不保留未使用的 `_graph.astream`
  旁路；在线检索只能通过显式 run-bound graph factory 进入。
- 查询 embedding 生成后立即检查实际长度和有限数值。声明维度与实际返回维度不一致
  时稳定返回 `INDEX_MODEL_MISMATCH`，hybrid 不得降级为关键词路掩盖身份错误。
- 候选 SQL 强制 `policy_chunk_set.content_sha256 = policy_doc.content_sha256`，
  证据投影再做防御性一致性检查；hash 不匹配时返回 `NO_EVIDENCE`。
- 评测接口显式接收 `as_of`，scope leak 使用完整有效区间判断，不再只比较年份。
- 删除未发布且 loader 无法读取的 v0.1 golden 草稿及旧 baseline；保留有效 v0.2。
  `data/golden/README.md` 明确 retrieval-only 版本只要求 `qa.jsonl + manifest.json`，
  端到端版本才要求 `cases.jsonl`。
- 统一结果诊断对象和 manifest SHA256 格式校验，删除重复分支。

### 测试

- 新增/加强真实库测试：POLICY_QA task input 的学校与文档范围进入 binding、调用方不能
  覆盖、全国政策在学校筛选下保留、chunk-set hash 不匹配、实际 embedding 维度错误。
- `test_retrieval_eval.py` 新增 `as_of` 日期边界泄漏用例，并遍历所有已提交 golden
  版本执行 loader。
- 离线 `make py-check py-test`：166 tests 通过（92 skipped）。
- 真实 DSN `make py-test`：166 tests 通过（1 个外部 algo 测试跳过）。
- fixture CLI 以 `--as-of 2025-11-19` 重跑成功，仍保持 `quality_gate_applied=false`。

### 风险与边界

- golden set v0.2 仍为 `PENDING_SIGNOFF`，fixture baseline 不是正式质量结论。
- 公共 POLICY_QA Submit API 仍需后续编排票提供 query text 和结构化筛选字段；当前
  binding 已保证即使内部 task input 存在筛选值，也不会由调用方直接覆盖。

## 39. M4a 双轴复核收口

**日期与范围**：2026-11-19。针对第二轮双轴 review 的剩余硬发现做最终收口。

### 修复

- `GENERATE_PLAN`、`REVISE_PLAN` 等任务也从持久 task input 读取学校与文档类型筛选；
  不再只对 `POLICY_QA` 生效，调用方仍不能传入覆盖参数。
- 校级文档判定同时检查 `document_type` 与 `issuer_type`。`charter` /
  `university_notice` 即使被误标为 national/provincial，缺少 `school_id` 也不得进入
  正式结果；真正的 national/provincial `policy` 仍可在学校筛选下命中。
- `gap-analysis.md` 同步当前 RunLifecycle、政策检索 seam、在线检索节点与剩余待办，
  删除“所有业务节点均为 TODO”等陈旧表述。
- 测试中的科类字段统一使用 `category`，移除废弃 `track`。
- 重新生成 code identity 为 `b291889` 的 fixture baseline；旧 baseline 明确标为
  历史留存，不再冒充当前冻结结果。

### 测试

- 新增“省级误标的校级章程缺 school_id”真实库回归，并保留全国/省级长期政策正向用例。
- 新增 `GENERATE_PLAN` 从持久 task input 读取学校/文档类型筛选的 run-bound 图测试。

## 40. M4a POLICY_QA 前端验收纵切

**日期与范围**：2026-11-19。补通
`POLICY_QA → gateway → RunLifecycle → RetrievePolicy → 前端证据卡`；不生成最终政策
答案、资格判断或报告。

### 实现

- proto additive 新增 `PolicyQaInput`、`PolicyEvidenceResult`、
  `PolicyEvidenceItem`、RunView/RunEvent typed evidence，以及
  `POST /v1/policy-queries`。
- RunLifecycle 接受 typed POLICY_QA，按持久 task input 构造检索 query；`FOUND` 和
  `NO_EVIDENCE` 发布不可变 `POLICY_EVIDENCE`，scope/配置/技术错误进入稳定 FAILED。
- 新增 `PostgresPolicyRetriever` 生产 adapter；未配置 embedding 时 hybrid 保持同范围
  关键词降级。
- 迁移 19 允许 `run_result` 保存无 Profile 的 `POLICY_EVIDENCE`。
- 前端新增政策检索模式、证据卡、安全 `NO_EVIDENCE` 文案、hash/locator/source 展示；
  文档原文只按纯文本渲染。
- 新增 acceptance seed、Make target 和 `docs/m4a-frontend-acceptance.md`。

### 测试

- Python：POLICY_QA FOUND/NO_EVIDENCE、失败 reason、gRPC GetRun/RESULT 一致性。
- Go：typed policy-query service/HTTP 和 `policyEvidence` JSON 投影。
- 前端：typed 提交、证据安全渲染、NO_EVIDENCE、失败提示。
- Playwright：既有 preview、POLICY_QA FOUND、POLICY_QA NO_EVIDENCE。
- 真实 orchestrator + gateway HTTP 烟雾：返回 `FOUND`，evidence 的 release/hash/
  locator/source 完整。
- acceptance seed 通过真实 `RetrievePolicy`：转专业与全国资助政策返回 FOUND，火星
  查询返回 NO_EVIDENCE。
- 复核后验收 seed 改为按 seed version + application year 派生内容哈希 `release_id`，
  已存在时只复用不删除；年度章程使用当前验收年的有效区间，重复执行不会改写已发布
  release，也不会因 run 外键失败。
- Evidence wire 补齐 `evidence_type/content_sha256/created_at/producer_version` 和
  `rerank_timeout_ms`；前端仅允许 http/https 来源链接；运行中任务刷新后会恢复 SSE。
- 新增真实 2025 章程装载脚本：从主库复制 2,859 份文档和已有 1024 维 embedding，
  回填 school_id/hash/source/chunk set/applicability/release，发布
  含 relevance policy 的 `rel_real_2025_8b7c08a71dc6d68a375284ad`。2,093 个
  超过 600 字符的旧 chunk
  不进入 release；最终装载 2,856 份文档、23,453 个合规 chunk。
- 生产入口改用 worker 异步执行，避免首次 BGE 模型加载阻塞 `SubmitRun` 并造成
  gateway 504；新增 `POLICY_RETRIEVAL_AS_OF`，旧 2025 数据验收固定
  `as_of=2025-11-19`。

### 边界

- 当前只展示检索证据，不生成最终政策解释。
- seed 只允许写入数据库名含 `test` / `acceptance` 的 DSN。

## 41. M4a follow-up：相关性资格与 top_k 上限

**日期与范围**：2026-11-19，对应 issue #14。修复 `top_k=5` 在单校、单份章程内
用低相关同文档 chunk 补满结果的问题；不改变硬过滤和证据投影。

### 实现

- 迁移 20 新增 release 级 `release_policy_relevance`，保存策略版本、最低 vector
  cosine similarity 和最低 keyword similarity。
- `RetrievePolicy` 在 RRF 前分别过滤两路低相关候选；`top_k` 明确为最多返回数量，
  合格候选不足时返回实际数量，全部不合格时返回 `NO_EVIDENCE`。
- diagnostics 增加 `relevance_filtered_candidates` 和
  `relevance_policy_version`，经 proto additive 字段进入 GetRun/RESULT 和前端。
- 真实 2025 release 升级为 `rel_real_2025_8b7c08a71dc6d68a375284ad`，初版策略为
  `m4a-relevance-v1`、vector `0.50`、keyword `0.10`。该校验值来自真实 golden
  set 初步分布，仍需 owner 在扩展数据上复核。
- golden set 新增不可变 `v0.3.0-draft`：31 条，新增真实 2465 同文档 hard-negative
  用例；harness 报告 `evidence_precision@5`、`irrelevant_evidence_rate`，并对
  `must_exclude_chunk_ids` 命中逐例失败。
- `v0.4.0-draft` 绑定真实 release：9 个失效院校 expected chunk 重映射到当前合规
  chunk；2 条政策用例标为 `PENDING_RELEASE_BINDING`，2 条长沙理工用例标为
  `PENDING_RELEASE_REBUILD`，2 条“没有该专业限制”语义用例标为
  `PENDING_SEMANTIC_RELEVANCE`。非 ACTIVE case 不调用检索器，不进入指标分母，
  但在 baseline 中显式报告跳过数量与原因。

### 验证

- 真实 2465 查询：`vectorCandidates=8`，过滤 7 条，最终仅返回 `c0004`。
- v0.3 fixture baseline：recall/MRR/precision 均为 1，仅证明 harness 与契约。
- v0.3 真实 vector 评测：recall@5/MRR 为 0.931，relevance precision 仍只有
  `0.064`，旧 30 条草稿中存在 expected chunk 已被 600 字符规则移除和
  `NO_EVIDENCE` 标签与真实数据不一致的问题。新 2465 用例通过且无 hard-negative
  leak；真实质量门禁继续 FAIL，不冒充 owner signoff。
- v0.4 真实 vector 诊断：25 条 ACTIVE 全部进入分母，recall@5/MRR 为 1.0、
  scope leak 为 0，precision@5 为 0.149，另有 6 条已知缺口显式跳过。由于
  `quality_gate_applied=false` 且 owner 未签字，这不是正式质量通过结论。

## 42. M4b-01：EvaluateEligibility 最小纵向闭环

**日期与范围**：2026-11-19，对应 issue #16。建立结构化资格规则的第一条
release-bound 路径；不实现完整规则生命周期、PARTIAL/NONE 覆盖、条件字段与
RunLifecycle 接线。

### 实现

- proto additive 新增 `EvaluateEligibility`，请求携带 CONFIRMED 画像版本及不可变
  事实快照、run 绑定的 `release_id`、适用域和院校专业组候选；响应包含专业
  PASS/FAIL/UNKNOWN、覆盖、规则/来源证据、稳定原因和诊断计数。
- 迁移 21 新增 `rule_version` 与 `rule_coverage`；规则内容、来源证据、release
  与审查状态有数据库约束，FULL 覆盖必须声明来源和说明。
- `svc-algo` 从 release 读取 PUBLISHED 且有效的规则快照，只有 FULL 选科覆盖允许
  PASS；release 缺失/撤销返回 BLOCKED。规则选择、求值和汇总隐藏在唯一 seam 内。
- 旧 `CheckConstraints` 保持原行为和协议；新路径不改候选身份、概率、分档或优化顺序。

### 验证

- 业务表驱动测试覆盖选科 PASS/FAIL、撤销 release、DRAFT 画像拒绝与缺失覆盖 UNKNOWN。
- service DTO 测试与 bufconn 生成客户端测试覆盖新 RPC。
- 真实 PostgreSQL 集成测试验证 release/rule/coverage 插入 → EvaluateEligibility →
  PASS/FAIL/证据 → release 撤销 BLOCKED。
- fresh-schema migration 测试通过；全量 Go 单测与 Python 离线测试通过。

### 边界

- 当前只验证 `SUBJECT_REQUIREMENT` 与 FULL 覆盖；完整 AST、PARTIAL/NONE、更多
  条件字段、专业五态组汇总和正式 run 接线仍由 M4b-02~07 完成。

## 43. M4b-04：受限表达式与三值逻辑

**日期与范围**：2026-11-19，对应 issue #18。把规则条件收敛为 typed AST，
实现字段 allowlist、Kleene 三值求值和资源上限；不实现 RuleVersion 生命周期、
覆盖不足语义、条件字段接入、组级五态或 RunLifecycle 接线。

### 实现

- `RuleExpression` 增加 field/value/values/left/right 类型化节点，支持 `AND / OR / NOT / EQ / NEQ / IN / GTE / LTE / PRESENT / CONTAINS_ALL / CONTAINS_ANY / SUBJECT_MATCH`。
- AST 使用 `expression_version=1`；字段必须同时属于 Profile allowlist 与规则的
  `required_profile_fields`，声明集合必须与 AST 实际引用完全一致。
- `AND` 在任一分支 NO_MATCH 时为 NO_MATCH，`OR` 在任一分支 MATCH 时为 MATCH；
  已知 FAIL 优先于其他规则产生的缺失事实，避免结果被输入顺序覆盖。
- 缺失、UNKNOWN、DECLINED 产生缺失字段；字段存在但类型错误产生 UNKNOWN，不做宽松转换。
- 表达式 JSON 使用严格解码，拒绝未允许键；资源上限为深度 16、节点 64、
  列表 32 项、单字符串 256 字节、JSON 32 KiB。
- 迁移 23 新增 `expression_version` 与 32 KiB schema 约束；数据层把残留解析错误
  归一为 `ValidationReason`，由唯一 `EvaluateEligibility` seam 返回 BLOCKED。

### 验证

- biz 表驱动测试覆盖 AND/OR/NOT 全三值组合、全部原子 operator、`PRESENT` 三态、
  类型不匹配、FAIL 优先级、未知字段/operator、非法 arity、资源超限与确定性。
- service seam 测试验证非法 AST 映射为 BLOCKED；真实 PostgreSQL + bufconn 测试验证
  typed AST 重复调用逐项一致，以及含额外 SQL 键的持久化规则被稳定阻断。
- storage contract 测试验证 `expression_version != 1` 与超过 32 KiB 的表达式被 schema 拒绝。
- `00→23` fresh-schema 测试通过；新增迁移已加入 sqlc、RunLifecycle 测试迁移链与
  policy acceptance schema helper。

### 边界

- 条件字段的实际画像快照接入、FULL/PARTIAL/NONE 覆盖、RuleVersion 生命周期、
  专业五态汇总和 RunLifecycle 正式门禁仍由其他 M4b 票完成。

## 44. M4b-02：RuleVersion 生命周期与发布生效门禁

**日期与范围**：2026-11-19，对应 issue #17。实现规则版本生命周期、内容不可变、
发布审计与冲突门禁；不扩展 PARTIAL/NONE 覆盖匹配、条件字段接入、组内五态汇总或
RunLifecycle 接线。

### 实现

- 迁移 22 为 `rule_version` 增加创建/发布操作者与时间字段、已发布元数据约束和
  精确 scope 唯一索引；新增 `rule_lifecycle_event` 追加式审计。
- 数据库触发器只允许 `DRAFT -> REVIEWED -> PUBLISHED -> REVOKED`，禁止直接插入
  PUBLISHED、跳级、回退、从 REVOKED 恢复以及修改已完成版本的内容字段。
- DRAFT 不绑定 release；发布事务原子校验 release、同 release 证据引用、对应
  RuleCoverage 和同 `rule_key` 的 applicable scope 唯一性。
- `EvaluateEligibility` 防御残留重叠生效规则并返回 `BLOCKED / RULE_CONFLICT`；
  REVOKED 规则不参与评估、不回退旧版本，并使未被新发布版本覆盖的规则类型失效为
  UNKNOWN。
- fresh-schema 迁移链更新到 23；现有 M4b service 集成夹具改为 DRAFT → REVIEWED →
  PUBLISHED 的合法路径。

### 验证

- 真实 PostgreSQL 生命周期测试覆盖审计、非法转换、内容不可变、发布冲突、缺覆盖、
  撤销原因、撤销后 UNKNOWN，以及新版本发布后恢复覆盖。
- Eligibility 行为测试覆盖 DRAFT/REVIEWED 不改变已发布结果、重叠 scope 阻断、
  非重叠同 key 共存，以及与 M4b-04 AST 校验共存。
- RunLifecycle fresh-schema 迁移测试通过。

### 边界

- 撤销规则不自动发布替代规则或新 release；恢复完整覆盖必须发布新规则版本和
  release。
- 本票不从文本或 LLM 生成规则，也不提供审核 UI。

## 45. M4b-03：RuleCoverage 与覆盖不足 UNKNOWN

**日期与范围**：2026-11-19，对应 issue #19。实现覆盖声明的最具体 scope 匹配、
FULL/PARTIAL/NONE/缺失覆盖语义和 release 发布后冻结；不接入条件字段、组内五态
汇总或 RunLifecycle。

### 实现

- `EvaluateEligibility` 对每个专业按 `rule_type + scope` 选择最具体覆盖声明；
  专业级声明覆盖组级、院校级和省级声明。同具体度出现多个适用声明时返回
  `BLOCKED / RULE_CONFLICT`，不依赖查询返回顺序。
- 仅 FULL 覆盖允许专业 PASS；PARTIAL、NONE 和无匹配声明均产生
  `UNKNOWN / RULE_COVERAGE_MISSING`，整体结果为 PARTIAL。
- 迁移 25 增加规范 scope key、未知/空 scope 校验、可追溯来源元素校验、命名化的
  FULL 来源和 PARTIAL/NONE 缺口说明约束。
- release 处于 STAGING 时允许准备覆盖；PUBLISHED/REVOKED 后禁止插入、更新或删除
  `RuleCoverage`，保证绑定 run 读取的覆盖快照不会被后续配置替换。
- 现有测试夹具调整为先以 STAGING 写入规则/覆盖，再发布 release；新增真实
  PostgreSQL 冻结与规范化约束测试，以及经生成客户端进入 `EvaluateEligibility`
  的覆盖行为测试。

### 验证

- biz 测试覆盖 PARTIAL/NONE、缺失覆盖、最具体 FULL/PARTIAL 覆盖、同具体度冲突和
  非适用声明隔离。
- service + PostgreSQL 测试覆盖 broad FULL + specific PARTIAL、broad NONE +
  candidate FULL、同具体度冲突、NONE、缺失覆盖，以及发布后修改覆盖不影响重复评估。
- data storage 测试覆盖规范 scope 唯一性、非法 scope、FULL 空来源、缺口说明和
  PUBLISHED 后插入/更新/删除均失败。
- `00→25` fresh-schema、Go build/check/test、Python check/test 与既有 M4b 集成
  测试通过。

### 边界

- 本票不生成 coverage，也不从条款推断覆盖；发布者必须显式声明。
- 条件字段接入、专业五态组汇总和正式 run 接线仍由 M4b-05~07 完成。

## 46. M4b-05：条件事实字段与已确认画像快照

**日期与范围**：2026-11-19，对应 issue #20。把单科成绩、色觉、性别、户籍和
学籍接入结构化资格规则，并把 Profile 已确认 revision 投影为确定性事实快照；
不实现覆盖不足、组级五态或 RunLifecycle 正式接线。

### 实现

- 迁移 24 新增 `eligibility_profile_field` 字段目录，声明稳定 field key、
  `STRING / INT / STRING_LIST` 类型和枚举允许值；规则 allowlist 不再只依赖
  编译期字符串列表。
- `EvaluateEligibility` 从规则仓库加载字段目录，表达式字段、literal 类型和枚举
  值均按目录校验。未登记字段返回 `RULE_UNKNOWN_FIELD`。
- 单科规则使用 `single_subject_scores.<subject>`，覆盖语文、数学、英语、物理、
  历史、化学、生物、思想政治和地理；色觉、性别、户籍与学籍使用 Profile 契约中
  的稳定 flat key。
- 缺失、UNKNOWN、DECLINED 在专业结果中成为 `MISSING_REQUIRED_FACT` 并返回规则
  实际引用的缺失字段；类型或枚举非法产生 UNKNOWN，不宽松转换后 PASS。
- Profile 内部新增已确认 revision 到资格事实的确定性投影。DRAFT revision
  返回 `PROFILE_NOT_CONFIRMED`，事实按 field key 排序，不读取自由文本或历史数据。
- 新迁移已加入 sqlc schema、RunLifecycle 测试迁移链和 policy acceptance schema
  helper；fresh schema 名称更新为 `00→25`。

### 验证

- biz 表驱动测试覆盖单科 PASS/FAIL/缺失/UNKNOWN、色觉、性别 DECLINED、非法枚举、
  户籍与学籍缺失，以及只返回适用规则所需字段。
- service + 真实 PostgreSQL + bufconn 测试逐项覆盖五类条件事实；另验证字段目录
  是运行时 allowlist 权威来源。
- 字段目录 storage contract 验证关键字段、枚举值和非法 value type。
- Python 测试验证 CONFIRMED facts 的稳定排序与 DRAFT 拒绝。
- 合并后专用测试库 fresh schema `00→25` 与相关 integration tests 通过。

### 边界

- 仍不在本票实现 FULL/PARTIAL/NONE 的最具体 scope 覆盖、组内 `PASS_PARTIAL`
  和 RunLifecycle 正式资格门禁。

## 47. M4b-06：组内多专业五态与 PASS_PARTIAL

**日期与范围**：2026-11-19，对应 issue #21。完成院校专业组内多专业资格汇总、
候选身份完整性校验与稳定排序；不接 RunLifecycle。

### 实现

- 专业结果保持 `PASS / FAIL / UNKNOWN / NOT_APPLICABLE /
  MISSING_REQUIRED_FACT` 五态。明确限报优先为 FAIL，当前计划外专业为
  NOT_APPLICABLE，规则所需事实缺失为 MISSING_REQUIRED_FACT。
- 组级汇总为 `PASS / PASS_PARTIAL / FAIL / UNKNOWN`：全部专业 PASS 才为 PASS；
  至少一个 PASS 且其余仅为 FAIL/NOT_APPLICABLE 时为 PASS_PARTIAL；无 PASS 时为
  FAIL；任一 UNKNOWN/MISSING_REQUIRED_FACT 使整组 UNKNOWN。
- PASS_PARTIAL 保留每个专业状态、受限专业及其 failure rule keys；组证据按稳定
  major/rule 顺序去重，不得把部分专业可报解释为整组可报。
- 候选身份加入 fail-closed 校验：空组、重复组、空/重复专业代码、缺失学校或组
  身份均返回 BLOCKED；请求 scope 缺省省、年、科类或批次返回
  `ELIGIBILITY_SCOPE_INVALID`。
- 结果按 group key、major code 和规则证据稳定排序；同输入重复评估逐项一致。
- 本票无新增 schema：候选身份与专业状态属于 RPC 请求/响应，现有 21→25 表结构
  已足够，无需为纯请求校验增加空白迁移。

### 验证

- biz 测试覆盖 PASS_PARTIAL、全 PASS、全 FAIL、UNKNOWN 支配 PASS、
  MISSING_REQUIRED_FACT、NOT_APPLICABLE、空组、重复组/专业、缺失身份、
  不完整 scope 和确定性排序。
- service + 真实 PostgreSQL + bufconn 测试验证多专业 PASS_PARTIAL、受限专业
  明细、重复请求逐项一致，以及空组/重复专业/不完整 scope 的稳定阻断。
- 既有生命周期、AST、条件事实、覆盖、fresh schema 与 CheckConstraints 回归通过。

### 边界

- 本票不判断招生计划是否存在；`NOT_APPLICABLE` 依赖上游提供的
  `in_current_plan=false`，仍不得从历史专业身份推断当年可报。
- RunLifecycle 尚未消费该五态结果，V1_FORMAL 门禁仍由 M4b-07 完成。

## 48. M4b-07：RunLifecycle 资格门禁与结果发布

**日期与范围**：2026-11-19，对应 issue #22。把 `EvaluateEligibility` 接入真实
`GENERATE_PLAN` run，发布资格事实并在优化前执行 fail-closed 门禁；不实现文本检索、
报告 Verifier 或 LLM 规则生成。

### 实现

- RunLifecycle 从 run 记录读取不可变 `release_id` 和 CONFIRMED
  `profile_version`，用 `ProfileRuntime.ToEligibilityFacts` 生成稳定排序的 typed
  facts 快照，再将 `EstimateProb` 候选组映射为 `EligibilityCandidateGroup`。
- 固定调用顺序改为 `RankConvert → EstimateProb → EvaluateEligibility →
  OptimizeWishlist`。旧 `CheckConstraints` RPC 保持可调用，但正式资格链路不再调用
  或选择其宽松结果。
- 校验资格回复只包含原始候选组和专业，重复、缺失、注入组/专业、未定义状态均
  fail-closed。COMPLETE 要求无 UNKNOWN；PARTIAL 要求存在 UNKNOWN 诊断。
- 只有组状态 PASS/PASS_PARTIAL 的候选进入优化。PARTIAL 中已通过组可形成内部预览，
  但 `formal_eligibility_ready=false`；UNKNOWN、MISSING_REQUIRED_FACT、FAIL、
  BLOCKED 不进入正式资格集合。PASS_PARTIAL 组保留受限专业明细。
- `AlgorithmPreview` additive 增加 typed `eligibility` 和 `eligibility_gate`，
  GetRun/WatchRun 返回同一资格事实；`formal_recommendation` 仍固定为 false。
- 发布前仍复验 lease、state_version、确认画像与 release；评估期间 release 撤销时
  以 `RELEASE_REVOKED` 终止，不发布结果。

### 验证

- 新增 RunLifecycle 真实 PostgreSQL 端到端测试覆盖 COMPLETE、PARTIAL、
  PASS_PARTIAL、资格 release 阻断和候选组注入；断言优化输入、资格门禁、受限专业
  和 formal=false。
- 既有 ALGORITHM_PREVIEW、失败门禁、lease、transport、migration 与检索回归全部
  通过；生成 gRPC client 路径验证新增资格 proto 可往返。
- `make api`、`make build && make check test`、`make sqlc-check`、
  `make py-check py-test` 通过；完整 RunLifecycle 真实库套件 77 tests OK，
  `ORCHESTRATOR_ALGO_ADDR` 未设置时真实 svc-algo TCP 用例按约定跳过。

### 边界

- `ALGORITHM_PREVIEW` 仍不是正式报告，`formal_recommendation=false`；COMPLETE 只
  表示资格事实完整，不代表报告证据或全部发布门禁通过。
- 当年招生计划身份、在线 RAG、三类证据 Verifier、LLM Gateway、Strategy、
  Critic 和正式报告仍未完成，继续作为未核验条件与后续 milestone。

## 49. M4b code review 修复：资格门禁与 release 快照收口

**日期与范围**：2026-11-19。修复 M4b 双轴审查发现的正式门禁、历史专业身份、
规则静态校验、release 字段目录和稳定原因缺口；不扩展文本检索、报告 Verifier 或
LLM 规则生成。

### 实现

- 迁移 26 将画像字段目录改为 `release-scoped`：发布后冻结，新 release 从不可变模板
  初始化；`EvaluateEligibility` 只读取 run 绑定 release 的字段快照。
- 所有声明的 `RuleCoverage` 类型都进入候选覆盖检查；只要存在 PARTIAL/NONE 或缺失
  声明，候选保持 UNKNOWN，结果不得成为 COMPLETE。RunLifecycle 还会独立校验
  coverage 全为 FULL，防止错误回复把部分覆盖提升为正式资格。
- `EstimateProb` 的候选专业新增 `in_current_plan`，由报考年 `enrollment_plan`
  的专业代码成员查询产生。RunLifecycle 原样转交该事实，历史专业不再被无条件标为当年
  可报。
- `SUBJECT_MATCH` 必须显式携带规则 `subject_req`；评估不再回退到历史候选文本。
- `EvaluateEligibilityRequest` 新增 `profile_snapshot_sha256`，由 confirmed profile
  version 与排序后的 typed facts 生成跨语言一致摘要；算法拒绝缺失、重复字段或摘要不匹配。
- `EligibilityRepo` 同时校验 `release_scope`，省年科批不受绑定 release
  支持时返回 `PROFILE_UNSUPPORTED_SCOPE`。
- `RuleVersion` 进入 REVIEWED/PUBLISHED 前执行与运行时共享的静态校验：受限 AST、
  allowlist/required fields、literal 类型、来源证据、scope 和有效区间不合法时拒绝状态
  转换。
- 补齐 `RULE_SOURCE_MISSING` 与 `BATCH_UNSUPPORTED` 稳定原因；清理误导性的
  group key 参数命名。

### 验证

- 新增覆盖缺口、非本科批、缺来源、字段目录 release 隔离、调用方错误标记 COMPLETE
  覆盖、历史专业计划成员和非法规则审查的回归测试。
- Go 纯逻辑与全部 M4b 真实 PostgreSQL 集成测试通过；fresh schema 从 00→26 通过。
- Python 全量通过；完整 RunLifecycle 真实库套件 80 tests OK，仅真实
  svc-algo TCP 用例因未设置地址按约定跳过。

## 50. M4c-01：VerifyPackage 文档事实闭环

**日期与范围**：2026-11-19，对应 issue #25。建立三类证据共用的证据包与 Claim
运行时形状，只实现 `DocumentEvidence` 校验；正式发布门禁和持久化仍由后续票完成。

### 实现

- 唯一事实校验入口为 `VerifyPackage(EvidencePackage) -> VerificationReport`。
  三类证据分派、Claim 顺序、引用检查和稳定原因均隐藏在 verifier 内部。
- 文档 Claim 校验同一 `release_id`、EvidenceRef/原件的 excerpt hash、section path
  与字符 locator、省份/科类/批次/学校/年份/生效区间，以及原文对 Claim 的支持关系。
- 悬空、跨 release、身份字段缺失和无法核验文本均 fail-closed；重复 Claim/evidence
  ID 不得通过。可选文本 entailment adapter 必须带版本，并在报告标记
  `deterministic=false`。
- 报告按 Claim ID 与 Evidence ID 稳定排序；同一输入重复校验得到相同状态、原因和
  检查顺序。现有 `DocumentEvidence`、`RetrievePolicy`、`ALGORITHM_PREVIEW` 和
  `POLICY_EVIDENCE` 行为保持兼容。

### 验证

- 新增 `VerifyPackage` 行为测试覆盖成功、hash/locator、错误学校/年份/适用域、
  悬空与跨 release、重复 Claim、身份缺失、文本不可核验、版本化 entailment 和
  确定性重放。
- `make py-check py-test` 通过：212 tests OK，112 个依赖真实库/外部服务的用例按
  既有约定跳过。

### 边界

- 本票未实现 `StructuredEvidence`、`DerivedEvidence`、`PASS/BLOCKED/PARTIAL`
  完整剥离状态机、资格/说明统一门禁或正式结果持久化。

## 51. M4c-02：VerifyPackage 结构化事实闭环

**日期与范围**：2026-11-19，对应 issue #26。通过同一个 `VerifyPackage`
主 seam 增加 `StructuredEvidence` 校验；不实现派生算法重算、完整发布状态机或
正式结果持久化。

### 实现

- `EvidenceRef` additive 支持类型化 `StructuredEvidence`，包含数据集、表、
  主键、业务键、字段路径、规范值、单位、来源年份、精度、行/快照 hash 和
  `release_id`。
- `VerifyPackage` 通过注入的只读 `StructuredEvidenceAuthority` 重新解析同 release
  权威行或快照，逐字段比较业务身份、值类型、值、单位、来源年份和精度；不使用
  RAG 文本或自由 JSON 替代结构化来源。
- NULL、缺失值语义和 0 保持区分。行/快照 hash、权威 release、业务键或字段身份
  不一致时 fail-closed；缺少类型化 payload、权威来源或完整身份时返回
  `UNVERIFIABLE`。
- 结构化 Claim 的数字、单位和来源年份必须带渲染绑定，且绑定必须指向该 Claim 的
  EvidenceRef。绑定缺失、改写或错指证据时返回 `CLAIM_RENDER_MISMATCH`。
- 现有文档事实路径、失败即关闭语义与稳定排序保持不变。

### 验证

- `VerifyPackage` 行为测试新增同 release 正向、数值/类型/单位/来源年份/精度漂移、
  错误业务键、NULL/0 区分、hash/跨 release、类型化 payload 缺失和渲染绑定错误。
- `make py-check py-test` 通过：222 tests OK，112 个依赖真实库/外部服务的用例按
  既有约定跳过。

### 边界

- 本票只定义结构化权威来源边界和注入式 adapter，不在 Verifier 内拼接任意数据表
  SQL；生产查询 adapter 由后续集成与持久化票据接线。
- `DerivedEvidence`、普通 `UNVERIFIABLE` 剥离、统一正式门禁和不可变结果仍由后续
  M4c tickets 完成。

## 52. M4c-03：VerifyPackage 派生事实闭环

**日期与范围**：2026-11-19，对应 issue #27。通过同一个 `VerifyPackage`
主 seam 增加 `DerivedEvidence` 校验；不新增算法、不实现完整撤销状态机或正式结果
持久化。

### 实现

- `EvidenceRef` additive 支持类型化 `DerivedEvidence`，保存 operation、算法与版本、
  配置 hash、输入快照 hash、Profile、release、样本年份、输出字段/值、单位、精度、
  不可变产物 hash、假设和告警。
- `VerifyPackage` 可注入版本化 `DerivedEvidenceRecomputer`。具备重算条件时以重算
  产物为权威；否则比较已保存不可变算法产物。缺少重算或比对所需身份时 fail-closed。
- 派生 Claim 精确比较规范输出值、单位、精度、样本年份、输出字段、Profile、
  release 和算法身份；样本年份按去重后的升序规范集合比较。
- 数值统一按规范十进制比较，兼容 `Decimal`、数字字符串与 JSON 数值表示；近似浮点
  值不得借用容差通过，输出值或单位缺失时 fail-closed。
- 数字、单位和样本年份必须带指向本 Claim EvidenceRef 的渲染绑定；绑定缺失、改写或
  错指证据时返回 `CLAIM_RENDER_MISMATCH`。
- 近似概率、效用等派生 Claim 不得表达“保证录取”“保证你被录取”等超出算法能力的
  承诺；合法的“不保证录取”免责声明保持可校验。

### 验证

- `VerifyPackage` 行为测试覆盖不可变产物正向、确定性重算成功与不可用回退、算法/
  配置/输入/Profile 身份冲突、产物 hash、规范十进制、近似值拒绝、数字/单位/精度/
  样本年份漂移、缺值与身份字段缺失、样本年份规范化、确定性重放和录取承诺越界。
- `make py-check py-test` 通过：242 tests OK，112 个依赖真实库/外部服务的用例按
  既有约定跳过。

### 边界

- 本票只定义派生事实校验与注入边界，不在 orchestrator 内重写算法。
- 普通 `UNVERIFIABLE` 剥离、统一正式门禁、不可变证据包持久化和原子发布仍由后续
  M4c tickets 完成。

## 53. M4c-04：Claim 关键性、剥离与 VerificationReport 状态机

**日期与范围**：2026-11-19，对应 issue #28。通过同一个 `VerifyPackage`
主 seam 汇总三类 Claim，完成 `PASS / BLOCKED / PARTIAL` 报告状态、关键性门禁和
普通未知剥离；不实现资格正式门禁、结果持久化或 RunLifecycle 发布接线。

### 实现

- `ClaimVerification` additive 返回 Claim 的关键性；`VerificationReport` 返回最终
  保留事实的 citation coverage。
- 任一 `CONTRADICTED` 均阻断；任一 `CRITICAL / UNVERIFIABLE` 返回
  `BLOCKED / CRITICAL_CLAIM_UNVERIFIABLE`。只有 `NORMAL / UNVERIFIABLE` 可进入
  确定性剥离。
- 存在普通未知且仍有可发布的高关键事实时返回 `PARTIAL`，并稳定列出
  `stripped_claim_ids`；若所有事实都被剥离，则不生成可发布报告。
- Claim/Evidence 类型不匹配、重复 Claim/evidence ID、非法关键性和畸形 Claim
  属于结构性失败，不能被普通剥离绕过。
- 报告按 Claim ID、Evidence ID、blocking reason 与 stripped Claim ID 稳定排序；
  同一输入重复运行返回相同状态、原因、检查证据和覆盖结果。

### 验证

- `VerifyPackage` 行为测试新增普通未知剥离、关键未知、矛盾优先级、错误类型、
  重复身份、非法关键性、零保留事实、三类证据混合 `PARTIAL`、citation coverage
  和阻断/剥离顺序确定性。
- `make py-check py-test` 通过：254 tests OK，111 个依赖真实库/外部服务的用例按
  既有约定跳过。

### 边界

- citation coverage 按最终保留的事实性 Claim 计算；`BLOCKED` 没有可发布结果，
  因此覆盖率为 `0.0`。
- 资格 `COMPLETE / PASS_PARTIAL` 门禁、必需说明校验、不可变证据包持久化和原子
  发布仍由后续 M4c tickets 完成。

## 54. M4c-05：资格、版本与必需说明的 V1_FORMAL 统一门禁

**日期与范围**：2026-11-19，对应 issue #29。通过唯一的 `VerifyPackage` seam
接入版本化正式门禁上下文；只消费既有资格结论，不新增资格规则，也不实现证据包
持久化、原子发布或 RunLifecycle 接线。

### 实现

- `EvidencePackage` additive 增加可选 `gate_context`，兼容既有构造方式；缺失时
  fail closed 为 `BLOCKED / FORMAL_GATE_CONTEXT_MISSING`。
- `formal-gate-v1` 上下文绑定 release、确认 Profile、result version、算法配置
  版本/hash、Verifier 版本、资格摘要和三类必需说明。版本或身份冲突均稳定阻断，
  `DERIVED` 证据的配置 hash 也必须与上下文一致。
- 资格只接受整体 `COMPLETE`，覆盖必须存在且全部为 `FULL`，冲突/未知计数必须为
  零。组只接受 `PASS` 或没有 `UNKNOWN` 的 `PASS_PARTIAL`；组/专业
  `UNKNOWN / FAIL / MISSING_REQUIRED_FACT` 全部阻断。合法 `PASS_PARTIAL` 必须
  保留受限专业说明，并拒绝“全组可报”等整组过度表述。
- `DISCLAIMER`、`RISK_NOTICE`、`NO_ADMISSION_GUARANTEE` 必须同时满足类型语义、
  规范化文本 hash 和同 release 证据绑定；缺失或改写阻断。
- 文档、结构化、派生 Claim 均禁止“保证录取”“包录取”“100%录取”等等价承诺；
  合法“不保证录取”仍可通过。全局门禁失败时不会降级为可发布 `PARTIAL`。

### 验证

- `VerifyPackage` 行为测试新增 gate context 缺失、版本/身份冲突、算法配置漂移、
  资格整体与覆盖、组/专业状态、合法与非法 `PASS_PARTIAL`、必需说明语义/hash/
  release 绑定、跨 Claim 类型录取承诺和全局阻断优先于普通剥离。
- `make py-check py-test PY=/Users/francis/miniconda3/envs/ai-gaokao-counselor/bin/python3`
  通过：266 tests OK，112 个依赖真实库/外部服务的用例按既有约定跳过。

### 边界

- 本票不创建 `V1_FORMAL` 结果，不持久化证据包/验证报告，不修改 RunLifecycle
  发布事务；这些仍由后续 M4c tickets 完成。

## 55. M4c-05 合并前 Review 修复：三项 fail-open 收口

**日期与范围**：2026-11-19，对应 issue #29 合并前 review。仍只通过同一个
`VerifyPackage` 公共行为修复，不改变正式门禁 API。

### 问题与实现

- 资格覆盖原先只检查 `status=FULL`，空白 `rule_type` 也会被视为有效覆盖；现要求
  `rule_type` 非空白，否则 `BLOCKED / ELIGIBILITY_COVERAGE_INCOMPLETE`。
- `PASS_PARTIAL` 的受限专业使用空串时，`"" in restriction` 恒真，导致空/空白专业
  代码被误认为已说明；现直接拒绝空/空白受限专业代码，返回
  `ELIGIBILITY_RESTRICTED_MAJORS_MISSING`。
- 必需说明原先只检查 `DOCUMENT` payload，任意 `STRUCTURED` payload 会直接跳过；
  现要求结构化字段路径明确指向规范文本或对应 notice 类型，值精确等于
  `normalized_text` 且内容 hash 绑定同一行/快照。无关招生计划行返回
  `REQUIRED_NOTICE_MISMATCH`。

### 验证

- 按严格 TDD 先为三个缺口各加 `VerifyPackage` 失败测试，确认旧实现分别误报
  `PASS`，修复后转绿。
- `test_evidence_verification.py` 70 tests OK。

## 56. M4c-06：不可变证据包与 V1_FORMAL 原子发布

**日期与范围**：2026-11-20，对应 issue #30。将既有 `VerifyPackage` 报告接入
`RunLifecycle.PublishFormalResult`，只新增正式发布边界，不新增第二个事实校验入口，
不改写 `ALGORITHM_PREVIEW / POLICY_EVIDENCE` 的既有语义。

### 实现

- `PublishFormalResult` 只接受 `PASS` 与合法 `PARTIAL`；`BLOCKED` 在同一事务将 run
  失败为 `VALIDATION_FAILED`，不创建可查询的正式结果。PARTIAL 持久化剥离后的
  Claim/Evidence 和完整报告，保留稳定 `stripped_claim_ids`。
- 新增迁移 `27_formal_evidence_results.sql`：`run_result` 支持 `V1_FORMAL`；
  `run_evidence_package`、`run_verification_report` 与 `run_result_state_event`
  保存正文、摘要和独立状态。结果正文、证据包、报告与状态事件均由触发器拒绝
  UPDATE/DELETE；release 撤销只追加 `STALE / RELEASE_REVOKED`。
- 发布事务再次校验 principal、state version、lease owner/epoch、确认 Profile、
  cancel 标志和 release 有效性；发布与取消竞争由行锁及条件更新裁决。成功时结果、
  证据包、报告、run 状态、ACTIVE 状态和 RESULT 事件同一事务提交。
- 输入摘要按 Claim、EvidenceRef、必需说明、资格组/专业和限制信息稳定排序后计算。
  同一 `result_version` 同摘要重试不重新执行验证、不追加第二份正文或 RESULT 事件；
  同版本不同输入摘要或跨 run 复用返回 `RESULT_VERSION_CONFLICT`。
- orchestrator proto 加法新增 `FormalResultSummary`；gRPC GET 与 Watch 返回同一
  `result_version`、证据包/报告摘要、逐 Claim 状态、阻断原因和剥离 ID。既有结果
  类型继续走原映射，旧结果不会被提升为 `V1_FORMAL`。

### 验证

- 严格 TDD 先覆盖成功发布、发布/取消竞态、Profile/lease/state、release 撤销、
  PARTIAL、BLOCKED、结果版本冲突、规范化重放、数据库不可变、release 失效标记和
  gRPC GET/Watch 一致性，再实现对应行为。
- `test_run_lifecycle_formal.py` 11 tests OK。
- 全部 `test_run*.py` 真实 PostgreSQL 回归：92 tests OK，1 个依赖外部 algo 地址
  的用例按既有条件跳过。
- 迁移覆盖 fresh `00 → 27` 与 `26 → 27` 升级，并验证升级前 `POLICY_EVIDENCE`
  结果仍可读取且 `formal_result` 为空。

### 边界

- LangGraph 图内 verify/report 节点、报告重写与回算上限仍由后续 M5/FR-18/FR-19
  工作接入；本票只闭合已生成证据包的正式发布边界。

## 57. M4c-06 合并前 Review 修复：不可变结果反例与等价证据重放

**日期与范围**：2026-11-20，对应 issue #30 独立审查 blocker。按严格 TDD 修复
正式结果降级绕过、嵌套样本年份误判冲突，并删除无调用方的结果映射死代码。

### 问题与实现

- `run_result` 不可变触发器原先只检查 `NEW.result_kind`。已发布的 `V1_FORMAL`
  可被联合同步改为 `POLICY_EVIDENCE`、清除确认 Profile 并改写 payload；现
  UPDATE 同时检查 `OLD.result_kind` 与 `NEW.result_kind`，DELETE 继续检查旧行。
- 正式输入摘要原先未规范化 Claim 与 `DERIVED` 证据中的嵌套 `sample_years`；
  Verifier 已按去重升序集合比较，导致仅年份顺序不同的同一证据包重试误报
  `RESULT_VERSION_CONFLICT`。现 canonicalization 在 Claim 规范化事实、Claim/Notice
  引用和 evidence registry 中统一年份集合语义，年份集合实质变化仍冲突。
- 删除 `RunLifecycle._formal_result_payload`；该静态映射无调用方。

### 验证

- 真实 PostgreSQL 先新增正式结果降级 UPDATE 反例，修复前未抛错，修复后拒绝且
  原 `V1_FORMAL`、确认 Profile 与 payload 保持不变。
- `PublishFormalResult` 公共行为测试先复现重排嵌套 `sample_years` 的重试冲突，
  修复后同 `result_version`、证据包摘要与报告摘要均稳定重放。
- `test_run_lifecycle_formal.py` 13 tests OK；`test_run_lifecycle_migration.py`
  5 tests OK。
- `make py-check py-test PY=/Users/francis/miniconda3/envs/ai-gaokao-counselor/bin/python3`
  通过：284 tests OK，126 个依赖真实库/外部服务的用例按既有约定跳过。
- `make build check test` 通过。

## 58. M4c 最终 code review 修复：正式结果与 Verifier 深模块收口

**日期与范围**：2026-11-20，固定点 `6647529`，审查对象
`codex/m4c-integration` tip `ae6e535`。按 findings 全部 11 项施工，保持
`VerifyPackage` 唯一事实校验 seam 和既有 RunLifecycle 状态机。

### 实现

- 正式结果 payload 原样保存 `disclaimer / risk_notice / no_admission_guarantee`，
  GET、Watch、RESULT 与 gRPC 返回同一文本；proto 仅加法新增字段。完整报告字段从
  误导性的 `verification_summary` 改名为 `verification_report`，读取旧 payload 时保留
  `verification_summary` 兼容回退。
- `verifier.py` 从 1481 行拆为包级状态机、`claims.py`、`formal_gate.py`、
  `normalization.py`、`models.py`；唯一公开校验入口仍为 `VerifyPackage`。
- 样本年份去重升序统一使用 `evidence.canonical.normalize_sample_years`；Formal
  输入摘要与 Verifier 不再各自实现。
- 删除仅测试覆写的 `_before_formal_publish_hook`，改为构造函数注入
  `formal_publish_guard`；取消与 release 撤销竞态行为保持不变。
- 新增迁移 `28_authoritative_formal_gate.sql`：RunLifecycle 在
  `EvaluateEligibility` 后保存不可变 `run_formal_gate_artifact`。发布时复核 release、
  Profile、Verifier、release manifest 配置、适用日期与资格摘要；伪造
  `COMPLETE + PASS` 返回 `QUALIFICATION_ARTIFACT_MISMATCH`。
- 文档事实要求单位/来源年份出现在同一原文和渲染绑定；结构化事实要求业务键生成的
  subject、field path 对应的 predicate 及语义渲染绑定；必需说明引用统一经 registry
  解析；entailment adapter 版本必须一致。
- `applicable_as_of` 纳入 package 和 gate context，Verifier 不再读取系统当前日期。

### 验证

- 严格 TDD 逐项新增公开行为反例：文档单位/年份漂移、结构化 subject/predicate 错配、
  未注册 notice、adapter 版本漂移、日期变化、伪造资格上下文、notice 不进入正式结果。
- 真实 PostgreSQL：`test_run_lifecycle_formal.py` 14 tests OK，
  `test_run_lifecycle_migration.py` 5 tests OK；fresh `00 → 28` 与 `26 → 28` 升级通过。
- 全量 Python（显式真实测试库）：290 tests OK，2 个依赖外部 algo 地址/语料条件的用例
  按既有约定跳过。
- `make api PY=/Users/francis/miniconda3/envs/ai-gaokao-counselor/bin/python3`、
  `make build check test` 均通过。

## 59. M4c 最终 merger 修复：跨原文拼接与 artifact 日期漂移

**日期与范围**：2026-11-20，分支 `codex/m4c-review-fixes`，修复 issue #30/M4c
最终 merger 的两个 blocker。

### 问题与实现

- `VerifyPackage` 原先分别验证每个文档渲染绑定，可能把不同 excerpt 中的数值、单位
  和来源年份拼成一条 `VERIFIED` 文档 Claim。现要求所有关键绑定共同解析到同一
  `EvidenceRef`，跨原文组合返回 `CLAIM_RENDER_MISMATCH` 并阻断。
- 权威正式 `run_formal_gate_artifact` 的 `applicable_as_of` 原取 worker 当前时钟
  日期，导致同一 run 在跨日重试时产生不同摘要并冲突。现从 run 查询中携带持久化
  `created_at`，固定使用其日期生成 artifact。

### 验证

- 严格 TDD 先新增三个独立 excerpt 的文档 Claim 跨原文拼接反例，修复前错误为
  `PASS`，修复后为 `BLOCKED / CONTRADICTED / CLAIM_RENDER_MISMATCH`。
- 真实 PostgreSQL 公开行为测试将 lifecycle 时钟固定在 2024-01-02，断言 artifact
  日期等于 run 的持久创建日期；修复前为 `2024-01-02` 而 run 创建日为
  `2026-11-20`，修复后一致。
- 同一 run 在 2024-01-02 生成 artifact 后模拟 worker 中断，再由 2024-01-03 的
  lifecycle 重试；重试成功且 artifact 日期与 SHA-256 均保持不变。
- `test_evidence_verification.py` 77 tests OK；真实库
  `test_run_lifecycle_algorithm_preview.py` 6 tests OK，1 个外部 algo 用例按既有
  条件跳过；全量 Python 292 tests OK，2 个条件用例跳过。
- `make api`、`make build`、`make check`、`make test` 与 `git diff --check` 通过。

## 60. M5a-01 类型化 Chat 单次治理闭环

**日期与范围**：2026-11-20，issue #32。按 TDD 从生成的
`LLMGatewayClient.Chat` 纵向打通可信身份、release-bound 路由/价格、预算预占、
fake provider、按实际 usage 结算和权威调用账本；不实现后续票的并发重试、对账
用例、prompt/schema、敏感分类、缓存或完整流式接口。

### 契约与实现

- `ChatRequest` 保留 1–7 号旧字段，只加法新增 session/run/release/logical call、
  prompt/schema 引用和数据分类；正文没有 principal、model、provider、URL 或重试
  覆盖字段。`ChatResponse` 保留 1–5 号旧字段，新增 typed `Usage / Cost /
  AttemptSummary`、structured output、route/price 版本、logical/ledger 身份及
  finish reason。
- gRPC handler 只解析 incoming `x-principal-id` 并映射稳定错误。私有 Chat 引擎在
  provider 前校验 principal、session、run、release 状态与 run 剩余执行时间，随后
  从 release 绑定的 `llm_role_route + llm_model_allowlist + llm_price_table`
  解析唯一主模型；缺路由、禁用白名单、非 PUBLISHED/已生效价格均在 provider
  零调用下失败。
- 请求 hash 覆盖规范化 messages、prompt/schema 引用、role、route、模型顺序、
  temperature、token/timeout、release 与数据分类。`logical_call_id` 在
  `(principal_id, session_id)` 内唯一：已完成同 hash 重放持久结果和同一 attempt，
  异 hash 返回 `IDEMPOTENCY_CONFLICT`，在途/待对账返回 `CALL_IN_PROGRESS`。
- 预占事务先按唯一约束创建 logical call，再用单条条件 UPDATE 原子检查和增加
  权威预算行，并创建首个 `RESERVED` attempt；provider 调用只在事务提交后发生。
  成功结算事务以 logical call 行锁防止重复释放，扣减全部预留、增加实际 provider
  tokens/cost，并将 logical call、attempt 与持久最终内容一起推进到 `SUCCEEDED`。
  provider 不确定故障保留预留并进入 `PENDING_RECONCILIATION / PARTIAL_UNKNOWN`；
  `llm_call_log` 仅作 best-effort 查询投影，不参与余额裁决。
- `migrations/29_llm_gateway_chat.sql` 只增新建价格版本/条目、release 模型白名单与
  role route、预算作用域、逻辑调用和 provider attempt，并补 `llm_call_log`
  查询身份列。价格表在 `published_at` 前允许补齐条目，发布后条目禁止原地
  UPDATE/DELETE；预算行 CHECK 与条件 UPDATE 同时守住 token/cost 双上限。
- `cmd/llm-gateway` 读取本服务 YAML 和 `LLMGW_DSN`，未接线真实 provider 时注入
  fail-closed provider；Compose 改为依赖 PostgreSQL、挂载只读 configs 并暴露
  9001。Makefile 新增 `test-integration-llmgw`，只接受显式可写
  `LLMGW_TEST_DSN`。

### 验证

- 主 seam 测试使用真实 PostgreSQL 临时 schema、Kratos gRPC + bufconn 和生成的
  `LLMGatewayClient`，直接查询账本仅用于证明预占/结算和唯一约束。
- 覆盖：成功原子预占/结算、可信 metadata 替代 Trace/正文、跨 principal 与非法
  run、未知角色、禁用模型、缺失价格、release 撤销、预算不足零 attempt、请求上限
  不能放宽、完成态重放/异请求冲突、旧 proto 字段号、fresh 结构 schema `00/07→29`、`28→29`
  升级及价格/预算/logical call 唯一性。
- `LLMGW_TEST_DSN=postgresql://.../ai_gaokao_ticket2_test?sslmode=disable \
  go test -tags integration ./internal/llmgw ./cmd/llm-gateway -count=1` 通过。

### 边界与风险

- 生产入口当前只有 fail-closed provider；没有真实模型、真实费用或供应商 SDK
  重试。`StreamChat` 明确返回 `STREAM_NOT_IMPLEMENTED`。
- 未实现多 attempt fallback/并发幂等竞争、未知费用对账用例、prompt/schema 注册
  与验证、六类敏感数据分类及缓存；这些是 #33 起的后续 M5a 增量。
- request hash 当前对规范化请求做 SHA-256，数据分类与直接标识检测尚未接入；
  在 #37 完成前不得把该入口视为完整隐私边界。

## 61. M5a-02 principal + session 预算硬边界

**日期与范围**：2026-11-20，issue #33。按 TDD 从生成的
`LLMGatewayClient.Chat` 收紧 `principal_id + session_id` 作用域的 token/cost
硬上限；不实现 #34 的多 attempt fallback、并发同 ID 仲裁或后续 prompt/schema、
敏感分类、缓存与流式能力。

### 事务与状态语义

- 默认作用域仍为 50,000 provider tokens 与 150,000 `USD_MICRO`。同一 session
  的新 run 继续累计，同一 principal 的新 session 建立独立 scope；服务端不接受
  客户端覆盖限额或作用域身份。
- logical call 直接以 `IN_PROGRESS`、首个 provider attempt 直接以
  `IN_PROGRESS` 与预算预占在同一事务提交。预占只用一条带
  `budget_status = 'ACTIVE'` 和 `settled + reserved + request <= limit` 条件的
  UPDATE 仲裁；失败事务回滚 logical call/attempt，provider 零调用。
- 成功结算在同一事务中扣减整笔预留、增加实际 provider tokens/cost，并持久化
  attempt 与逻辑调用；成功响应只在该事务提交后返回。
- provider 通过 `ErrProviderNotCharged` 明确表示未受理且未计费时，attempt 进入
  `FAILED_NO_CHARGE`、logical call 进入 `FAILED`，token/cost 预留全部释放。
- 实际 usage 超过预占时，attempt 进入 `PENDING_RECONCILIATION`，logical call
  进入 `BUDGET_OVERRUN`，预算 scope 锁存 `budget_status = 'BUDGET_OVERRUN'`；
  留存既有预留和实际 usage 供人工对账，后续新调用在 provider 零调用下返回
  `BUDGET_OVERRUN`。
- `llm_call_log` 继续只做 best-effort 投影；其写入失败、表缺失或内容不一致均不
  参与预算判定，权威来源仅为 scope / logical call / provider attempt。
- `migrations/30_llm_budget_overrun.sql` 只增作用域封锁状态与时间、扩展 logical
  call 状态约束；不修改历史迁移或旧 `chat_session` 预算列。

### 验证

- 真实 PostgreSQL 主 seam 测试从生成的 `LLMGatewayClient.Chat` 进入，使用
  Kratos gRPC + bufconn 和 fake provider；每个测试使用独立临时 schema。
- 覆盖并发 token 上限、并发成本上限、失败请求零 provider/attempt、跨 run 累计、
  独立 session 不串账、默认 50K/150K、成功结算差额、明确未计费全额释放、
  实际用量超预留后封锁后续调用，以及删除 `llm_call_log` 后权威预算仍正确。
- fresh schema 覆盖 `00/07→30`，升级路径覆盖 `28→29→30`；SQL CHECK 拒绝非法
  `budget_status`。

### 边界与风险

- provider 仍只有 fail-closed 实现；`ErrProviderNotCharged` 是可测试的内部
  adapter 语义，不代表已接入真实供应商 SDK。
- 因 `BUDGET_OVERRUN` 保持预留待人工对账，本票没有新增对账 RPC；#35 需要提供
  幂等对账并决定超限费用的最终入账方式。
- route 当前仍按最坏 `max_attempts` 预占但只执行首个 attempt；fallback 与可重试
  故障分类属于 #34。

## 62. M5a-03 logical call 幂等、attempt 与主备切换

**日期与范围**：2026-11-20，issue #34。按 TDD 从生成的
`LLMGatewayClient.Chat` 补齐 logical call 幂等竞争、独立 provider attempt、
可重试故障主备切换与逐 attempt 成本归属；不实现 #35 的未知计费对账或后续
prompt/schema、敏感分类、缓存与流式能力。

### 契约与实现

- `ProviderFailure` 把供应商故障收窄为“adapter 已确认未受理且未计费”的类型化
  结果。`Retryable=true` 才允许使用 route 的下一个模型；参数错误、认证失败和
  schema 不支持返回不可重试稳定原因。未分类连接异常、超时与断流继续走
  `PENDING_RECONCILIATION / PARTIAL_UNKNOWN`，不猜测为免费。
- 每个 route 模型最多对应一次真实供应商请求；provider adapter 契约要求关闭 SDK
  内部自动重试。`migrations/31_llm_attempt_limit.sql` 将 route
  `max_attempts` 和 attempt 序号同时限制在 1–2，避免配置或 SDK 重试绕过上限。
- 首次 attempt 与预算预占仍在同一事务提交。可重试失败先把当前 attempt 推进为
  `FAILED_NO_CHARGE`，再为下一模型创建新的 `attempt_id` 和独立起止时间；备用
  模型成功后只推进一个 logical call，按实际模型价格汇总 usage/cost、释放总预留
  差额，并把两个 attempt 按顺序返回。
- 两次 attempt 都明确未计费时，logical call 进入 `FAILED` 并释放全部预留；
  不可重试故障只记录首个失败 attempt、不调用备用模型。成功请求的幂等重放从权威
  attempt 表重建完整 attempt 摘要、最终实际模型和账本身份，不新增 provider 调用。
- request hash 测试覆盖 messages、prompt/schema 版本、raw schema、数据分类、
  temperature、token/timeout、release 与 route 模型顺序；同 hash 完成态重放，
  异 hash 返回 `IDEMPOTENCY_CONFLICT`。真实 PostgreSQL 唯一约束负责同 ID 并发
  仲裁，输家返回 `CALL_IN_PROGRESS`。
- 复核实测补上终态失败重放：`FAILED / BUDGET_OVERRUN` 返回持久稳定原因，
  `PARTIAL_UNKNOWN` 返回待对账原因，只有 `RESERVED / IN_PROGRESS` 返回
  `CALL_IN_PROGRESS`；终态重放不会新增 provider attempt。

### 验证

- 主 seam 测试继续使用真实 PostgreSQL 临时 schema、Kratos gRPC + bufconn、生成的
  `LLMGatewayClient` 与确定性 fake provider。
- 覆盖主模型 429 后备用成功、两个 attempt 的不同 ID/模型/起止时间/provider
  request id/usage/cost/error、两次 attempt 耗尽、不可重试参数/认证/schema
  故障不降级、同 ID 在途并发只产生一次 provider 调用、完成态重放、request hash
  维度、预算完整释放及 `00/07→32` / `28→29→30→31→32` 迁移。
- `LLMGW_TEST_DSN=postgresql://.../ai_gaokao_ticket2_test \
  go test -tags integration ./internal/llmgw/... -count=1` 通过。

### 边界与风险

- 生产入口仍只有 fail-closed provider；typed failure 的“确认未计费”责任在真实
  adapter，未接线前不能视为供应商账单真相。
- 未知计费、重复对账、预算超限最终入账仍由 #35 处理；本票只保证未知异常不进入
  自动重试，并保留既有 logical/attempt/预留身份。
- 核心硬限制为两次 attempt；若未来需要同模型网络重试或多级 fallback，必须发布
  新契约与迁移，不能扩大现有 `max_attempts` 语义。

## 63. M5a-04 未知计费保留与内部对账

**日期与范围**：2026-11-20，issue #35。按 TDD 补齐 `Chat` 的供应商超时、
usage 缺失/不可信与可能已受理断连语义，以及不新增公共 RPC 的内部追加式对账；
不实现 #36 prompt/schema、#39 `StreamChat` 或真实供应商/账单拉取。

### 契约与实现

- Provider 内部响应新增显式 `UsageStatus`。`UNTRUSTED`、未知状态、全零且未声明
  可信的 usage 与负数 usage 均进入 `PENDING_RECONCILIATION / PARTIAL_UNKNOWN`，
  预算预留保持占用；不再把通用 `ErrProviderUnavailable` 猜成免费或普通不可用。
- `context.DeadlineExceeded` 稳定映射为 `PROVIDER_TIMEOUT`，其他未分类异常映射
  `RECONCILIATION_REQUIRED`。gRPC `ErrorInfo.metadata` 携带
  `logical_call_id / attempt_id / ledger_entry_id`，并复用相同身份重放长期未知
  状态；错误 message 不拼接供应商原始正文。
- `migrations/33_llm_attempt_reconciliation.sql` 为 logical call 冻结包含全部
  attempt 的 `reserved_provider_tokens`，并新建追加式
  `llm_attempt_reconciliation`。迁移前旧行保持 `NULL`，不回填猜测值；对账对
  缺少冻结总额的旧行明确拒绝。
- `internal/llmgw/reconciliation.go::Reconcile` 是进程内 adapter：`CHARGED`
  按调用冻结价格表的使用量原子释放预留并结算；`NO_CHARGE` 原子转
  `FAILED_NO_CHARGE` 并释放。没有可信最终内容时 logical call 为
  `FAILED / RECONCILED_CHARGED`，不返回伪造成功内容。
- 实际 usage/cost 超过 logical 预留时，attempt 保持待对账，logical call 与预算
  scope 锁存 `BUDGET_OVERRUN`，原预留不释放，后续 `Chat` 在 provider 零调用下
  拒绝。相同依据重复对账先命中追加审计并返回原结果；不同依据返回
  `RECONCILIATION_CONFLICT`。审计行保存原 attempt 状态、原错误码、原预留和明确
  usage/cost，发布后 UPDATE/DELETE 被触发器拒绝。

### 验证

- 真实 PostgreSQL 公开 seam 测试继续从生成的 `LLMGatewayClient.Chat` 进入，
  使用 Kratos gRPC + bufconn 与 fake provider；对账 adapter 仅通过内部 Go 接口
  调用，不新增 proto RPC。
- 覆盖 `PROVIDER_TIMEOUT` 身份 metadata、供应商正文不回显、长期未知重复请求
  不新增 provider attempt、usage 缺失与显式不可信、明确计费、明确免计费、
  同依据重复、异依据冲突、超预留封锁后续调用，以及审计原预留/原错误保留。
- fresh/upgrade 迁移扩展到 `33`；既有 Chat、并发预算、主备切换、幂等和终态
  重放测试全部回归通过。

### 边界与风险

- 当前生产 provider 仍为 fail-closed；真实 adapter 必须把供应商超时、未知 usage
  和“是否可能已受理”忠实映射到内部状态，不能依据错误字符串猜测。
- 对账输入 `evidence_ref` 只保存内部账单行或未受理证明引用，不自动抓取账单；
  运营 adapter 负责在其可信边界内取得明确依据。
- 升级前已存在的未知 attempt 没有历史 `reserved_provider_tokens` 可无损推导，
  对账会 fail closed，需先完成人工账务审计，不能静默猜测回填。

## 64. M5a-03 注册 prompt 模板与 response schema

**日期与范围**：2026-11-20，issue #36。按 TDD 从生成的
`LLMGatewayClient.Chat` 收紧 prompt/schema 授权：只允许 release 内注册且未撤销的
版本；不实现 #37 六类敏感分类、#38 缓存或 #39 `StreamChat`。

### 契约与实现

- `ChatRequest` 新增 `map<string,string> prompt_variables`。旧 `messages/json_schema`
  保留 wire 兼容；调用方 system message 返回 `PROMPT_TEMPLATE_NOT_ALLOWED`，raw
  `json_schema` 单独出现返回 `RESPONSE_SCHEMA_NOT_ALLOWED`，不能授权或覆盖注册
  schema。`ChatResponse` 新增 prompt/schema key/version；合法结构化输出继续返回
  typed `structured_output`，并同时返回 route/model/price 身份。
- `migrations/32_llm_prompt_schema_registry.sql` 只增建立 release-bound
  `llm_prompt_template` / `llm_response_schema`，PK 为 `(release_id,key,version)`；
  内容摘要与模板/schema 内容不可原地改写，仅允许 `ACTIVE -> REVOKED`。route 新增
  `requires_response_schema`；logical call 冻结 prompt/schema key/version/摘要并以
  composite FK 绑定注册行。
- `Chat` 在预算预占前解析并校验 prompt/schema。模板用 `{{variable_name}}` 渲染，
  system/user 模板生成服务端消息，调用方非 system messages 只能追加；解析消息、
  prompt/schema key/version 与内容摘要全部进入 request hash。任一项版本变化会形成
  新语义，同 `logical_call_id` 返回 `IDEMPOTENCY_CONFLICT`。
- 结构化 output 必须是 JSON object，并先按唯一注册 schema 校验，再返回
  protobuf `Struct`。校验失败返回 `SCHEMA_VALIDATION_FAILED`；logical call 记为
  `FAILED`、attempt 记录供应商真实 usage 与错误码，权威预算按实际 token/cost
  结算并释放差额，`llm_call_log` 只写 best-effort 失败投影。
- JSON Schema 校验使用已缓存的 `github.com/google/jsonschema-go v0.4.3`；没有
  引入网络 schema 加载或供应商侧授权路径。

### 验证

- 主 seam 测试继续使用真实 PostgreSQL 临时 schema、Kratos gRPC + bufconn、生成
  `LLMGatewayClient` 与 fake provider；直接 SQL 断言仅用于证明预算结算、注册内容
  不可变和失败终态。
- 覆盖：模板 key/version 有效、变量渲染、无 messages 时由模板生成内容、未注册/
  撤销模板、system message 绕过、route 强制 schema、raw schema 单独/覆盖、
  撤销 schema、合法 structured output、schema 失败按真实 usage 结算及安全重放、
  prompt/schema/变量版本进入 request hash。
- fresh schema 覆盖 `00/07 -> 32`，升级路径覆盖 `28 -> 29 -> 30 -> 31 -> 32`；注册
  prompt/schema 内容的 UPDATE/DELETE 与撤销后恢复均被数据库触发器和约束拒绝。

### 边界与风险

- 本票只做授权与 schema 正确性；敏感原文字段识别、出站脱敏仍是 #37。
- `CacheHit` 当前固定为 false，没有缓存表或缓存命中；prompt/schema 版本已进入
  request hash，供 #38 直接扩展缓存键。
- fake provider 把注册 schema 作为内部请求参数，但不代表真实供应商已接线；
  生产入口仍 fail closed，`StreamChat` 仍返回未实现。

## 65. M5a-06 六类数据分类与敏感原文边界

**日期与范围**：2026-11-20，issue #37。按 TDD 从生成的
`LLMGatewayClient.Chat` 收紧云端数据准入与供应商输出；不实现 #38 缓存，
不实现 #39 `StreamChat`，也不改变 run 的业务资格判断。

### 契约与实现

- `ChatRequest` 保留旧 `data_classification` string，加法新增 typed
  `DataClassification data_class = 18`。六类为
  `PUBLIC_DATA / PRINCIPAL_DATA / INQUIRY_DATA /
  SENSITIVE_ELIGIBILITY_DATA / OPERATIONAL_DATA / SECRET`；未知枚举、未知旧值或
  新旧字段映射冲突返回 `REQUEST_INVALID`。
- 旧 `PUBLIC` 映射 `PUBLIC_DATA`，`INTERNAL` 映射 `PRINCIPAL_DATA`，
  `SENSITIVE` 映射 `SENSITIVE_ELIGIBILITY_DATA`。兼容不扩大权限：旧
  `INTERNAL` 不会变成公开数据，旧 `SENSITIVE` 始终拒绝。
- `migrations/34_llm_data_classification.sql` 为每个 route 增加
  `allowed_data_classes`，默认只允许 `PUBLIC_DATA`；数据库拒绝空集合、NULL
  项、未知值和 `SENSITIVE_ELIGIBILITY_DATA / SECRET`。logical call 冻结规范化
  data class，便于审计。
- `Chat` 在 run/route/prompt 持久化与 provider 调用前检测消息、模板变量、trace
  及模板渲染后的完整消息：中国大陆手机号、18 位身份证号、邮箱、常见
  key/token/private-key 形态，以及健康、色觉、视力、性别、户籍/学籍原始事实。
  命中返回 `SENSITIVE_DATA_NOT_ALLOWED`，不写 logical call/log 投影，provider
  零调用。模板渲染后的二次检测可拒绝多个安全变量片段拼成的直接标识。
- 单一 `PASS / FAIL / UNKNOWN` 脱敏规则结论正向通过，包括“色觉要求结论：
  FAIL”“健康限制核验为 UNKNOWN”等上游常用表达；原文敏感描述与结论同时出现
  仍拒绝。
- provider 输出在 schema 校验、持久化和响应前执行同一守卫；结构化输出通过
  schema 后还会递归检查解码后的键和值，避免 `\uXXXX` 转义绕过原始字符串扫描。
  手机号、身份标识或健康原文回显时不返回内容、不写 `response_payload`；真实
  usage/cost 原子结算后 logical call 稳定为
  `FAILED / SENSITIVE_DATA_NOT_ALLOWED`，重放 provider 零新增调用。
  `BUDGET_OVERRUN` 也不再持久化原始模型内容。
- 请求 hash 只保存脱敏规范输入的 SHA-256；日志与错误仅包含稳定 reason，不保存
  model key、prompt 原文或未脱敏供应商响应。

### 验证

- 主 seam 测试继续使用真实 PostgreSQL 临时 schema、Kratos gRPC + bufconn、生成
  `LLMGatewayClient` 与 fake provider，直接 SQL 仅观察拒绝是否零持久化及实际
  usage 结算。
- 覆盖六类 typed 分类、route 允许/拒绝、未知枚举、旧三类别名、敏感/机密永久
  拒绝、手机号（含分隔符）、身份证号、邮箱、model token、健康/色觉/视力/性别/
  户籍/学籍原文、trace/模板变量检测、脱敏结论输入与输出正向、供应商敏感回显
  不落库及稳定重放；merge 复核补测模板渲染拼接直接标识与结构化 JSON 转义回显。
- fresh schema 覆盖 `00/07 -> 34`，升级路径覆盖 `28 -> 29 -> ... -> 34`。

### 边界与风险

- 检测是针对已知高置信形态的横向安全边界，不是完整自然语言 DLP；未命中检测的
  隐晦敏感表达仍需上游画像/规则层负责最小化。当前策略是命中即拒绝，不自动做
  不可靠脱敏。
- 真实 provider 仍未接线，生产入口 fail closed；敏感输出若实际 usage 同时超过
  预算预留，仍需在后续对账流程中协调 `BUDGET_OVERRUN` 与
  `SENSITIVE_DATA_NOT_ALLOWED` 的运营优先级。
- 缓存与 `StreamChat` 未实现；本票只为未来缓存声明默认仅 `PUBLIC_DATA` 可共享。

## 66. M5a-07 release-bound Chat 结果缓存

**日期与范围**：2026-11-20，issue #38。按 TDD 从生成的
`LLMGatewayClient.Chat` 增加 release-bound 结果缓存；不实现 #39
`StreamChat`，不接真实付费 provider。

### 契约与实现

- `migrations/35_llm_result_cache.sql` 为 route 增加默认关闭的
  `allow_result_cache`，为 logical call / 旧投影增加 `cache_hit` 与 key hash，
  并建立 `llm_result_cache`。缓存行保留 release、key hash、prompt/schema/route/
  model/data policy 身份、缓存 schema 版本、claim 租约与
  `IN_PROGRESS / READY / FAILED / INVALIDATED` 状态；request manifest 只保存
  治理摘要和规范化 messages 的 SHA-256，不保存 prompt 明文。
- `internal/llmgw/cache.go` 构造 canonical key：
  prompt key/version/摘要、脱敏 messages、schema key/version/摘要、role、route、
  完整模型序列、temperature、输出上限、release、data classification/policy 与
  `llm-result-cache-v1`。只有 route 显式允许、`temperature=0` 且
  `PUBLIC_DATA` 时才创建 claim。
- 首次 miss 用 `cache_key_hash` 唯一约束和 claim token 抢占；同键并发请求在 holder
  未完成时返回 `CALL_IN_PROGRESS`，provider 仍只调用一次。claim 租约覆盖 route
  允许的全部 attempt，避免双 attempt 接近完整超时后错误接管；过期 claim 可由后续
  请求接管，失败/schema 不通过的 claim 进入 `FAILED`，后续安全重试可重新填充。
- 成功结果会在 schema 与敏感输出守卫之后写入 `READY`。缓存命中另建
  `SUCCEEDED` logical call，`cache_hit=true`、
  `settlement_status=CACHE_HIT`，不创建 provider attempt、不增加预算 settled/
  reserved token/cost；同 logical call 重放继续返回原零费用账本身份。
- release 撤销触发器把相关缓存转为
  `INVALIDATED / RELEASE_REVOKED` 并清除结果。Chat 仍在读缓存前按当前
  `release.status` fail closed；缓存清理只删 `llm_result_cache`，不触碰权威预算、
  logical call、attempt 或已结算费用。
- 供应商原始错误、未脱敏/敏感输出、schema 失败结果与敏感输入 prompt 均不进入
  可读缓存；失败 claim 只保留治理摘要。

### 验证

- 主 seam 测试继续使用真实 PostgreSQL 临时 schema、Kratos gRPC + bufconn、生成
  `LLMGatewayClient` 与 fake provider。覆盖首次 miss/第二次 hit、消息、prompt、
  schema、route、模型、temperature、输出上限、data policy 与 release 变化、
  PUBLIC/temperature/route 准入、结构化结果回放、并发 miss 单调用、幂等重放与
  cache hit 区分、命中后同 ID 重放、release 撤销自动失效、敏感输入/输出与
  provider 错误不缓存、schema 失败不缓存且可重试、清理不改账本。
- fresh schema 验证 `00/07 -> 35` 与缓存表；升级路径验证
  `28 -> 29 -> ... -> 35`、旧 run 保留及 route/logical call 新列存在。

### 边界与风险

- 缓存目前只服务普通 `Chat`；#39 `StreamChat` 仍未实现。生产 provider 仍
  fail closed，未执行真实付费模型调用，供应商 usage/cost 仍由真实 adapter 接线后
  验证。
- 共享缓存仅允许 `PUBLIC_DATA`。未来若开放 principal/session 作用域缓存，必须
  新增加固键和迁移，不能只放宽当前 data class 判断。

## 67. M5a-08 类型化 StreamChat 与 Chat 共用治理

**日期与范围**：2026-11-20，issue #39。按 TDD 从生成的
`LLMGatewayClient.StreamChat` 接通既有 Chat 治理能力；不生成报告，不改
RunLifecycle，不接真实付费 provider。

### 契约与实现

- `ChatDelta` 保留 `content=1`、`done=2`，加法新增 `event=3`。新
  `ChatStreamEvent` 固定承载流内 `sequence`、事件类型、logical/attempt/model、
  delta/final content、usage/cost、finish reason、schema 校验状态、structured
  output、ledger、route/price/release、缓存命中和稳定错误 metadata。
- `Chat` 与 `StreamChat` 都进入 `executeGovernedCall`；流式 handler 仅把同一个
  `ChatResponse` 映射为事件。正常新调用为
  `START -> DELTA -> USAGE -> DONE`，最终结果与普通 Chat 共用敏感守卫、schema
  校验、结算事务、attempt 摘要和安全错误。
- 预算、敏感数据、raw schema 等预占前拒绝只发送一个 `ERROR`，不发送 `START`，
  provider 与 attempt 均为零。缓存命中或已完成幂等重放按 `DELTA -> DONE` 回放；
  缓存 DONE 为 usage/cost 零、attempt 空、`CACHE_HIT`。
- provider 与 schema 错误在 DONE 前发送稳定 `ERROR` 后以同一 gRPC 状态终止，不
  发送成功内容。客户端在 provider 请求可能已受理后断开时，服务端用独立 5 秒
  持久化上下文记录 attempt `PENDING_RECONCILIATION`、logical call
  `PARTIAL_UNKNOWN`、error code `STREAM_INTERRUPTED`，原预算预占保持不改。
- 流序号由 `grpcStreamObserver` 从 1 单调递增，仅属于本次 RPC；proto 描述符测试
  明确禁止 `event_seq` 字段。#39 不需要新增数据库对象，现有 `29` 至 `35`
  账本足以表达流式成功、失败和未知费用。

### 验证

- 主 seam 继续使用真实 PostgreSQL 临时 schema、生成的
  `LLMGatewayClient` 与 fake provider。测试 server 改用原生 `grpc.NewServer`：
  Kratos v2.7.3 的 `Server.Serve(bufconn)` 未初始化 endpoint，流式拦截器会发生
  nil dereference；生产 Kratos 装配不受影响。
- 覆盖正常事件顺序与旧 `content/done` 可读、预算/敏感/raw schema 预拒绝零
  provider、缓存零 attempt 回放、主备两个 attempt 归属、schema 失败真实 usage
  结算、provider 超时待对账、结构化 DONE、客户端断流待对账、`Chat/StreamChat`
  成功与错误语义等价，以及 proto 旧字段号固定和新事件字段号。
- `LLMGW_TEST_DSN=postgresql://.../ai_gaokao_ticket2_test \
  go test -tags integration ./internal/llmgw -count=1` 全量通过；该套件同时回归
  并发预算、幂等、release 缓存、敏感分类与 fresh/upgrade 迁移。

### 边界与风险

- `Provider` 当前仍是一次请求返回最终内容的内部 seam，因此 DELTA 是最终守卫后
  的完整内容块；typed streaming wire 已稳定，但未宣称供应商 token 实时转发。
  真实流式 adapter 接线后仍只能在 DONE 将结果视为业务成功。
- 生产 provider 与账单 adapter 仍 fail closed；未执行真实付费模型调用，因此
  provider request id、usage/cost 和断流账单仍只由确定性测试替身验证。
- 客户端断开后无法接收 ERROR；可通过预占错误 metadata 或后续内部对账按
  `logical_call_id + attempt_id` 定位。未实现自动账单拉取或人工对账 UI。

## 68. M5a 两轴 review-fix：可信 usage、失败收口与 sqlc 收口

**日期与范围**：2026-11-20，`codex/m5a-integration@a48b79e` 的两轴审查修复。
只修改 `review-fixes` worktree；不推送、不关闭 issue，不接真实付费 provider。

### 行为与安全修复

- `ProviderResponse.UsageStatus` 现在只有显式 `TRUSTED` 才进入成功结算。空值、
  `UNTRUSTED` 和未知值即使携带非零 token，也保持
  `PARTIAL_UNKNOWN / PENDING_RECONCILIATION` 与原预算预占；fake provider 的成功
  fixture 全部显式标记可信 usage。
- `StreamChat` 在预算预占后、provider 调用前发送 `START` 失败时，使用独立持久化
  上下文原子转换 logical call/attempt 为 `FAILED / FAILED_NO_CHARGE`，释放预算
  与缓存 claim，并追加失败投影；稳定原因新增 `STREAM_START_FAILED`。
- 生产 `nil` provider 使用 typed `ProviderFailure` 表达“明确未受理”，不再把
  fail-closed 装配误记为未知费用。对应调用返回 `PROVIDER_UNAVAILABLE`，预算释放、
  attempt 为 `FAILED_NO_CHARGE`。
- M5a 角色封闭为 `profile/report/verify/policy_qa`；调用方消息角色只允许
  `user/assistant`，`system` 仍只能由注册模板生成。角色与内容均参与敏感扫描，
  模板渲染后和 JSON 字符串反转义后再次检查。
- `BUDGET_OVERRUN` 在任何缓存读取前预检，并在缓存命中事务内再次锁定复核；阻断后
  不创建新的成功 logical call，也不访问 provider。

### SQL 与结构维护

- `sqlc.yaml` 新增独立 LLMGateway 目标：
  `internal/llmgw/data/queries` → `internal/llmgw/dbgen`。治理读取、预占/结算、
  缓存、对账和投影的条件更新全部改为生成方法；pgx 事务仍由服务编排。
- 生成的 `dbgen` 不混入 algo 包，`make sqlc-generate` / `make sqlc-check` 通过。
- 新增 Python 兼容测试，读取 `make api` 生成的 `ChatDelta.content=1/done=2`、
  additive `event=3`；Go 主 seam 同时验证角色、release、logical call、
  session/run、prompt 等必需字段缺失时在 provider 前 fail closed。

### 验证

- 新增回归测试先从生成客户端观察到失败：空 usage 状态被结算、START 发送失败
  悬挂预占、生产 provider 待对账、角色/消息 role 越界、超限缓存命中成功。
  修复后逐项转绿。
- `LLMGW_TEST_DSN=... go test -tags integration ./internal/llmgw -count=1`
  覆盖完整 M5a 真实 PG 套件；`go test ./internal/llmgw ./cmd/llm-gateway`、
  Python 兼容单测与 sqlc check 通过。

### 边界

- 真实 provider、账单 adapter、真实付费模型与 orchestrator 入口仍未接线；生产
  fail closed。fake provider 只证明治理 seam 行为，不证明供应商实际 usage 或账单。
- 流式 `DELTA` 仍是最终守卫后的完整内容块，不宣称供应商 token 实时转发。

## 69. M5b-01 确定性正式报告与自动 V1_FORMAL 发布

**日期与范围**：2026-11-20，issue #41。把确认画像后的 worker 成功路径从
`ALGORITHM_PREVIEW` 升级为确定性正式报告与 `V1_FORMAL` 自动发布；不接报告
LLM、不新增 golden set/EvalRun，不改检索器、资格规则或第二业务场景。

### 行为与实现

- 新增内部确定性报告构造器：从不可变算法预览、权威资格 artifact 和
  `applicable_as_of` 生成候选身份、最终排名、概率、位次、样本年份和资格状态
  Claims。算法事实使用同 release 的 `DERIVED` EvidenceRef；必需说明使用
  release-bound `STRUCTURED` EvidenceRef。系统当前日期不参与事实规范化。
- worker 在画像确认并完成四个算法 RPC 后，构造 `EvidencePackage`，调用既有
  `VerifyPackage`，再通过 `PublishFormalResult` 原子发布结果、已剥离证据包、
  完整验证报告、ACTIVE 状态和 RESULT 事件。调用方与测试仍只从
  `SubmitRun → ResumeRun → GetRun` 观察成功或失败。
- 正式结果 payload 新增版本化 `m5b-report-v1` 报告：方案概况、逐候选说明、
  资格核验和风险提示。Orchestrator proto 以新字段加法返回同一报告；
  Python/GetRun/Watch/gRPC 读取共享同一内容。
- 缺失样本年份、非法报告输入、非完整资格覆盖、UNKNOWN/FAIL 组、缺失必需说明、
  证据类型/版本不一致均 fail closed。释放已提交但报告构造失败时进入
  `FAILED / VALIDATION_FAILED`，不暴露正式结果。
- 正式发布事务补充主动执行额度复核，避免算法调用耗尽 10 分钟后仍发布；
  WAITING_HITL 仍不计入主动执行时间。

### 验证

- 新增主 seam 测试覆盖确定性报告成功、RESULT/GetRun/gRPC 同内容、必需说明原文、
  证据覆盖率 100%、报告输入缺失稳定失败。
- 现有算法、资格、租约、取消、正式证据、迁移与传输测试升级为正式报告语义；
  `ORCHESTRATOR_TEST_DSN=...` 下全部 `test_run*.py` 通过。
- `make py-check py-test`、`make build check test` 通过；proto 通过统一
  `make api` 生成，未手改生成物。

### 边界与风险

- 本票只提供确定性叙事；受 schema 约束的 LLM 报告叙事、剥离/降级策略留给 #42。
- 文档证据与结构化招生数据的真实覆盖仍依赖既有检索/规则发布链路；本票没有把
  算法派生事实伪装为文档事实。
- `ALGORITHM_PREVIEW` 标识与存储能力为兼容保留，但已确认画像的正常成功路径现在
  发布 `V1_FORMAL`；需要预览的独立产品入口不在本票范围。

## 70. M5b-05 敏感资格数据的 consent 使用闸门

**日期与范围**：2026-11-20，issue #45。为已确认画像中的敏感资格字段增加
principal、目的、数据类别和 policy 版本绑定的 consent 检查；不实现删除、保留、
备份恢复或报告 LLM 叙事。

### 行为与实现

- 新增 `consent_record` 迁移，保存 principal、目的、数据类别、policy 版本、
  授予/撤销时间和监护人确认。敏感字段分类覆盖色觉/视力/健康、性别、户籍和学籍；
  只有 `PROVIDED/DERIVED` 且值存在时触发闸门。
- consent 查询按 `SENSITIVE_ELIGIBILITY_EVALUATION + SENSITIVE_ELIGIBILITY_DATA`
  精确匹配当前 policy 版本。存在有效记录才放行；当前版本已撤销返回
  `CONSENT_REVOKED`；缺失、目的不符、数据类别不符或 policy 过期返回
  `CONSENT_REQUIRED`。
- 检查发生在编译算法/资格预览前，并在已有预览准备进入报告时再次执行，防止同意在
  执行期间撤销后继续发布。失败沿用 RunLifecycle 稳定终态，不产生正式结果。
- PROFILE_CONFIRM/INTAKE_ASK 的持久业务事件对敏感字段值做确定性脱敏，仅保留字段
  状态和来源；测试同时检查事件、RESULT 和正式结果不包含色觉原始值。

### 验证

- 新增主 seam 测试覆盖无同意、撤销、目的不符、数据类别不符、policy 过期、有效
  同意和敏感值脱敏；成功路径仍从 `SubmitRun → ResumeRun → GetRun` 发布
  `V1_FORMAL`。
- 真实 PostgreSQL 下完整 `test_run*.py` 共 103 项通过、1 项因未配置真实算法地址
  跳过；`make py-check py-test` 通过。

### 边界与风险

- 本票只闭合 consent 使用闸门；删除、保留和恢复由后续 M5b tickets 完成。
- 报告 LLM 尚未接入；后续报告叙事必须只消费规则级脱敏结论，不能回读敏感画像值。

## 71. M5b-06 principal 删除即时封锁、tombstone 与资源 ledger

**日期与范围**：2026-11-20，issue #46。实现 `DeletePrincipalData` 控制面
adapter；不实现保留清理（#47）或备份恢复重放（#48），不新增公开业务 seam。

### 行为与安全边界

- 删除请求在可信 principal、绑定请求的一次性确认和稳定 `request_id` 通过后，
  先原子写入 `DELETION_PENDING`、最小 tombstone 与请求 ledger，再执行资源清理。
  同一 `request_id` 已完成时重放原 receipt；部分失败时保持 pending，后续重试
  继续清理且不重新消费确认。
- `RunLifecycle` 的 `SubmitRun`、`ResumeRun`、`GetRun`、`WatchRun`、`CancelRun`
  与 run-bound 检索 binding 在私有访问前检查 principal 状态。删除中返回
  `DELETION_IN_PROGRESS`，删除完成后返回 `PRINCIPAL_DELETED`，不会先泄露 run
  是否存在。
- 清理覆盖 Profile revision/confirmation、interrupt、event、命令 receipt、
  `plan_checkpoint`、官方 LangGraph checkpoint、正式结果、证据包、验证报告、
  资格 artifact 与状态事件。run/session 保留最小去标识化 stub 以维持调用账本
  引用；LLM logical call/预算/兼容投影去除 principal/session 关联、响应原文和
  错误文本。
- tombstone 只保存 principal 的 SHA-256 不可逆引用、request id、稳定原因码和
  时间；用户提交的自由文本 `reason` 与确认 token 不进入 tombstone/ledger。
- 正式结果默认仍不可改写；新迁移只在显式开启
  `app.principal_deletion=on` 的删除事务中允许受控 DELETE，防止以删除实现绕过
  ACTIVE/STALE 追加状态。

### 验证

- 测试从 `SubmitRun → GetRun` 观察删除前可读、删除中不可读、删除后不可见；
  同时验证已删除 principal 不能重新 `SubmitRun`。
- 删除已发布 `V1_FORMAL` 后，`run_result / run_evidence_package /
  run_verification_report / run_formal_gate_artifact` 均清零；公开 release
  保持存在。
- 覆盖幂等重放且确认只消费一次、部分失败保持 pending 后重试成功、无效确认
  稳定拒绝且保持原访问、Profile/task input/event/checkpoint 清理，以及敏感
  reason 不落入审计表。
- `ORCHESTRATOR_TEST_DSN=...` 下 #46 集成测试与完整真实 PostgreSQL 套件通过；
  `make py-check`、`make build check test` 通过。

### 边界与风险

- 本票只关闭删除请求与即时封锁；7/30/180 天定时保留策略由 #47 完成，
  备份恢复时 tombstone/ledger 重放由 #48 完成。
- Redis 投影未直接清理；现状没有按 principal 存储的权威 Redis 对象，最终恢复
  与投影重建仍须在 #48 验证。
- 确认签发与重新认证服务不在本票范围；控制面通过
  `DeletionConfirmationAuthority` 注入，生产未接线时不能放行删除。
