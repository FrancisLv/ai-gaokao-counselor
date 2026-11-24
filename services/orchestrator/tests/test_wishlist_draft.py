"""用户拟填专业草稿：版本、幂等、所有权和资格字段校验。"""
from __future__ import annotations

import asyncio
from uuid import UUID

import grpc
from api.algo.v1 import algo_pb2

from run_lifecycle_support import RunLifecyclePostgresTest
from test_run_lifecycle_algorithm_preview import FakeAlgoClient


class MultiGroupFakeAlgoClient(FakeAlgoClient):
    def __init__(self):
        super().__init__()
        self.second = algo_pb2.CandidateWithProb()
        self.second.CopyFrom(self.candidate)
        self.second.candidate.university_code = "U002"
        self.second.candidate.university_name = "第二大学"
        self.second.candidate.major_group = "02"
        self.second.candidate.majors[0].major_code = "M02"
        self.second.candidate.majors[0].major_name = "第二专业"
        self.second.prob.probability = 0.85
        self.second.prob.category = "稳"

    async def EstimateProb(self, request):
        self.calls.append(("EstimateProb", request))
        return algo_pb2.EstimateProbReply(
            stable_candidates=[self.candidate, self.second],
            stable_total=2,
            basis=algo_pb2.AuditBasis(
                user_rank=15860,
                user_percentile=0.1234,
                sample_years=[2022, 2023, 2024],
                method="weighted_hit",
                province="湖南",
                category="物理类",
            ),
        )

    async def EvaluateEligibility(self, request):
        self.calls.append(("EvaluateEligibility", request))
        return algo_pb2.EvaluateEligibilityReply(
            status=(
                algo_pb2.EligibilityResultStatus
                .ELIGIBILITY_RESULT_STATUS_COMPLETE
            ),
            groups=[
                algo_pb2.GroupEligibility(
                    group_key=(
                        f"{candidate.university_code}|"
                        f"{candidate.major_group}"
                    ),
                    status=(
                        algo_pb2.GroupEligibilityStatus
                        .GROUP_ELIGIBILITY_STATUS_PASS
                    ),
                    majors=[
                        algo_pb2.MajorEligibility(
                            major_code=major.major_code,
                            status=(
                                algo_pb2.MajorEligibilityStatus
                                .MAJOR_ELIGIBILITY_STATUS_PASS
                            ),
                        )
                        for major in candidate.majors
                    ],
                )
                for candidate in request.candidate_groups
            ],
            coverage=[
                algo_pb2.EligibilityRuleCoverage(
                    rule_type="SUBJECT_REQUIREMENT",
                    status=(
                        algo_pb2.RuleCoverageStatus
                        .RULE_COVERAGE_STATUS_FULL
                    ),
                    covered_data_sources=["test admissions plan"],
                )
            ],
            evidence_bundle_id="eligibility:test",
            diagnostics=algo_pb2.EligibilityDiagnostics(
                evaluated_rule_count=1,
                unknown_count=0,
            ),
        )

    async def OptimizeWishlist(self, request):
        self.calls.append(("OptimizeWishlist", request))
        return algo_pb2.OptimizeWishlistReply(
            wishlist=algo_pb2.WishlistResult(
                candidates=[
                    algo_pb2.CandidateWithScore(
                        cwp=self.candidate,
                        utility=algo_pb2.UtilityScore(score=0.8),
                        final_rank=1,
                    ),
                    algo_pb2.CandidateWithScore(
                        cwp=self.second,
                        utility=algo_pb2.UtilityScore(score=0.7),
                        final_rank=2,
                    ),
                ],
                report=algo_pb2.OptimizationReport(
                    stable_count=2,
                    safe_guard_ok=False,
                ),
            )
        )


