"""真实 TCP 四 RPC 与可选负载验收；由 Go 夹具启动独立服务，不调用模型。"""
import concurrent.futures
import json
import math
import os
import sys
import time

import grpc
from google.rpc import error_details_pb2, status_pb2
from api.algo.v1 import algo_pb2 as pb, algo_pb2_grpc


def expect_error(call, request, code, reason):
    try:
        call(request, timeout=10)
    except grpc.RpcError as exc:
        assert exc.code() == code, exc
        raw = dict(exc.trailing_metadata()).get("grpc-status-details-bin")
        detail = status_pb2.Status.FromString(raw)
        reasons = []
        for item in detail.details:
            info = error_details_pb2.ErrorInfo()
            if item.Unpack(info):
                reasons.append(info.reason)
        assert reason in reasons, reasons
    else:
        raise AssertionError("错误输入意外成功")


def run(addr):
    with grpc.insecure_channel(addr) as channel:
        grpc.channel_ready_future(channel).result(timeout=10)
        client = algo_pb2_grpc.AlgoStub(channel)
        req = pb.EstimateProbRequest(province="湖南", category="物理类", year="2025", score=600, subjects=["物理", "化学", "生物"])
        rank = client.RankConvert(pb.RankConvertRequest(province="湖南", category="物理类", year="2025", score=600), timeout=10)
        assert rank.rank == 15860
        estimate = client.EstimateProb(req, timeout=10)
        assert estimate.basis.user_rank == rank.rank
        assert list(estimate.basis.sample_years) == [2022, 2023, 2024]
        assert estimate.basis.method == "weighted_hit"
        all_candidates = list(estimate.rush_candidates) + list(estimate.stable_candidates) + list(estimate.safe_candidates)
        assert len(all_candidates) == estimate.rush_total + estimate.stable_total + estimate.safe_total > 0
        keys = set()
        for c in all_candidates:
            key = (c.candidate.university_code, c.candidate.major_group)
            assert key not in keys
            keys.add(key)
            assert math.isfinite(c.prob.probability) and .01 <= c.prob.probability <= .99
            assert len(c.candidate.history_data) == c.prob.data_years > 0
            for h in c.candidate.history_data:
                assert 2022 <= h.year <= 2024
                assert h.total_count > 0 and h.min_percentile == h.group_min_rank / h.total_count
        checked = client.CheckConstraints(pb.CheckConstraintsRequest(candidates=all_candidates, subjects=req.subjects, category=req.category), timeout=10)
        assert list(checked.kept) == all_candidates and not checked.rejected
        wishlist = client.OptimizeWishlist(pb.OptimizeWishlistRequest(candidates=checked.kept), timeout=10).wishlist
        assert len(wishlist.candidates) == 12 and wishlist.report.safe_guard_ok
        for i, c in enumerate(wishlist.candidates):
            assert c.final_rank == i + 1
            assert set(c.utility.weights) == {"tier", "major", "location", "strength"}
            assert c.cwp.candidate.university_code
        expect_error(client.RankConvert, pb.RankConvertRequest(province="湖南", category="物理类", year="2025", score=404), grpc.StatusCode.FAILED_PRECONDITION, "SCORE_BELOW_CONTROL_LINE")
        expect_error(client.OptimizeWishlist, pb.OptimizeWishlistRequest(candidates=[pb.CandidateWithProb(candidate=pb.Candidate(university_code="A", major_group="1"))]), grpc.StatusCode.FAILED_PRECONDITION, "CANDIDATE_WITHOUT_PROB")
        expect_error(client.OptimizeWishlist, pb.OptimizeWishlistRequest(candidates=[all_candidates[0], all_candidates[0]]), grpc.StatusCode.INVALID_ARGUMENT, "DUPLICATE_CANDIDATE")
        print(json.dumps({"python_four_rpc": "通过", "candidates": len(all_candidates), "response_bytes": estimate.ByteSize()}, ensure_ascii=False))
        if os.getenv("ALGO_LOAD") == "1":
            def request(_):
                started = time.perf_counter()
                try:
                    reply = client.EstimateProb(req, timeout=30)
                    assert reply == estimate, "并发响应与固定基准不同"
                    return (time.perf_counter() - started) * 1000, 0, reply.ByteSize()
                except Exception:
                    return (time.perf_counter() - started) * 1000, 1, 0

            for concurrency in (1, 8):
                with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
                    results = list(executor.map(request, range(100)))
                latencies = sorted(r[0] for r in results)
                errors = sum(r[1] for r in results)
                print(json.dumps({"concurrency": concurrency, "requests": 100, "p50_ms": round(latencies[49], 3), "p95_ms": round(latencies[94], 3), "max_ms": round(latencies[-1], 3), "error_rate": errors / 100, "response_bytes": results[0][2]}))
                assert errors == 0
                if concurrency == 1:
                    assert latencies[-1] < 2000, "单并发请求超过2秒"


if __name__ == "__main__":
    run(sys.argv[1])
