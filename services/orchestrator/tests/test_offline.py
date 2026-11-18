"""已有 Python 逻辑离线回归：不加载真实模型、不访问数据库。"""
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

import numpy as np
from rag.etl import (DocMeta, MAX_CHUNK_LEN, _split_long_text, _merge_short_articles,
                     split_charter, process_single_file, iter_charter_files)
from rag.embedder import Embedder


class ETLTests(unittest.TestCase):
    def test_plain_text_and_preamble_preserved(self):
        meta = DocMeta(school_name="测试大学", year=2025, url="https://example.invalid")
        for text in ("没有章节标记的普通正文。", "第一章 总则\n前言一。\n前言二。\n第一条 正文。"):
            chunks = split_charter(text, meta)
            self.assertTrue(chunks)
            joined = "".join(c.text for c in chunks)
            for part in text.splitlines()[1:] if "第一章" in text else [text]:
                self.assertIn(part.replace("第一条 ", ""), joined)
            for c in chunks:
                self.assertEqual(c.meta["school_name"], meta.school_name)
                self.assertEqual(c.meta["year"], 2025)
                self.assertEqual(c.meta["url"], meta.url)

    def test_long_sentence_and_prefix_limits(self):
        for text in ("字" * 1801, "甲" * 601 + "。" + "乙" * 601, "甲。" * 800):
            chunks = _split_long_text(text)
            self.assertEqual("".join(chunks), text)
            self.assertTrue(all(0 < len(c) <= MAX_CHUNK_LEN for c in chunks))
        chunks = split_charter("第一章 总则\n" + "概述" * 400 + "\n第一条 " + "正文" * 800, DocMeta())
        self.assertTrue(all(len(c.text) <= MAX_CHUNK_LEN for c in chunks))

    def test_merge_bound_and_section_path(self):
        articles = [("第一条", "甲" * 290), ("第二条", "乙" * 590), ("第三条", "丙" * 100), ("第四条", "丁" * 50)]
        chunks = _merge_short_articles(articles, "第一章")
        self.assertTrue(all(len(c.text) <= MAX_CHUNK_LEN for c in chunks))
        self.assertIn("第四条", chunks[-1].section_path)
        self.assertEqual("".join(c.text for c in chunks).replace("\n", ""), "".join(text for _, text in articles))

    def test_multiple_number_formats(self):
        for text in ("一、学校概况\n内容甲\n二、录取办法\n内容乙", "1. 学校概况\n内容甲\n2. 录取办法\n内容乙"):
            chunks = split_charter(text, DocMeta())
            self.assertTrue(chunks)
            self.assertIn("内容甲", "".join(c.text for c in chunks))
            self.assertIn("内容乙", "".join(c.text for c in chunks))
            self.assertIn("录取办法", chunks[-1].section_path)
        self.assertEqual(split_charter("", DocMeta()), [])

    def test_numbered_heading_only_and_nested_body(self):
        for text in ("一、单行正文甲\n二、单行正文乙", "1. 单行正文甲\n2. 单行正文乙"):
            joined = "".join(c.text for c in split_charter(text, DocMeta()))
            self.assertIn("单行正文甲", joined)
            self.assertIn("单行正文乙", joined)
        chunks = split_charter("第一章 总则\n第一条 正文\n一、子项甲\n1. 子项乙\n3.14万元", DocMeta())
        joined = "".join(c.text for c in chunks)
        for part in ("一、子项甲", "1. 子项乙", "3.14万元"):
            self.assertIn(part, joined)
        self.assertTrue(all("第一条" in c.section_path for c in chunks))

    def test_split_lossless_and_invalid_limit(self):
        text = "甲\n\n 乙。" * 500
        for limit in (1, 17, MAX_CHUNK_LEN):
            chunks = _split_long_text(text, limit)
            self.assertEqual("".join(chunks), text)
            self.assertTrue(all(0 < len(c) <= limit for c in chunks))
        for limit in (0, -1):
            with self.assertRaises(ValueError):
                _split_long_text("正文", limit)
            with self.assertRaises(ValueError):
                _merge_short_articles([], "第一章", limit)

    def test_local_corpus_chunk_invariants(self):
        # 容器目录比源码树浅，不用固定下标访问 parents；语料可只读挂载。
        explicit_root = os.getenv("CHARTER_CORPUS_ROOT")
        root = Path(explicit_root or (Path(__file__).resolve().parent / "../../../docs/charters/2025")).resolve()
        if explicit_root:
            self.assertTrue(root.is_dir(), "显式指定的章程语料目录不存在")
        elif not root.is_dir():
            self.skipTest("本地章程语料未提供；合成夹具仍覆盖全部逻辑")
        pairs = list(iter_charter_files(root))
        self.assertTrue(pairs)
        for pair in pairs:
            with self.subTest(school=pair[0].stem):
                meta, chunks = process_single_file(*pair)
                self.assertTrue(chunks)
                self.assertTrue(all(c.text.strip() and c.section_path and len(c.text) <= MAX_CHUNK_LEN for c in chunks))
                self.assertTrue(all(c.meta["school_name"] == meta.school_name for c in chunks))

    def test_file_metadata_and_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("b", "a", "_index", "orphan"):
                (root / (name + ".json")).write_text(json.dumps({"school_name": "测试大学", "school_id": 123, "year": 2024, "url": "https://example.invalid"}), encoding="utf-8")
                if name != "orphan":
                    (root / (name + ".txt")).write_text("头部\n====\n第一章 总则\n第一条 正文", encoding="utf-8")
            pairs = list(iter_charter_files(root))
            self.assertEqual([j.stem for j, _ in pairs], ["a", "b"])
            meta, chunks = process_single_file(*pairs[0])
            self.assertEqual(meta.school_id, "123")
            self.assertTrue(chunks)
            self.assertNotIn("头部", chunks[0].text)
            self.assertEqual(chunks[0].meta["year"], 2024)