class WishlistDraftLifecycleTests(RunLifecyclePostgresTest):
    async def _wait_for_interrupt(
        self,
        lifecycle,
        run_id,
        *,
        principal_id: str,
    ):
        for _ in range(300):
            view = await lifecycle.GetRun(
                principal_id=principal_id,
                run_id=run_id,
            )
            if view.state == "WAITING_HITL" and view.current_interrupt is not None:
                return view
            await asyncio.sleep(0.01)
        self.fail("run did not expose a profile interrupt")

    async def _wait_for_terminal(
        self,
        lifecycle,
        run_id,
        *,
        principal_id: str,
    ):
        for _ in range(300):
            view = await lifecycle.GetRun(
                principal_id=principal_id,
                run_id=run_id,
            )
            if view.state in {
                "EXPIRED",
                "SUCCEEDED",
                "FAILED",
                "CANCELLED",
            }:
                return view
            await asyncio.sleep(0.01)
        self.fail("run did not reach a terminal state")

    async def _publish_formal(self, lifecycle, *, principal: str):
        from app.run_lifecycle import (
            ConfirmProfileAnswer,
            ResumeRunCommand,
            StructuredProfileInput,
            SubmitRunCommand,
        )
        from app.run_lifecycle.worker import RunLifecycleWorker

        worker = RunLifecycleWorker(lifecycle, poll_interval=0.01)
        await worker.start()
        self.addAsyncCleanup(worker.stop)
        submitted = await lifecycle.SubmitRun(
            SubmitRunCommand(
                principal_id=principal,
                session_id=None,
                task_kind="GENERATE_PLAN",
                input=StructuredProfileInput(
                    province="湖南",
                    application_year=2025,
                    category="物理类",
                    batch="本科批",
                    score=600,
                    subjects=("物理", "化学", "生物"),
                ),
                idempotency_key=f"draft-{principal}-submit",
            )
        )
        waiting = await self._wait_for_interrupt(
            lifecycle,
            submitted.run_id,
            principal_id=principal,
        )
        await lifecycle.ResumeRun(
            ResumeRunCommand(
                principal_id=principal,
                run_id=submitted.run_id,
                interrupt_id=waiting.current_interrupt.interrupt_id,
                expected_state_version=waiting.state_version,
                profile_version=UUID(
                    waiting.current_interrupt.payload["profile_version"]
                ),
                answer=ConfirmProfileAnswer(),
                idempotency_key=f"draft-{principal}-confirm",
            )
        )
        completed = await self._wait_for_terminal(
            lifecycle,
            submitted.run_id,
            principal_id=principal,
        )
        self.assertEqual(completed.state, "SUCCEEDED")
        return completed

    async def test_draft_versions_replay_and_conflicts(self):
        from app.run_lifecycle import (
            RunLifecycle,
            RunLifecycleError,
            WishlistDraftGroup,
        )

        await self.seed_release()
        algo = FakeAlgoClient()
        lifecycle = RunLifecycle(
            self.dsn,
            auto_profile=True,
            worker_owner="worker-wishlist-draft",
            algo_client=algo,
        )
        self.addAsyncCleanup(lifecycle.close)
        completed = await self._publish_formal(
            lifecycle,
            principal="principal-draft",
        )
        result_version = completed.result.result_version
        groups = (
            WishlistDraftGroup(
                university_code="U001",
                major_group="01",
                major_codes=("M01",),
            ),
        )

        first = await lifecycle.SaveWishlistDraft(
            principal_id="principal-draft",
            result_version=result_version,
            expected_version=0,
            idempotency_key="draft-save-1",
            groups=groups,
        )
        self.assertEqual(first.draft_version, 1)
        self.assertEqual(first.groups, groups)
        conn = await self._connect()
        try:
            stored = await (
                await conn.execute(
                    """
                    SELECT draft_version_id, idempotency_key, draft_version
                    FROM wishlist_draft_version
                    WHERE principal_id = %s AND result_version = %s
                    ORDER BY draft_version
                    """,
                    ("principal-draft", result_version),
                )
            ).fetchall()
        finally:
            await conn.close()
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["idempotency_key"], "draft-save-1")

        replay = await lifecycle.SaveWishlistDraft(
            principal_id="principal-draft",
            result_version=result_version,
            expected_version=0,
            idempotency_key="draft-save-1",
            groups=groups,
        )
        self.assertEqual(replay.draft_version_id, first.draft_version_id)
        self.assertEqual(
            sum(
                1
                for name, _ in algo.calls
                if name == "SummarizeWishlistDraft"
            ),
            1,
        )

        second = await lifecycle.SaveWishlistDraft(
            principal_id="principal-draft",
            result_version=result_version,
            expected_version=1,
            idempotency_key="draft-save-2",
            groups=groups,
        )
        self.assertEqual(second.draft_version, 2)
        restored = await lifecycle.GetWishlistDraft(
            principal_id="principal-draft",
            result_version=result_version,
        )
        self.assertEqual(restored, second)

        from api.orchestrator.v1 import orchestrator_pb2 as pb
        from api.orchestrator.v1 import orchestrator_pb2_grpc as pb_grpc
        from app.server import OrchestratorServicer

        server = grpc.aio.server()
        pb_grpc.add_OrchestratorServicer_to_server(
            OrchestratorServicer(lifecycle=lifecycle),
            server,
        )
        port = server.add_insecure_port("127.0.0.1:0")
        await server.start()
        self.addAsyncCleanup(server.stop, None)
        channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
        self.addAsyncCleanup(channel.close)
        reply = await pb_grpc.OrchestratorStub(channel).GetWishlistDraft(
            pb.GetWishlistDraftRequest(result_version=str(result_version)),
            metadata=(("x-principal-id", "principal-draft"),),
        )
        self.assertEqual(reply.draft.draft_version, 2)
        self.assertEqual(list(reply.draft.groups[0].major_codes), ["M01"])
        self.assertEqual(reply.draft.risk.group_count, 1)
        self.assertEqual(reply.draft.risk.stable_count, 1)
        self.assertEqual(reply.draft.risk.safe_guard_target, 9)

        with self.assertRaises(RunLifecycleError) as stale:
            await lifecycle.SaveWishlistDraft(
                principal_id="principal-draft",
                result_version=result_version,
                expected_version=1,
                idempotency_key="draft-stale",
                groups=groups,
            )
        self.assertEqual(stale.exception.reason, "DRAFT_VERSION_CONFLICT")

    async def test_draft_accepts_subset_and_preserves_user_order(self):
        from app.run_lifecycle import RunLifecycle, WishlistDraftGroup

        await self.seed_release()
        lifecycle = RunLifecycle(
            self.dsn,
            auto_profile=True,
            worker_owner="worker-wishlist-subset",
            algo_client=MultiGroupFakeAlgoClient(),
        )
        self.addAsyncCleanup(lifecycle.close)
        completed = await self._publish_formal(
            lifecycle,
            principal="principal-wishlist-subset",
        )
        result_version = completed.result.result_version

        subset = await lifecycle.SaveWishlistDraft(
            principal_id="principal-wishlist-subset",
            result_version=result_version,
            expected_version=0,
            idempotency_key="subset-v1",
            groups=(
                WishlistDraftGroup(
                    university_code="U001",
                    major_group="01",
                    major_codes=("M01",),
                ),
            ),
        )
        self.assertEqual(len(subset.groups), 1)
        self.assertIsNotNone(subset.risk)
        self.assertEqual(subset.risk.group_count, 1)
        self.assertEqual(subset.risk.stable_count, 1)
        self.assertAlmostEqual(subset.risk.slip_rate, 1 - 0.67, places=6)
        self.assertAlmostEqual(
            subset.risk.expected_satisfaction or 0,
            0.8,
            places=6,
        )

        ordered = await lifecycle.SaveWishlistDraft(
            principal_id="principal-wishlist-subset",
            result_version=result_version,
            expected_version=1,
            idempotency_key="subset-v2",
            groups=(
                WishlistDraftGroup(
                    university_code="U002",
                    major_group="02",
                    major_codes=("M02",),
                ),
                WishlistDraftGroup(
                    university_code="U001",
                    major_group="01",
                    major_codes=("M01",),
                ),
            ),
        )
        self.assertEqual(
            [
                (group.university_code, group.major_group)
                for group in ordered.groups
            ],
            [("U002", "02"), ("U001", "01")],
        )
        self.assertEqual(ordered.risk.group_count, 2)
        self.assertEqual(ordered.risk.stable_count, 2)

    async def test_draft_rejects_unknown_or_unselectable_major(self):
        from app.run_lifecycle import (
            RunLifecycle,
            RunLifecycleError,
            WishlistDraftGroup,
        )

        await self.seed_release()
        lifecycle = RunLifecycle(
            self.dsn,
            auto_profile=True,
            worker_owner="worker-wishlist-draft-invalid",
            algo_client=FakeAlgoClient(),
        )
        self.addAsyncCleanup(lifecycle.close)
        completed = await self._publish_formal(
            lifecycle,
            principal="principal-draft-invalid",
        )

        with self.assertRaises(RunLifecycleError) as invalid:
            await lifecycle.SaveWishlistDraft(
                principal_id="principal-draft-invalid",
                result_version=completed.result.result_version,
                expected_version=0,
                idempotency_key="draft-invalid-major",
                groups=(
                    WishlistDraftGroup(
                        university_code="U001",
                        major_group="01",
                        major_codes=("NOT-IN-RESULT",),
                    ),
                ),
            )
        self.assertEqual(
            invalid.exception.reason,
            "DRAFT_SELECTION_INVALID",
        )

    async def test_draft_rejects_a_group_outside_the_formal_report(self):
        from app.run_lifecycle import (
            RunLifecycle,
            RunLifecycleError,
            WishlistDraftGroup,
        )

        await self.seed_release()
        lifecycle = RunLifecycle(
            self.dsn,
            auto_profile=True,
            worker_owner="worker-wishlist-unknown-group",
            algo_client=FakeAlgoClient(),
        )
        self.addAsyncCleanup(lifecycle.close)
        completed = await self._publish_formal(
            lifecycle,
            principal="principal-wishlist-unknown-group",
        )

        with self.assertRaises(RunLifecycleError) as invalid:
            await lifecycle.SaveWishlistDraft(
                principal_id="principal-wishlist-unknown-group",
                result_version=completed.result.result_version,
                expected_version=0,
                idempotency_key="draft-unknown-group",
                groups=(
                    WishlistDraftGroup(
                        university_code="U999",
                        major_group="99",
                        major_codes=("M01",),
                    ),
                ),
            )
        self.assertEqual(
            invalid.exception.reason,
            "DRAFT_SELECTION_INVALID",
        )

    async def test_draft_cannot_save_after_release_revoke(self):
        from app.run_lifecycle import (
            RunLifecycle,
            RunLifecycleError,
            WishlistDraftGroup,
        )

        release_id = await self.seed_release()
        lifecycle = RunLifecycle(
            self.dsn,
            auto_profile=True,
            worker_owner="worker-wishlist-draft-revoked",
            algo_client=FakeAlgoClient(),
        )
        self.addAsyncCleanup(lifecycle.close)
        completed = await self._publish_formal(
            lifecycle,
            principal="principal-draft-revoked",
        )
        conn = await self._connect()
        try:
            await conn.execute(
                "UPDATE release SET status = 'REVOKED' WHERE release_id = %s",
                (release_id,),
            )
        finally:
            await conn.close()

        with self.assertRaises(RunLifecycleError) as revoked:
            await lifecycle.SaveWishlistDraft(
                principal_id="principal-draft-revoked",
                result_version=completed.result.result_version,
                expected_version=0,
                idempotency_key="draft-revoked",
                groups=(
                    WishlistDraftGroup(
                        university_code="U001",
                        major_group="01",
                        major_codes=("M01",),
                    ),
                ),
            )
        self.assertEqual(revoked.exception.reason, "RELEASE_REVOKED")

    async def test_list_wishlists_returns_latest_version_only_for_owner(self):
        from app.run_lifecycle import (
            RunLifecycle,
            WishlistDraftGroup,
        )

        await self.seed_release()
        lifecycle = RunLifecycle(
            self.dsn,
            auto_profile=True,
            worker_owner="worker-wishlist-list",
            algo_client=FakeAlgoClient(),
        )
        self.addAsyncCleanup(lifecycle.close)
        completed = await self._publish_formal(
            lifecycle,
            principal="principal-wishlist-owner",
        )
        result_version = completed.result.result_version
        groups = (
            WishlistDraftGroup(
                university_code="U001",
                major_group="01",
                major_codes=("M01",),
            ),
        )
        await lifecycle.SaveWishlistDraft(
            principal_id="principal-wishlist-owner",
            result_version=result_version,
            expected_version=0,
            idempotency_key="wishlist-list-v1",
            groups=groups,
        )
        await lifecycle.SaveWishlistDraft(
            principal_id="principal-wishlist-owner",
            result_version=result_version,
            expected_version=1,
            idempotency_key="wishlist-list-v2",
            groups=groups,
        )

        from api.orchestrator.v1 import orchestrator_pb2 as pb
        from api.orchestrator.v1 import orchestrator_pb2_grpc as pb_grpc
        from app.server import OrchestratorServicer

        server = grpc.aio.server()
        pb_grpc.add_OrchestratorServicer_to_server(
            OrchestratorServicer(lifecycle=lifecycle),
            server,
        )
        port = server.add_insecure_port("127.0.0.1:0")
        await server.start()
        self.addAsyncCleanup(server.stop, None)
        channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
        self.addAsyncCleanup(channel.close)
        stub = pb_grpc.OrchestratorStub(channel)

        reply = await stub.ListWishlists(
            pb.ListWishlistsRequest(limit=20),
            metadata=(("x-principal-id", "principal-wishlist-owner"),),
        )
        self.assertEqual(len(reply.wishlists), 1)
        item = reply.wishlists[0]
        self.assertEqual(item.result_version, str(result_version))
        self.assertEqual(item.draft_version, 2)
        self.assertEqual(item.status, "CURRENT")
        self.assertEqual(item.group_count, 1)
        self.assertEqual(item.major_count, 1)

        other = await stub.ListWishlists(
            pb.ListWishlistsRequest(limit=20),
            metadata=(("x-principal-id", "principal-other"),),
        )
        self.assertEqual(len(other.wishlists), 0)

    async def test_wishlist_history_restore_creates_new_version(self):
        from app.run_lifecycle import (
            RunLifecycle,
            WishlistDraftGroup,
        )

        await self.seed_release()
        lifecycle = RunLifecycle(
            self.dsn,
            auto_profile=True,
            worker_owner="worker-wishlist-restore",
            algo_client=FakeAlgoClient(),
        )
        self.addAsyncCleanup(lifecycle.close)
        completed = await self._publish_formal(
            lifecycle,
            principal="principal-wishlist-restore",
        )
        result_version = completed.result.result_version
        groups = (
            WishlistDraftGroup(
                university_code="U001",
                major_group="01",
                major_codes=("M01",),
            ),
        )
        first = await lifecycle.SaveWishlistDraft(
            principal_id="principal-wishlist-restore",
            result_version=result_version,
            expected_version=0,
            idempotency_key="restore-v1",
            groups=groups,
        )
        await lifecycle.SaveWishlistDraft(
            principal_id="principal-wishlist-restore",
            result_version=result_version,
            expected_version=1,
            idempotency_key="restore-v2",
            groups=groups,
        )

        history = await lifecycle.ListWishlistHistory(
            principal_id="principal-wishlist-restore",
            result_version=result_version,
        )
        self.assertEqual([item.draft_version for item in history], [2, 1])

        restored = await lifecycle.RestoreWishlistDraftVersion(
            principal_id="principal-wishlist-restore",
            result_version=result_version,
            draft_version=first.draft_version,
            idempotency_key="restore-from-v1",
        )
        self.assertEqual(restored.draft_version, 3)
        self.assertEqual(restored.groups, first.groups)
        history = await lifecycle.ListWishlistHistory(
            principal_id="principal-wishlist-restore",
            result_version=result_version,
        )
        self.assertEqual([item.draft_version for item in history], [3, 2, 1])

    async def test_delete_wishlist_removes_all_versions_and_checks_owner(self):
        from app.run_lifecycle import (
            RunLifecycle,
            RunLifecycleError,
            WishlistDraftGroup,
        )

        await self.seed_release()
        lifecycle = RunLifecycle(
            self.dsn,
            auto_profile=True,
            worker_owner="worker-wishlist-delete",
            algo_client=FakeAlgoClient(),
        )
        self.addAsyncCleanup(lifecycle.close)
        completed = await self._publish_formal(
            lifecycle,
            principal="principal-wishlist-delete",
        )
        result_version = completed.result.result_version
        groups = (
            WishlistDraftGroup(
                university_code="U001",
                major_group="01",
                major_codes=("M01",),
            ),
        )
        for version in (1, 2):
            await lifecycle.SaveWishlistDraft(
                principal_id="principal-wishlist-delete",
                result_version=result_version,
                expected_version=version - 1,
                idempotency_key=f"delete-save-{version}",
                groups=groups,
            )

        with self.assertRaises(RunLifecycleError) as forbidden:
            await lifecycle.DeleteWishlist(
                principal_id="principal-other",
                result_version=result_version,
                request_id="delete-other",
            )
        self.assertEqual(forbidden.exception.reason, "RESULT_NOT_FOUND")

        receipt = await lifecycle.DeleteWishlist(
            principal_id="principal-wishlist-delete",
            result_version=result_version,
            request_id="delete-owner",
        )
        self.assertTrue(receipt.deleted)
        self.assertEqual(
            await lifecycle.ListWishlists(
                principal_id="principal-wishlist-delete",
            ),
            (),
        )
        conn = await self._connect()
        try:
            draft_count = await (
                await conn.execute(
                    """
                    SELECT COUNT(*) AS count
                    FROM wishlist_draft_version
                    WHERE principal_id = %s AND result_version = %s
                    """,
                    ("principal-wishlist-delete", result_version),
                )
            ).fetchone()
            tombstone = await (
                await conn.execute(
                    """
                    SELECT request_id
                    FROM wishlist_deletion
                    WHERE principal_id = %s AND result_version = %s
                    """,
                    ("principal-wishlist-delete", result_version),
                )
            ).fetchone()
        finally:
            await conn.close()
        self.assertEqual(draft_count["count"], 0)
        self.assertEqual(tombstone["request_id"], "delete-owner")
