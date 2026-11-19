"""混合检索薄层：pgvector 向量 ‖ pg_trgm 关键词 → RRF 融合。

阶段一实现（见 docs/prd.md FR-08 与 RAG 分阶段演进策略）：
- 向量一路：pgvector HNSW，余弦距离（embedding 已归一化）；
- 关键词一路：pg_trgm GIN 索引加速的 ILIKE 子串匹配，similarity() 排序
  （tsvector 全文一路因 'simple' 分词对中文不可用，zhparser 待评测后决策）；
- 融合：RRF（Reciprocal Rank Fusion），score = Σ 1/(k + rank)；
- 硬过滤优先：province/year/school 等元数据经 meta @> 先过滤（WHERE），
  再进相似度排序——是过滤条件，不是排序因素（FR-08）。

约定：SQL 全参数化；两路各自带稳定 tie-break（ORDER BY ..., id），
融合结果同输入同输出（可复现纪律与 svc-algo 一致）。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

# RRF 标准平滑常数（Cormack et al., 2009 推荐值）
RRF_K = 60

# 元数据硬过滤字段 → meta JSONB 键（冗余写入策略，禁止 JOIN 主表过滤）
_META_FILTERS = {
    "school_name": "school_name",
    "year": "year",
}

_SELECT_COLUMNS = """
    id, doc_id, section_path, text,
    meta->>'school_name' AS school_name,
    (meta->>'year')::int AS year