class EmbeddingTests(unittest.TestCase):
    def make_embedder(self, dim=1024):
        class FakeModel:
            def __init__(self, *args, **kwargs):
                pass

            def get_sentence_embedding_dimension(self):
                return dim

            def encode(self, texts, **kwargs):
                assert texts, "空输入不应调用模型"
                assert kwargs["normalize_embeddings"] is True
                return np.ones((len(texts), dim), dtype=np.float64)

        with patch.dict(sys.modules, {"sentence_transformers": types.SimpleNamespace(SentenceTransformer=FakeModel)}):
            return Embedder("offline-test")

    def test_shape_dtype_and_empty(self):
        embedder = self.make_embedder()
        self.assertEqual(embedder.dim, 1024)
        self.assertEqual(embedder.encode(["甲", "乙"]).shape, (2, 1024))
        self.assertEqual(embedder.encode_single("甲").dtype, np.float32)
        self.assertEqual(embedder.encode([]).shape, (0, 1024))

    def test_dimension_mismatch_fails_fast(self):
        with self.assertRaises(ValueError):
            self.make_embedder(dim=3)


class ImportAndGraphTests(unittest.TestCase):
    def test_all_runtime_imports(self):
        for name in ("app.main", "app.server", "graph.build", "graph.state", "rag.etl", "rag.embedder", "rag.retrieve"):
            importlib.import_module(name)
        for service in ("algo", "gateway", "llmgw", "orchestrator"):
            importlib.import_module(f"api.{service}.v1.{service}_pb2_grpc")

    def test_existing_graph_routes_only(self):
        from graph.build import route_after_intake, MAX_ASK_ROUNDS, build_graph
        self.assertEqual(route_after_intake({"missing": []}), "confirm")
        self.assertEqual(route_after_intake({"missing": ["score"], "ask_round": 0}), "ask")
        self.assertEqual(route_after_intake({"missing": ["score"], "ask_round": MAX_ASK_ROUNDS}), "confirm")
        self.assertIsNotNone(build_graph())


