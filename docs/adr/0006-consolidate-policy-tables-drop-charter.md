# 0006 · 文档表收口到 policy_*，废弃 charter_*

- 状态：已接受（2026-11-18）
- 决策者：Francis
- 关联：`gap-analysis.md §6` 决策6、`migrations/00_schema.sql:272` vs `migrations/07_policy_chunk_trgm.sql:30`

## 背景

仓库存在两套文档表：`00_schema.sql` 定义 `charter_doc / charter_chunk`（含 vector(1024) + tsv + HNSW/GIN 索引），`07_policy_chunk_trgm.sql` 定义 `policy_doc / policy_chunk`（含 trgm）。经全仓 grep 核实：`ingest_charters.py` 写入、`qa_check.py` 与 `retrieve.py` 查询的**全是 `policy_*`**；`charter_*` 全仓无任何读写，是孤儿空表。

## 决策

收口到 **`policy_*`（活表）**，废弃 `charter_*`（孤儿空表）。零数据迁移：新增一个迁移把 `charter_doc / charter_chunk` 标注废弃（`COMMENT ON TABLE ... IS 'DEPRECATED: use policy_*'`）或 drop；检索 / ETL 一律走 `policy_chunk`，**禁止查 `charter_chunk`**（会查到空表）。按禁改清单，不改 `00_schema.sql` 历史定义，废弃通过新迁移表达。

## 理由 / 权衡

- `policy_*` 是唯一被写入和查询的活表，收口到它零迁移成本；
- 反向收口到 `charter_*` 需把 1024 维向量 + tsv + trgm 全量迁移并改所有脚本，等于重做向量库，无功能收益；
- 命名语义不足（内容是招生章程，policy 偏泛）用注释 / 视图别名弥补，不值得为此重做向量库。

## 被否决的备选

- 收口到 `charter_*` 并迁移数据（重做向量库，成本高无收益）；
- 建视图别名兼容、两套都留（双轨债务仍在，未来仍需收口）。

## 影响

- 新增一个迁移处理 `charter_*` 废弃；
- 所有检索 / ETL / 质检脚本确认走 `policy_*`（现状已是）；
- 术语以 `GLOSSARY.md` 为准：活表 `policy_chunk`，避免 `charter_chunk`。