"""

# 查询切词的分隔符：空白 + 常见中英文标点
_TERM_SPLIT = re.compile(r"[\s,，.。!！?？;；:：、\"'“”‘’()（）\[\]【】<>《》/\\|~·…—\-]+")

# 过短的 token 不参与关键词匹配（易误命中且 trgm 相似度无意义）
_MIN_TERM_LEN = 2


@dataclass
class RetrievedChunk:
    """一条检索结果（融合后）。"""

    id: str
    doc_id: str
    section_path: str
    text: str
    school_name: str | None = None
    year: int | None = None
    # 各路原始名次与分数（1-based；缺席为 None），供调试与引用审计
    vector_rank: int | None = None
    vector_distance: float | None = None
    keyword_rank: int | None = None
    keyword_sim: float | None = None
    # RRF 融合分
    rrf_score: float = 0.0
    meta: dict = field(default_factory=dict)


def extract_terms(query: str, max_terms: int = 4) -> list[str]:
    """确定性切词：按空白/标点切 query，保留长度 >= 2 的 token。

    不做 NLP 分词（那是 zhparser 阶段的事）；「东莞理工学院 转专业政策」
    → ["东莞理工学院", "转专业政策"]。不足两个字符的 token 不进入检索。
    """
    tokens = [t for t in _TERM_SPLIT.split(query.strip()) if len(t) >= _MIN_TERM_LEN]
    # 去重保序，截断到 max_terms（防 ILIKE 条件爆炸）
    seen: set[str] = set()
    terms: list[str] = []
    for t in tokens:
        if t not in seen:
            seen.add(t)
            terms.append(t)
        if len(terms) >= max_terms:
            break
    return terms


def rrf_fuse(
    vector_rows: list[dict],
    keyword_rows: list[dict],
    k: int = RRF_K,
) -> list[RetrievedChunk]:
    """RRF 融合两路结果。

    输入为两路各自的有序行列表（dict 需含 id/section_path/text 等列，
    向量路可带 distance，关键词路可带 sim）。输出按融合分降序，
    同分按 id 升序 tie-break（确定性）。
    """
    fused: dict[str, RetrievedChunk] = {}

    def _ensure(row: dict) -> RetrievedChunk:
        cid = row["id"]
        if cid not in fused:
            fused[cid] = RetrievedChunk(
                id=cid,
                doc_id=row.get("doc_id"),
                section_path=row.get("section_path"),
                text=row.get("text"),
                school_name=row.get("school_name"),
                year=row.get("year"),
            )
        return fused[cid]

    for rank, row in enumerate(vector_rows, start=1):
        chunk = _ensure(row)
        chunk.vector_rank = rank
        chunk.vector_distance = row.get("distance")
        chunk.rrf_score += 1.0 / (k + rank)

    for rank, row in enumerate(keyword_rows, start=1):
        chunk = _ensure(row)
        chunk.keyword_rank = rank
        chunk.keyword_sim = row.get("sim")
        chunk.rrf_score += 1.0 / (k + rank)

    return sorted(fused.values(), key=lambda c: (-c.rrf_score, c.id))


class HybridRetriever:
    """向量 ‖ 关键词双路混合检索器（压在 pgvector / pg_trgm 上的薄层）。"""

    def __init__(self, conn, embedder=None, rrf_k: int = RRF_K):
        """
        Args:
            conn: psycopg 连接
            embedder: Embedder 实例；仅走关键词路时可为 None（惰性使用）
            rrf_k: RRF 平滑常数
        """
        self._conn = conn
        self._embedder = embedder
        self._rrf_k = rrf_k

    # ---------- 向量一路 ----------

    def vector_search(
        self,
        query: str,
        top_k: int = 20,
        filters: dict | None = None,
    ) -> list[dict]:
        """pgvector 余弦距离检索（HNSW）。filters 为元数据硬过滤。"""
        if self._embedder is None:
            raise ValueError("vector_search 需要 Embedder（构造时未传入）")
        q_vec = self._embedder.encode_single(query)
        vec_str = "[" + ",".join(f"{v:.6f}" for v in q_vec.tolist()) + "]"

        where_sql, kwargs = self._build_filters(filters)
        kwargs["vec"] = vec_str
        kwargs["top_k"] = top_k

        sql = f"""
            SELECT {_SELECT_COLUMNS},
                   embedding <=> %(vec)s::vector AS distance
            FROM policy_chunk
            WHERE {where_sql}
            ORDER BY embedding <=> %(vec)s::vector, id
            LIMIT %(top_k)s
        """
        return self._query(sql, kwargs)

    # ---------- 关键词一路 ----------

    def keyword_search(
        self,
        query: str,
        top_k: int = 20,
        filters: dict | None = None,
        terms: list[str] | None = None,
    ) -> list[dict]:
        """pg_trgm 关键词检索：任一 term ILIKE 子串命中即召回，similarity 排序。

        依赖 idx_chunk_text_trgm（GIN gin_trgm_ops）加速 ILIKE。
        """
        if terms is None:
            terms = extract_terms(query)
        if not terms:
            return []

        where_sql, kwargs = self._build_filters(filters)
        # ILIKE 模式串转义（%\_ 与反斜杠本身）
        like_terms = [
            "%" + t.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_") + "%"
            for t in terms
        ]
        ilikes = " OR ".join(f"text ILIKE %(t{i})s" for i in range(len(like_terms)))
        for i, pat in enumerate(like_terms):
            kwargs[f"t{i}"] = pat
        # 相似度对拼接后的 terms 计算（多 term 时代表整体贴合度）
        kwargs["sim_q"] = " ".join(terms)
        kwargs["top_k"] = top_k

        sql = f"""
            SELECT {_SELECT_COLUMNS},
                   similarity(text, %(sim_q)s) AS sim
            FROM policy_chunk
            WHERE ({ilikes}) AND {where_sql}
            ORDER BY sim DESC, id
            LIMIT %(top_k)s
        """
        return self._query(sql, kwargs)

    # ---------- 融合 ----------

    def search(
        self,
        query: str,
        top_k: int = 5,
        pool_k: int = 20,
        filters: dict | None = None,
        mode: str = "hybrid",
    ) -> list[RetrievedChunk]:
        """混合检索入口。

        Args:
            query: 用户查询
            top_k: 融合后返回条数
            pool_k: 每一路的召回池大小（融合前）
            filters: 元数据硬过滤（school_name / year）
            mode: "hybrid"（双路 RRF）/ "vector"（单路）/ "keyword"（单路）
        """
        if mode == "vector":
            rows = self.vector_search(query, top_k=top_k, filters=filters)
            return [c for c in rrf_fuse(rows, [], self._rrf_k)][:top_k]
        if mode == "keyword":
            rows = self.keyword_search(query, top_k=top_k, filters=filters)
            return [c for c in rrf_fuse([], rows, self._rrf_k)][:top_k]
        if mode != "hybrid":
            raise ValueError(f"未知 mode: {mode}")

        vector_rows = self.vector_search(query, top_k=pool_k, filters=filters)
        keyword_rows = self.keyword_search(query, top_k=pool_k, filters=filters)
        return rrf_fuse(vector_rows, keyword_rows, self._rrf_k)[:top_k]

    # ---------- 内部 ----------

    @staticmethod
    def _build_filters(filters: dict | None) -> tuple[str, dict]:
        """构造 meta @> 硬过滤 WHERE 子句（GIN(meta) 索引可用）。

        注意：@> 要求 JSON 类型严格一致。活库 meta.year 全部为 JSON number
        （已实测），故 year 统一强转 int；school_name 为字符串原样透传。
        """
        kwargs: dict = {}
        conditions: list[str] = []
        for key, meta_key in _META_FILTERS.items():
            if filters and filters.get(key) is not None:
                value = filters[key]
                if key == "year":
                    value = int(value)
                conditions.append(f"meta @> %(f_{key})s::jsonb")
                kwargs[f"f_{key}"] = json.dumps({meta_key: value})
        where_sql = " AND ".join(conditions) if conditions else "TRUE"
        return where_sql, kwargs

    def _query(self, sql: str, kwargs: dict) -> list[dict]:
        from psycopg.rows import dict_row

        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, kwargs)
            return [dict(r) for r in cur.fetchall()]