class HybridRetrievalTests(unittest.TestCase):
    """混合检索薄层（rag.retrieve）离线回归：不加载模型、不访问数据库。"""

    @staticmethod
    def _row(cid, **extra):
        return {"id": cid, "doc_id": f"doc_{cid}", "section_path": f"第一章 > {cid}",
                "text": f"正文{cid}", "school_name": "测试大学", "year": 2025, **extra}

    def test_extract_terms(self):
        from rag.retrieve import extract_terms
        self.assertEqual(extract_terms("东莞理工学院 转专业政策"), ["东莞理工学院", "转专业政策"])
        # 短 token 过滤、去重保序、max_terms 截断
        self.assertEqual(extract_terms("a 清华 清华 北大 复旦 交大"), ["清华", "北大", "复旦", "交大"])
        self.assertEqual(extract_terms("清华，转专业；大类分流"), ["清华", "转专业", "大类分流"])
        self.assertEqual(extract_terms("甲"), ["甲"])      # 无有效 token → 回退整句
        self.assertEqual(extract_terms("  色弱  "), ["色弱"])
        self.assertEqual(extract_terms("", max_terms=4), [])

    def test_rrf_fuse_score_order_and_tiebreak(self):
        from rag.retrieve import rrf_fuse
        vec = [self._row("a", distance=0.1), self._row("b", distance=0.2)]
        kw = [self._row("b", sim=0.9), self._row("c", sim=0.5)]
        fused = rrf_fuse(vec, kw, k=60)
        # b 双路命中分最高；a/c 单路同分按 id tie-break
        self.assertEqual([c.id for c in fused], ["b", "a", "c"])
        self.assertAlmostEqual(fused[0].rrf_score, 1 / 62 + 1 / 61, places=12)  # 向量rank2 + 关键词rank1
        self.assertEqual(fused[1].vector_rank, 1)
        self.assertIsNone(fused[1].keyword_rank)
        self.assertEqual(fused[2].keyword_rank, 2)
        self.assertAlmostEqual(fused[2].rrf_score, 1 / 62, places=12)

    def test_rrf_fuse_deterministic_on_ties(self):
        from rag.retrieve import rrf_fuse
        rows_a = [self._row("x"), self._row("y")]
        rows_b = [self._row("y"), self._row("x")]
        first = [c.id for c in rrf_fuse(rows_a, rows_b)]
        second = [c.id for c in rrf_fuse(rows_a, rows_b)]
        self.assertEqual(first, second)          # 同输入同输出
        self.assertEqual(first, ["x", "y"])      # 全员同分 → id 升序

    def test_rrf_fuse_empty_inputs(self):
        from rag.retrieve import rrf_fuse
        self.assertEqual(rrf_fuse([], []), [])
        only_kw = rrf_fuse([], [self._row("k1")])
        self.assertEqual([c.id for c in only_kw], ["k1"])

    def test_search_modes_and_fusion(self):
        from rag.retrieve import HybridRetriever
        retriever = HybridRetriever(conn=None, embedder=None)
        vec_rows = [self._row("v1", distance=0.1), self._row("both", distance=0.2)]
        kw_rows = [self._row("both", sim=0.8), self._row("k1", sim=0.4)]
        with patch.object(HybridRetriever, "vector_search", return_value=vec_rows) as vs, \
             patch.object(HybridRetriever, "keyword_search", return_value=kw_rows) as ks:
            hybrid = retriever.search("查询", top_k=3, mode="hybrid")
            # both 双路命中最高；v1 向量路 rank1（1/61）> k1 关键词路 rank2（1/62）
            self.assertEqual([c.id for c in hybrid], ["both", "v1", "k1"])
            self.assertEqual(vs.call_count, 1)
            self.assertEqual(ks.call_count, 1)
            # 单路模式只调对应一路
            vs.reset_mock(); ks.reset_mock()
            vec_only = retriever.search("查询", top_k=3, mode="vector")
            self.assertEqual([c.id for c in vec_only], ["v1", "both"])
            self.assertEqual(ks.call_count, 0)
            kw_only = retriever.search("查询", top_k=3, mode="keyword")
            self.assertEqual([c.id for c in kw_only], ["both", "k1"])
        with self.assertRaises(ValueError):
            retriever.search("查询", mode="unknown")

    def test_keyword_sql_escaping_and_filters(self):
        from rag.retrieve import HybridRetriever

        class FakeCursor:
            def __init__(self):
                self.sql = None
                self.params = None

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def execute(self, sql, params):
                self.sql, self.params = sql, params

            def fetchall(self):
                return []

        class FakeConn:
            def __init__(self):
                self.cursor_obj = FakeCursor()

            def cursor(self, row_factory=None):
                return self.cursor_obj

        conn = FakeConn()
        retriever = HybridRetriever(conn=conn, embedder=None)
        # 无 Embedder 时向量路必须显式报错，而非静默
        with self.assertRaises(ValueError):
            retriever.vector_search("查询")
        # 关键词路：特殊字符转义 + 硬过滤参数
        retriever.keyword_search("100% 转专业", filters={"school_name": "测试大学", "year": "2025"})
        sql, params = conn.cursor_obj.sql, conn.cursor_obj.params
        self.assertIn("ILIKE", sql)
        self.assertIn("similarity(text,", sql)
        self.assertEqual(params["t0"], r"%100\%%")          # % 被转义，前后加通配
        self.assertEqual(params["t1"], "%转专业%")
        self.assertIn("meta @> %(f_school_name)s::jsonb", sql)
        # year 字符串入参也强转 JSON number（活库 meta.year 为 number）
        self.assertEqual(json.loads(params["f_year"]), {"year": 2025})
        self.assertEqual(json.loads(params["f_school_name"]), {"school_name": "测试大学"})
        # _ 转义；反斜杠作为词分隔符（查询中的路径类字符不进入 ILIKE 模式）
        retriever.keyword_search("a_b 大类分流")
        self.assertEqual(conn.cursor_obj.params["t0"], r"%a\_b%")
        # 无 terms → 不发 SQL 直接空
        self.assertEqual(retriever.keyword_search("甲"), [])


if __name__ == "__main__":
    unittest.main()
