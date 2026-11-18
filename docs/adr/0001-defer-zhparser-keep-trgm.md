# 0001 · 中文全文检索维持 pg_trgm，推迟 zhparser

- 状态：已接受（2026-11-18）
- 决策者：Francis
- 关联：`gap-analysis.md §6` 决策1、PRD 风险#2、审查 §9.2、`services/orchestrator/rag/retrieve.py`

## 背景

关键词检索路当前走 `pg_trgm` 的 ILIKE + similarity，检索器并未消费 schema 里的 tsvector 列（`to_tsvector('simple')` 对中文不可用）。中文整句查询经切词后退化为整句 ILIKE、近乎零命中，语义召回实际由 pgvector 向量路主导；trgm 只对校名 / 专业名这类短实体有效。

## 决策

V1 维持「pgvector 向量路主导 + pg_trgm 兜短实体关键词」的现状，**不引入 zhparser / pg_jieba / pg_bigm**。待 M4 建立 golden set、量化出关键词路确实是召回瓶颈后，再评估是否进入分词阶段。

## 理由 / 权衡

- zhparser 等需自定义 PG 镜像（PRD 风险#2 标注为易卡点），且存量 tsv 需全量重建；
- 没有 golden set 就无法量化分词带来的 recall 提升，此时投入属过早优化；
- 向量路已能主导语义召回，关键词路退化对整体召回影响有限。

## 被否决的备选

- 现在上 zhparser（自定义镜像 + tsvector 真分词 + 全量重建 tsv）；
- pg_bigm / pg_jieba（同样需自定义镜像，pg_bigm 非真分词）。

## 影响

- 检索层无需改动镜像与存量数据；
- 长尾中文查询的关键词路召回偏弱，由向量路兜底，属已知可接受边界；
- **失效条件**：M4 建 golden set 后若量化证明关键词路是瓶颈，须复评本决策。
