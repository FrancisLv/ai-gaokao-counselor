#!/usr/bin/env python3
"""运行版本化检索 golden set，并输出可复现 baseline。"""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import date, datetime, timezone
import hashlib
import json
import platform
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
ORCHESTRATOR = ROOT / "services" / "orchestrator"
sys.path.insert(0, str(ORCHESTRATOR))

from eval.retrieval_eval import (  # noqa: E402
    build_baseline,
    evaluate_retrieval,
    load_retrieval_dataset,
)
from rag.policy_retrieval import (  # noqa: E402
    DocumentApplicability,
    DocumentEvidence,
    EvidenceRef,
    PolicyRetrievalResult,
    RetrievePolicy,
)

DEFAULT_DATASET = (
    ROOT
    / "data"
    / "golden"
    / "retrieval-hunan-2025-v0.1.0-draft"
)
DEFAULT_OUTPUT = (
    ROOT
    / "data"
    / "golden"
    / "baselines"
    / "retrieval-hunan-2025-v0.1.0-draft.rerank-off.fixture.json"
)
SOURCE_FILES = (
    "services/orchestrator/eval/retrieval_eval.py",
    "services/orchestrator/rag/policy_retrieval.py",
    "services/orchestrator/rag/retrieve.py",
    "scripts/run_retrieval_eval.py",
)


class DeterministicGoldenFixture:
    """冻结边界替身；仅验证评测流水线，不代表真实检索质量。"""

    def retrieve(self, query):
        case = self._cases[query.query_text]
        if "no_evidence" in case["tags"]:
            return PolicyRetrievalResult(
                "NO_EVIDENCE",
                query.release_id,
            )
        evidence = tuple(
            self._evidence(case, chunk_id, query.release_id)
            for chunk_id in case["expected_chunk_ids"]
        )
        return PolicyRetrievalResult(
            "FOUND",
            query.release_id,
            evidence,
        )

    def __init__(self, cases):
        self._cases = {case["query"]: case for case in cases}

    @staticmethod
    def _evidence(case, chunk_id, release_id):
        scope = case["scope"]
        document_id = (
            case["expected_doc_ids"][0]
            if case["expected_doc_ids"]
            else "fixture-document"
        )
        region = (
            "全国"
            if "national_policy" in case["tags"]
            else scope["province"]
        )
        text = f"fixture evidence for {case['case_id']}"
        school_ids = case.get("school_ids", [])
        school_id = school_ids[0] if school_ids else None
        document = DocumentEvidence(
            doc_id=document_id,
            chunk_id=chunk_id,
            title=f"fixture:{case['case_id']}",
            document_type=(
                case.get("document_types", ["charter"])[0]
            ),
            issuer="deterministic fixture",
            issuer_type=(
                "university" if school_id else "provincial"
            ),
            issuer_id="fixture",
            school_id=school_id,
            source_uri="fixture://retrieval-golden",
            fetched_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
            applicability=DocumentApplicability(
                regions=(region,),
                categories=(scope["category"],),
                batches=(scope["batch"],),
                valid_from=date(2025, 1, 1),
                valid_to=date(2025, 12, 31),
            ),
            section_path="fixture",
            excerpt_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            char_start=0,
            char_end=len(text),
            excerpt=text,
            release_id=release_id,
            document_content_sha256="0" * 64,
            raw_storage_uri="fixture://retrieval-golden/raw",
            chunk_set_version="fixture-v1",
        )
        return EvidenceRef(
            evidence_id=f"fixture:{case['case_id']}:{chunk_id}",
            evidence_type="DOCUMENT",
            release_id=release_id,
            content_sha256=document.excerpt_sha256,
            created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
            producer_version="retrieval-eval-fixture-v1",
            document=document,
        )


def git_value(*args: str) -> str:
    return subprocess.check_output(
        ("git", "-C", str(ROOT), *args),
        text=True,
    ).strip()


def source_tree_sha256() -> str:
    digest = hashlib.sha256()
    for relative_path in SOURCE_FILES:
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update((ROOT / relative_path).read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def code_identity() -> dict:
    dirty = bool(
        git_value("status", "--porcelain", "--untracked-files=no")
    )
    return {
        "git_sha": git_value("rev-parse", "HEAD"),
        "worktree_dirty": dirty,
        "source_tree_sha256": source_tree_sha256(),
        "python_version": platform.python_version(),
        "platform": platform.platform(),
    }


def parse_args():
    parser = argparse.ArgumentParser(
        description="运行 RetrievePolicy 检索 golden set"
    )
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--mode",
        choices=("hybrid", "vector", "keyword"),
        default="hybrid",
    )
    parser.add_argument(
        "--rerank-enabled",
        action="store_true",
        help="记录 rerank 配置；本 ticket 只支持 false baseline",
    )
    parser.add_argument("--rerank-model", default=None)
    parser.add_argument("--as-of", default="2025-11-19")
    parser.add_argument("--embedding-model", default=None)
    parser.add_argument("--index-version", default=None)
    parser.add_argument("--release-id", default=None)
    parser.add_argument("--dsn", default=None)
    parser.add_argument(
        "--fixture",
        action="store_true",
        help="使用冻结确定性替身；不得据此宣称真实检索质量",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    dataset = load_retrieval_dataset(args.dataset)
    if args.release_id:
        dataset = replace(
            dataset,
            manifest={**dataset.manifest, "release_id": args.release_id},
        )

    if args.fixture:
        retriever = DeterministicGoldenFixture(dataset.cases)
        execution = "deterministic-contract-fixture"
    else:
        if not args.dsn:
            raise SystemExit("real retrieval requires --dsn")
        if not args.release_id:
            raise SystemExit("real retrieval requires --release-id")
        if not args.index_version:
            raise SystemExit("real retrieval requires --index-version")
        import psycopg

        embedder = None
        if args.mode in ("hybrid", "vector"):
            if not args.embedding_model:
                raise SystemExit(
                    "hybrid/vector retrieval requires --embedding-model"
                )
            from rag.embedder import Embedder

            embedder = Embedder(model_name=args.embedding_model)
        as_of = date.fromisoformat(args.as_of)
        conn = psycopg.connect(args.dsn)
        retriever = RetrievePolicy(
            conn,
            embedder=embedder,
            today=lambda: as_of,
        )
        execution = "retrieve-policy"

    try:
        report = evaluate_retrieval(
            dataset,
            retriever,
            mode=args.mode,
            rerank_enabled=args.rerank_enabled,
        )
    finally:
        if not args.fixture:
            conn.close()

    retrieval_config = {
        "mode": args.mode,
        "embedder": args.embedding_model or (
            "deterministic-fixture-v1" if args.fixture else "not-used"
        ),
        "rerank_enabled": args.rerank_enabled,
        "rerank_model": args.rerank_model,
        "rrf_k": 60,
        "index_version": args.index_version or (
            "fixture-index-v1" if args.fixture else None
        ),
        "as_of": args.as_of,
        "top_k": 20,
        "pool_k": 20,
        "execution": execution,
        "authoritative_quality": False if args.fixture else True,
    }
    baseline = build_baseline(
        report,
        dataset,
        code_identity=code_identity(),
        retrieval_config=retrieval_config,
        generated_at=datetime.now(timezone.utc).isoformat(),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.output.exists():
        raise SystemExit(f"refusing to overwrite baseline: {args.output}")
    args.output.write_text(
        json.dumps(baseline, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "eval_run_id": baseline["eval_run_id"],
                "gate_status": baseline["gate_status"],
                "output": str(args.output),
            },
            ensure_ascii=False,
        )
    )
    if baseline["gate_status"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
