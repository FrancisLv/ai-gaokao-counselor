"""#30：不可变证据包与 V1_FORMAL 正式结果原子发布。"""
from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
import hashlib
import asyncio
from uuid import UUID, uuid4

import psycopg
import grpc

from evidence import (
    Claim,
    DerivedEvidence,
    DocumentApplicability,
    DocumentEvidence,
    EvidencePackage,
    EvidenceRef,
    FormalGateContext,
    NormalizedFact,
    QualificationCoverage,
    QualificationGroup,
    QualificationMajor,
    QualificationSummary,
    RenderedBinding,
    RequiredNotice,
    VerificationScope,
)
from run_lifecycle_support import RunLifecyclePostgresTest


def _document_evidence(
    release_id: str,
    *,
    evidence_key: str,
    excerpt: str,
) -> DocumentEvidence:
    return DocumentEvidence(
        doc_id=f"doc-{evidence_key}",
        chunk_id=f"chunk-{evidence_key}",
        title="湖南测试大学2025年招生章程",
        document_type="charter",
        issuer="湖南测试大学",
        issuer_type="university",
        issuer_id="school-1001",
        school_id="1001",
        source_uri=f"https://example.invalid/{evidence_key}",
        fetched_at=datetime(2025, 1, 2, tzinfo=timezone.utc),
        applicability=DocumentApplicability(
            regions=("湖南",),
            categories=("物理类",),
            batches=("本科批",),
            valid_from=date(2025, 1, 1),
            valid_to=date(2026, 12, 31),
        ),
        section_path=f"正式结果 > {evidence_key}",
        excerpt_sha256=hashlib.sha256(excerpt.encode()).hexdigest(),
        char_start=0,
        char_end=len(excerpt),
        excerpt=excerpt,
        release_id=release_id,
        document_content_sha256="a" * 64,
        raw_storage_uri=f"s3://bucket/{evidence_key}.txt",
        chunk_set_version="chunks-v1",
    )


def _document_ref(
    release_id: str,
    *,
    evidence_key: str,
    excerpt: str,
) -> EvidenceRef:
    document = _document_evidence(
        release_id,
        evidence_key=evidence_key,
        excerpt=excerpt,
    )
    return EvidenceRef(
        evidence_id=f"ev-{evidence_key}",
        evidence_type="DOCUMENT",
        release_id=release_id,
        content_sha256=document.excerpt_sha256,
        created_at=datetime(2025, 1, 2, tzinfo=timezone.utc),
        producer_version="policy-retrieval-v1",
        document=document,
    )


def _notice(
    release_id: str,
    notice_type: str,
    normalized_text: str,
) -> RequiredNotice:
    excerpt = f"{notice_type}：{normalized_text}"
    ref = _document_ref(
        release_id,
        evidence_key=notice_type.lower(),
        excerpt=excerpt,
    )
    return RequiredNotice(
        notice_type=notice_type,
        normalized_text=normalized_text,
        content_sha256=hashlib.sha256(normalized_text.encode()).hexdigest(),
        evidence_refs=(ref,),
    )


def _formal_package(
    *,
    release_id: str,
    profile_version: UUID,
    result_version: UUID,
    normal_claim_verifiable: bool = True,
) -> EvidencePackage:
    excerpt = "学生可以在第二学期申请转专业。"
    evidence = _document_ref(
        release_id,
        evidence_key="transfer",
        excerpt=excerpt,
    )
    critical_claim = Claim(
        claim_id="claim-transfer",
        claim_type="DOCUMENT_FACT",
        criticality="CRITICAL",
        normalized=NormalizedFact(
            subject="湖南测试大学",
            predicate="转专业政策",
            object=excerpt,
            unit=None,
        ),
        rendered_text=excerpt,
        evidence_refs=(evidence,),
        rendered_bindings=(
            RenderedBinding(
                fact_path="normalized.object",
                rendered_text=excerpt,
                evidence_id=evidence.evidence_id,
            ),
        ),
    )
    normal_excerpt = (
        "校园环境优美。"
        if normal_claim_verifiable
        else "校园环境另有说明。"
    )
    normal_evidence = _document_ref(
        release_id,
        evidence_key="campus",
        excerpt="校园环境优美。",
    )
    normal_claim = Claim(
        claim_id="claim-campus",
        claim_type="DOCUMENT_FACT",
        criticality="NORMAL",
        normalized=NormalizedFact(
            subject="湖南测试大学",
            predicate="校园环境",
            object=normal_excerpt,
            unit=None,
        ),
        rendered_text=normal_excerpt,
        evidence_refs=(normal_evidence,),
        rendered_bindings=(
            RenderedBinding(
                fact_path="normalized.object",
                rendered_text=normal_excerpt,
                evidence_id=normal_evidence.evidence_id,
            ),
        ),
    )
    notices = (
        _notice(
            release_id,
            "DISCLAIMER",
            "本方案仅提供志愿参考，不构成录取承诺。",
        ),
        _notice(
            release_id,
            "RISK_NOTICE",
            "录取概率为历史估计，存在滑档风险。",
        ),
        _notice(release_id, "NO_ADMISSION_GUARANTEE", "不保证录取。"),
    )
    result_id = str(result_version)
    return EvidencePackage(
        release_id=release_id,
        profile_version=str(profile_version),
        result_version=result_id,
        verifier_version="evidence-verifier-v1",
        scope=VerificationScope(
            province="湖南",
            application_year=2025,
            category="物理类",
            batch="本科批",
            school_id="1001",
        ),
        claims=(critical_claim, normal_claim),
        evidence_registry=(
            evidence,
            normal_evidence,
            *(
                ref
                for notice in notices
                for ref in notice.evidence_refs
            ),
        ),
        gate_context=FormalGateContext(
            context_version="formal-gate-v1",
            release_id=release_id,
            profile_version=str(profile_version),
            result_version=result_id,
            algorithm_config_version="algo-config-v1",
            algorithm_config_sha256="c" * 64,
            verifier_version="evidence-verifier-v1",
            qualification=QualificationSummary(
                status="COMPLETE",
                coverage=(
                    QualificationCoverage(
                        rule_type="ADMISSION_RULES",
                        status="FULL",
                    ),
                ),
                groups=(
                    QualificationGroup(
                        group_key="1001:01",
                        status="PASS",
                        majors=(
                            QualificationMajor(
                                major_code="080901",
                                status="PASS",
                            ),
                        ),
                    ),
                ),
                conflict_count=0,
                unknown_count=0,
            ),
            required_notices=notices,
        ),
    )


def _with_derived_sample_years(
    package: EvidencePackage,
    sample_years: tuple[int, ...],
) -> EvidencePackage:
    derived = DerivedEvidence(
        operation_id="rank_convert",
        algorithm="rank_conversion",
        algorithm_version="rank-v1",
        config_sha256="c" * 64,
        input_snapshot_sha256="d" * 64,
        profile_version=package.profile_version,
        release_id=package.release_id,
        sample_years=sample_years,
        output_field="rank",
        output_value=15860,
        unit="名",
        precision=0,
        artifact_sha256="e" * 64,
        assumptions=("使用湖南省同科类一分一段表",),
        warnings=("位次仅用于同科类比较",),
    )
    ref = EvidenceRef(
        evidence_id="ev-derived-rank",
        evidence_type="DERIVED",
        release_id=package.release_id,
        content_sha256=derived.artifact_sha256,
        created_at=datetime(2025, 1, 2, tzinfo=timezone.utc),
        producer_version="algo-estimate-v1",
        derived=derived,
    )
    claim = Claim(
        claim_id="claim-rank",
        claim_type="DERIVED_FACT",
        criticality="CRITICAL",
        normalized=NormalizedFact(
            subject="湖南物理类考生",
            predicate="rank",
            object=15860,
            unit="名",
            sample_years=sample_years,
            precision=0,
        ),
        rendered_text="考生位次为 15860 名，样本年份为 2022、2023、2024 年。",
        evidence_refs=(ref,),
        rendered_bindings=(
            RenderedBinding(
                fact_path="normalized.object",
                rendered_text="15860",
                evidence_id=ref.evidence_id,
            ),
            RenderedBinding(
                fact_path="normalized.unit",
                rendered_text="名",
                evidence_id=ref.evidence_id,
            ),
            RenderedBinding(
                fact_path="normalized.sample_years",
                rendered_text="2022、2023、2024",
                evidence_id=ref.evidence_id,
            ),
        ),
    )
    return replace(
        package,
        claims=(package.claims[0], claim),
        evidence_registry=(*package.evidence_registry, ref),
    )


class FormalResultLifecycleTests(RunLifecyclePostgresTest):
    async def _prepare_running_run(self, package: EvidencePackage):
        from app.run_lifecycle import (
            RunLifecycle,
            StructuredProfileInput,
            SubmitRunCommand,
        )

        release_id = await self.seed_release(release_id=package.release_id)
        lifecycle = RunLifecycle(
            self.dsn,
            worker_owner="worker-formal",
            algo_client=object(),
        )
        receipt = await lifecycle.SubmitRun(
            SubmitRunCommand(
                principal_id="principal-formal",
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
                idempotency_key=f"formal-submit-{uuid4()}",
            )
        )
        profile_version = UUID(package.profile_version)
        conn = await self._connect()
        try:
            await conn.execute(
                """
                INSERT INTO profile_revision (
                    profile_version, session_id, run_id, revision_no,
                    lifecycle, fields, confirmed_at
                )
                VALUES (%s, %s, %s, 1, 'CONFIRMED', %s::jsonb, NOW())
                """,
                (
                    profile_version,
                    receipt.session_id,
                    receipt.run_id,
                    '{"score":{"state":"KNOWN","source":"USER","value":600}}',
                ),
            )
            await conn.execute(
                """
                INSERT INTO profile_confirmation (
                    confirmation_id, profile_version, confirmed_by
                )
                VALUES (%s, %s, 'principal-formal')
                """,
                (uuid4(), profile_version),
            )
            await conn.execute(
                """
                UPDATE run
                SET working_profile_version = %s,
                    confirmed_profile_version = %s
                WHERE run_id = %s
                """,
                (profile_version, profile_version, receipt.run_id),
            )
            row = await lifecycle._claim_run(conn, receipt.run_id)
        finally:
            await conn.close()
        self.assertIsNotNone(row)
        return lifecycle, receipt.run_id, row

    async def test_pass_package_is_published_atomically_and_read_consistently(self):
        from app.run_lifecycle import PublishFormalResultCommand

        release_id = f"rel-formal-{uuid4().hex}"
        profile_version = uuid4()
        result_version = uuid4()
        package = _formal_package(
            release_id=release_id,
            profile_version=profile_version,
            result_version=result_version,
        )
        lifecycle, run_id, claim = await self._prepare_running_run(package)
        self.addAsyncCleanup(lifecycle.close)

        published = await lifecycle.PublishFormalResult(
            PublishFormalResultCommand(
                principal_id="principal-formal",
                run_id=run_id,
                expected_state_version=claim["state_version"],
                expected_lease_epoch=claim["lease_epoch"],
                package=package,
            )
        )

        self.assertTrue(published.accepted)
        self.assertFalse(published.replayed)
        self.assertEqual(published.result_version, result_version)
        self.assertEqual(published.result_kind, "V1_FORMAL")
        self.assertEqual(published.verification_status, "PASS")
        view = await lifecycle.GetRun(
            principal_id="principal-formal",
            run_id=run_id,
        )
        self.assertEqual(view.state, "SUCCEEDED")
        self.assertEqual(view.result.result_version, result_version)
        self.assertEqual(view.result.result_kind, "V1_FORMAL")
        self.assertEqual(
            view.formal_result.evidence_package_sha256,
            published.evidence_package_sha256,
        )
        self.assertEqual(
            view.formal_result.verification_report_sha256,
            published.verification_report_sha256,
        )
        self.assertEqual(
            view.formal_result.verification_summary["status"],
            "PASS",
        )

        events = lifecycle.WatchRun(
            principal_id="principal-formal",
            run_id=run_id,
            after_event_seq=0,
            poll_interval=0.01,
        )
        result_event = None
        try:
            while result_event is None:
                event = await anext(events)
                if event.type == "RESULT":
                    result_event = event
        finally:
            await events.aclose()

        self.assertEqual(
            result_event.payload["formal_result"]["result_version"],
            str(view.formal_result.result_version),
        )
        self.assertEqual(
            result_event.payload["formal_result"]["verification_summary"],
            view.formal_result.verification_summary,
        )
        self.assertEqual(
            result_event.payload["formal_result"]["evidence_package_sha256"],
            view.formal_result.evidence_package_sha256,
        )
        self.assertEqual(
            result_event.payload["result_version"],
            str(result_version),
        )

    async def test_cancel_wins_before_formal_commit_without_publishing_result(self):
        from app.run_lifecycle import (
            CancelRunCommand,
            PublishFormalResultCommand,
            RunLifecycle,
        )

        entered = asyncio.Event()
        continue_publish = asyncio.Event()

        class PausingFormalLifecycle(RunLifecycle):
            async def _before_formal_publish_hook(self, run_id):
                entered.set()
                await continue_publish.wait()

        release_id = f"rel-formal-cancel-{uuid4().hex}"
        profile_version = uuid4()
        result_version = uuid4()
        package = _formal_package(
            release_id=release_id,
            profile_version=profile_version,
            result_version=result_version,
        )
        lifecycle, run_id, claim = await self._prepare_running_run(package)
        await lifecycle.close()
        pausing = PausingFormalLifecycle(
            self.dsn,
            worker_owner="worker-formal",
            algo_client=object(),
        )
        self.addAsyncCleanup(pausing.close)

        publish_task = asyncio.create_task(
            pausing.PublishFormalResult(
                PublishFormalResultCommand(
                    principal_id="principal-formal",
                    run_id=run_id,
                    expected_state_version=claim["state_version"],
                    expected_lease_epoch=claim["lease_epoch"],
                    package=package,
                )
            )
        )
        await asyncio.wait_for(entered.wait(), timeout=2)
        cancelled = await pausing.CancelRun(
            CancelRunCommand(
                principal_id="principal-formal",
                run_id=run_id,
                idempotency_key="cancel-before-formal-publish",
            )
        )
        continue_publish.set()
        publication = await asyncio.wait_for(publish_task, timeout=2)

        self.assertTrue(cancelled.accepted)
        self.assertFalse(publication.accepted)
        self.assertEqual(publication.reason, "CANCELLED")
        view = await pausing.GetRun(
            principal_id="principal-formal",
            run_id=run_id,
        )
        self.assertEqual(view.state, "CANCELLED")
        self.assertIsNone(view.result)
        self.assertIsNone(view.formal_result)

    async def test_partial_publishes_only_retained_claims_and_stripped_ids(self):
        from app.run_lifecycle import PublishFormalResultCommand

        release_id = f"rel-formal-partial-{uuid4().hex}"
        profile_version = uuid4()
        result_version = uuid4()
        package = _formal_package(
            release_id=release_id,
            profile_version=profile_version,
            result_version=result_version,
            normal_claim_verifiable=False,
        )
        lifecycle, run_id, claim = await self._prepare_running_run(package)
        self.addAsyncCleanup(lifecycle.close)

        published = await lifecycle.PublishFormalResult(
            PublishFormalResultCommand(
                principal_id="principal-formal",
                run_id=run_id,
                expected_state_version=claim["state_version"],
                expected_lease_epoch=claim["lease_epoch"],
                package=package,
            )
        )
        view = await lifecycle.GetRun(
            principal_id="principal-formal",
            run_id=run_id,
        )
        conn = await self._connect()
        try:
            row = await (
                await conn.execute(
                    """
                    SELECT package.package_json,
                           report.report_json
                    FROM run_evidence_package package
                    JOIN run_verification_report report
                      ON report.result_version = package.result_version
                    WHERE package.result_version = %s
                    """,
                    (result_version,),
                )
            ).fetchone()
        finally:
            await conn.close()

        self.assertTrue(published.accepted)
        self.assertEqual(published.verification_status, "PARTIAL")
        self.assertEqual(
            published.stripped_claim_ids,
            ("claim-campus",),
        )
        self.assertEqual(
            view.formal_result.verification_summary["stripped_claim_ids"],
            ["claim-campus"],
        )
        self.assertEqual(
            [
                claim["claim_id"]
                for claim in row["package_json"]["claims"]
            ],
            ["claim-transfer"],
        )
        published_evidence_ids = {
            evidence["evidence_id"]
            for evidence in row["package_json"]["evidence_registry"]
        }
        self.assertNotIn("ev-campus", published_evidence_ids)
        self.assertEqual(
            row["report_json"]["stripped_claim_ids"],
            ["claim-campus"],
        )

    async def test_blocked_package_fails_run_without_queryable_formal_result(self):
        from app.run_lifecycle import PublishFormalResultCommand

        release_id = f"rel-formal-blocked-{uuid4().hex}"
        profile_version = uuid4()
        result_version = uuid4()
        package = _formal_package(
            release_id=release_id,
            profile_version=profile_version,
            result_version=result_version,
        )
        critical = package.claims[0]
        package = replace(
            package,
            claims=(
                replace(
                    critical,
                    normalized=replace(
                        critical.normalized,
                        object="校园不得转专业。",
                    ),
                ),
                *package.claims[1:],
            ),
        )
        lifecycle, run_id, claim = await self._prepare_running_run(package)
        self.addAsyncCleanup(lifecycle.close)

        publication = await lifecycle.PublishFormalResult(
            PublishFormalResultCommand(
                principal_id="principal-formal",
                run_id=run_id,
                expected_state_version=claim["state_version"],
                expected_lease_epoch=claim["lease_epoch"],
                package=package,
            )
        )
        view = await lifecycle.GetRun(
            principal_id="principal-formal",
            run_id=run_id,
        )
        conn = await self._connect()
        try:
            result_count = await (
                await conn.execute(
                    """
                    SELECT COUNT(*) AS count
                    FROM run_result
                    WHERE run_id = %s
                    """,
                    (run_id,),
                )
            ).fetchone()
        finally:
            await conn.close()

        self.assertFalse(publication.accepted)
        self.assertEqual(publication.reason, "VALIDATION_FAILED")
        self.assertEqual(publication.verification_status, "BLOCKED")
        self.assertEqual(view.state, "FAILED")
        self.assertEqual(view.terminal_reason, "VALIDATION_FAILED")
        self.assertIsNone(view.result)
        self.assertIsNone(view.result_payload)
        self.assertIsNone(view.formal_result)
        self.assertEqual(result_count["count"], 0)

    async def test_retry_replays_same_result_and_rejects_package_digest_conflict(self):
        from app.run_lifecycle import (
            PublishFormalResultCommand,
            RunLifecycle,
            RunLifecycleError,
        )

        class CountingEntailmentVerifier:
            verifier_version = "evidence-verifier-v1"

            def __init__(self):
                self.calls = 0

            def verify(self, claim, document):
                self.calls += 1
                return None

        release_id = f"rel-formal-replay-{uuid4().hex}"
        profile_version = uuid4()
        result_version = uuid4()
        package = _formal_package(
            release_id=release_id,
            profile_version=profile_version,
            result_version=result_version,
            normal_claim_verifiable=False,
        )
        lifecycle, run_id, claim = await self._prepare_running_run(package)
        await lifecycle.close()
        verifier = CountingEntailmentVerifier()
        lifecycle = RunLifecycle(
            self.dsn,
            worker_owner="worker-formal",
            algo_client=object(),
            entailment_verifier=verifier,
        )
        self.addAsyncCleanup(lifecycle.close)
        command = PublishFormalResultCommand(
            principal_id="principal-formal",
            run_id=run_id,
            expected_state_version=claim["state_version"],
            expected_lease_epoch=claim["lease_epoch"],
            package=package,
        )

        first = await lifecycle.PublishFormalResult(command)
        replay = await lifecycle.PublishFormalResult(command)
        reordered_package = replace(
            package,
            claims=tuple(reversed(package.claims)),
            evidence_registry=tuple(reversed(package.evidence_registry)),
        )
        reordered_replay = await lifecycle.PublishFormalResult(
            replace(command, package=reordered_package)
        )
        conflicting_claim = replace(
            package.claims[1],
            normalized=replace(
                package.claims[1].normalized,
                object="校园环境说明已变化。",
            ),
            rendered_text="校园环境说明已变化。",
        )
        conflict_package = replace(
            package,
            claims=(package.claims[0], conflicting_claim),
        )
        with self.assertRaises(RunLifecycleError) as raised:
            await lifecycle.PublishFormalResult(
                replace(command, package=conflict_package)
            )

        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        self.assertTrue(reordered_replay.replayed)
        self.assertEqual(replay.result_version, first.result_version)
        self.assertEqual(
            reordered_replay.result_version,
            first.result_version,
        )
        self.assertEqual(
            replay.evidence_package_sha256,
            first.evidence_package_sha256,
        )
        self.assertEqual(
            replay.verification_report_sha256,
            first.verification_report_sha256,
        )
        self.assertEqual(raised.exception.reason, "RESULT_VERSION_CONFLICT")
        self.assertEqual(verifier.calls, 1)
        conn = await self._connect()
        try:
            counts = await (
                await conn.execute(
                    """
                    SELECT
                        (SELECT COUNT(*) FROM run_result WHERE run_id = %s)
                            AS result_count,
                        (SELECT COUNT(*)
                         FROM run_evidence_package
                         WHERE run_id = %s) AS package_count,
                        (SELECT COUNT(*)
                         FROM run_verification_report
                         WHERE run_id = %s) AS report_count,
                        (SELECT COUNT(*)
                         FROM run_event
                         WHERE run_id = %s AND event_type = 'RESULT')
                            AS result_event_count
                    """,
                    (run_id, run_id, run_id, run_id),
                )
            ).fetchone()
        finally:
            await conn.close()
        self.assertEqual(counts["result_count"], 1)
        self.assertEqual(counts["package_count"], 1)
        self.assertEqual(counts["report_count"], 1)
        self.assertEqual(counts["result_event_count"], 1)

    async def test_retry_treats_reordered_nested_sample_years_as_equivalent(self):
        from app.run_lifecycle import PublishFormalResultCommand

        release_id = f"rel-formal-sample-years-{uuid4().hex}"
        profile_version = uuid4()
        result_version = uuid4()
        base_package = _formal_package(
            release_id=release_id,
            profile_version=profile_version,
            result_version=result_version,
        )
        package = _with_derived_sample_years(
            base_package,
            (2024, 2022, 2023),
        )
        reordered_package = _with_derived_sample_years(
            base_package,
            (2022, 2023, 2024),
        )
        lifecycle, run_id, claim = await self._prepare_running_run(package)
        self.addAsyncCleanup(lifecycle.close)
        command = PublishFormalResultCommand(
            principal_id="principal-formal",
            run_id=run_id,
            expected_state_version=claim["state_version"],
            expected_lease_epoch=claim["lease_epoch"],
            package=package,
        )

        first = await lifecycle.PublishFormalResult(command)
        replay = await lifecycle.PublishFormalResult(
            replace(command, package=reordered_package)
        )

        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.result_version, first.result_version)
        self.assertEqual(
            replay.evidence_package_sha256,
            first.evidence_package_sha256,
        )
        self.assertEqual(
            replay.verification_report_sha256,
            first.verification_report_sha256,
        )

    async def test_published_payload_is_immutable_and_release_revoke_only_marks_stale(self):
        from app.run_lifecycle import PublishFormalResultCommand

        release_id = f"rel-formal-stale-{uuid4().hex}"
        profile_version = uuid4()
        result_version = uuid4()
        package = _formal_package(
            release_id=release_id,
            profile_version=profile_version,
            result_version=result_version,
        )
        lifecycle, run_id, claim = await self._prepare_running_run(package)
        self.addAsyncCleanup(lifecycle.close)
        publication = await lifecycle.PublishFormalResult(
            PublishFormalResultCommand(
                principal_id="principal-formal",
                run_id=run_id,
                expected_state_version=claim["state_version"],
                expected_lease_epoch=claim["lease_epoch"],
                package=package,
            )
        )

        conn = await self._connect()
        try:
            with self.assertRaises(psycopg.Error):
                await conn.execute(
                    """
                    UPDATE run_result
                    SET payload = '{}'::jsonb
                    WHERE result_version = %s
                    """,
                    (result_version,),
                )
            with self.assertRaises(psycopg.Error):
                await conn.execute(
                    """
                    UPDATE run_evidence_package
                    SET package_json = '{}'::jsonb
                    WHERE result_version = %s
                    """,
                    (result_version,),
                )
            with self.assertRaises(psycopg.Error):
                await conn.execute(
                    """
                    DELETE FROM run_evidence_package
                    WHERE result_version = %s
                    """,
                    (result_version,),
                )
            with self.assertRaises(psycopg.Error):
                await conn.execute(
                    """
                    UPDATE run_verification_report
                    SET report_json = '{}'::jsonb
                    WHERE result_version = %s
                    """,
                    (result_version,),
                )
            with self.assertRaises(psycopg.Error):
                await conn.execute(
                    """
                    UPDATE run_result_state_event
                    SET status = 'STALE'
                    WHERE result_version = %s
                    """,
                    (result_version,),
                )
        finally:
            await conn.close()

        await self.revoke_release(release_id)
        view = await lifecycle.GetRun(
            principal_id="principal-formal",
            run_id=run_id,
        )
        conn = await self._connect()
        try:
            row = await (
                await conn.execute(
                    """
                    SELECT package.package_sha256,
                           report.report_sha256
                    FROM run_evidence_package package
                    JOIN run_verification_report report
                      ON report.result_version = package.result_version
                    WHERE package.result_version = %s
                    """,
                    (result_version,),
                )
            ).fetchone()
        finally:
            await conn.close()

        self.assertTrue(publication.accepted)
        self.assertEqual(view.formal_result.result_state, "STALE")
        self.assertEqual(
            view.formal_result.stale_reason,
            "RELEASE_REVOKED",
        )
        self.assertEqual(
            row["package_sha256"],
            publication.evidence_package_sha256,
        )
        self.assertEqual(
            row["report_sha256"],
            publication.verification_report_sha256,
        )

    async def test_published_formal_result_cannot_be_rewritten_as_policy_evidence(
        self,
    ):
        from app.run_lifecycle import PublishFormalResultCommand

        release_id = f"rel-formal-immutable-{uuid4().hex}"
        profile_version = uuid4()
        result_version = uuid4()
        package = _formal_package(
            release_id=release_id,
            profile_version=profile_version,
            result_version=result_version,
        )
        lifecycle, run_id, claim = await self._prepare_running_run(package)
        self.addAsyncCleanup(lifecycle.close)
        await lifecycle.PublishFormalResult(
            PublishFormalResultCommand(
                principal_id="principal-formal",
                run_id=run_id,
                expected_state_version=claim["state_version"],
                expected_lease_epoch=claim["lease_epoch"],
                package=package,
            )
        )

        conn = await self._connect()
        try:
            with self.assertRaises(psycopg.Error):
                await conn.execute(
                    """
                    UPDATE run_result
                    SET result_kind = 'POLICY_EVIDENCE',
                        confirmed_profile_version = NULL,
                        payload = '{"result_kind":"POLICY_EVIDENCE"}'::jsonb
                    WHERE result_version = %s
                    """,
                    (result_version,),
                )
            row = await (
                await conn.execute(
                    """
                    SELECT result_kind, confirmed_profile_version, payload
                    FROM run_result
                    WHERE result_version = %s
                    """,
                    (result_version,),
                )
            ).fetchone()
        finally:
            await conn.close()

        self.assertEqual(row["result_kind"], "V1_FORMAL")
        self.assertEqual(row["confirmed_profile_version"], profile_version)
        self.assertEqual(row["payload"]["result_kind"], "V1_FORMAL")

    async def test_release_revoked_before_publish_commit_fails_closed(self):
        from app.run_lifecycle import PublishFormalResultCommand, RunLifecycle

        release_id = f"rel-formal-revoked-{uuid4().hex}"
        profile_version = uuid4()
        result_version = uuid4()
        package = _formal_package(
            release_id=release_id,
            profile_version=profile_version,
            result_version=result_version,
        )
        lifecycle, run_id, claim = await self._prepare_running_run(package)
        await lifecycle.close()

        class RevokingFormalLifecycle(RunLifecycle):
            async def _before_formal_publish_hook(self, run_id):
                conn = await self._connect(autocommit=True)
                try:
                    await conn.execute(
                        """
                        UPDATE release
                        SET status = 'REVOKED', revoked_at = NOW()
                        WHERE release_id = %s
                        """,
                        (release_id,),
                    )
                finally:
                    await conn.close()

        hooked = RevokingFormalLifecycle(
            self.dsn,
            worker_owner="worker-formal",
            algo_client=object(),
        )
        self.addAsyncCleanup(hooked.close)
        publication = await hooked.PublishFormalResult(
            PublishFormalResultCommand(
                principal_id="principal-formal",
                run_id=run_id,
                expected_state_version=claim["state_version"],
                expected_lease_epoch=claim["lease_epoch"],
                package=package,
            )
        )
        view = await hooked.GetRun(
            principal_id="principal-formal",
            run_id=run_id,
        )

        self.assertFalse(publication.accepted)
        self.assertEqual(publication.reason, "RELEASE_REVOKED")
        self.assertEqual(view.state, "FAILED")
        self.assertEqual(view.terminal_reason, "RELEASE_REVOKED")
        self.assertIsNone(view.result)
        self.assertIsNone(view.formal_result)

    async def test_grpc_get_and_watch_expose_the_same_formal_summary(self):
        from api.orchestrator.v1 import orchestrator_pb2 as pb
        from api.orchestrator.v1 import orchestrator_pb2_grpc as pb_grpc
        from app.run_lifecycle import PublishFormalResultCommand
        from app.server import OrchestratorServicer

        release_id = f"rel-formal-grpc-{uuid4().hex}"
        profile_version = uuid4()
        result_version = uuid4()
        package = _formal_package(
            release_id=release_id,
            profile_version=profile_version,
            result_version=result_version,
        )
        lifecycle, run_id, claim = await self._prepare_running_run(package)
        self.addAsyncCleanup(lifecycle.close)
        await lifecycle.PublishFormalResult(
            PublishFormalResultCommand(
                principal_id="principal-formal",
                run_id=run_id,
                expected_state_version=claim["state_version"],
                expected_lease_epoch=claim["lease_epoch"],
                package=package,
            )
        )
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
        client = pb_grpc.OrchestratorStub(channel)
        metadata = (("x-principal-id", "principal-formal"),)

        view = await client.GetRun(
            pb.GetRunRequest(run_id=str(run_id)),
            metadata=metadata,
        )
        events = client.WatchRun(
            pb.WatchRunRequest(
                run_id=str(run_id),
                after_event_seq=0,
            ),
            metadata=metadata,
        )
        result_event = None
        try:
            while result_event is None:
                event = await asyncio.wait_for(events.read(), timeout=1)
                if event.type == pb.RESULT:
                    result_event = event
        finally:
            events.cancel()

        self.assertEqual(view.result.result_kind, "V1_FORMAL")
        self.assertEqual(
            view.formal_result.result_version,
            str(result_version),
        )
        self.assertEqual(
            view.formal_result.verification_status,
            "PASS",
        )
        self.assertEqual(
            result_event.formal_result,
            view.formal_result,
        )

    async def test_confirmed_profile_mismatch_is_rejected_before_result_insert(self):
        from app.run_lifecycle import (
            PublishFormalResultCommand,
            RunLifecycleError,
        )

        release_id = f"rel-formal-profile-{uuid4().hex}"
        profile_version = uuid4()
        other_profile_version = uuid4()
        result_version = uuid4()
        package = _formal_package(
            release_id=release_id,
            profile_version=profile_version,
            result_version=result_version,
        )
        lifecycle, run_id, claim = await self._prepare_running_run(package)
        self.addAsyncCleanup(lifecycle.close)
        mismatched = replace(
            package,
            profile_version=str(other_profile_version),
            gate_context=replace(
                package.gate_context,
                profile_version=str(other_profile_version),
            ),
        )

        with self.assertRaises(RunLifecycleError) as raised:
            await lifecycle.PublishFormalResult(
                PublishFormalResultCommand(
                    principal_id="principal-formal",
                    run_id=run_id,
                    expected_state_version=claim["state_version"],
                    expected_lease_epoch=claim["lease_epoch"],
                    package=mismatched,
                )
            )
        view = await lifecycle.GetRun(
            principal_id="principal-formal",
            run_id=run_id,
        )

        self.assertEqual(
            raised.exception.reason,
            "PROFILE_VERSION_MISMATCH",
        )
        self.assertEqual(view.state, "RUNNING")
        self.assertIsNone(view.result)
        self.assertIsNone(view.formal_result)

    async def test_stale_state_or_lease_epoch_cannot_publish(self):
        from app.run_lifecycle import (
            PublishFormalResultCommand,
            RunLifecycleError,
        )

        release_id = f"rel-formal-lease-{uuid4().hex}"
        profile_version = uuid4()
        result_version = uuid4()
        package = _formal_package(
            release_id=release_id,
            profile_version=profile_version,
            result_version=result_version,
        )
        lifecycle, run_id, claim = await self._prepare_running_run(package)
        self.addAsyncCleanup(lifecycle.close)
        command = PublishFormalResultCommand(
            principal_id="principal-formal",
            run_id=run_id,
            expected_state_version=claim["state_version"],
            expected_lease_epoch=claim["lease_epoch"],
            package=package,
        )

        for stale in (
            replace(
                command,
                expected_state_version=claim["state_version"] + 1,
            ),
            replace(
                command,
                expected_lease_epoch=claim["lease_epoch"] + 1,
            ),
        ):
            with self.assertRaises(RunLifecycleError) as raised:
                await lifecycle.PublishFormalResult(stale)
            self.assertEqual(raised.exception.reason, "RUN_STATE_CONFLICT")

        blocked_view = await lifecycle.GetRun(
            principal_id="principal-formal",
            run_id=run_id,
        )
        self.assertEqual(blocked_view.state, "RUNNING")
        self.assertIsNone(blocked_view.result)

        published = await lifecycle.PublishFormalResult(command)
        self.assertTrue(published.accepted)

    async def test_result_version_reuse_across_runs_is_a_stable_conflict(self):
        from app.run_lifecycle import (
            PublishFormalResultCommand,
            RunLifecycleError,
        )

        result_version = uuid4()
        first_package = _formal_package(
            release_id=f"rel-formal-first-{uuid4().hex}",
            profile_version=uuid4(),
            result_version=result_version,
        )
        first_lifecycle, first_run_id, first_claim = (
            await self._prepare_running_run(first_package)
        )
        self.addAsyncCleanup(first_lifecycle.close)
        await first_lifecycle.PublishFormalResult(
            PublishFormalResultCommand(
                principal_id="principal-formal",
                run_id=first_run_id,
                expected_state_version=first_claim["state_version"],
                expected_lease_epoch=first_claim["lease_epoch"],
                package=first_package,
            )
        )

        second_package = _formal_package(
            release_id=f"rel-formal-second-{uuid4().hex}",
            profile_version=uuid4(),
            result_version=result_version,
        )
        second_lifecycle, second_run_id, second_claim = (
            await self._prepare_running_run(second_package)
        )
        self.addAsyncCleanup(second_lifecycle.close)
        with self.assertRaises(RunLifecycleError) as raised:
            await second_lifecycle.PublishFormalResult(
                PublishFormalResultCommand(
                    principal_id="principal-formal",
                    run_id=second_run_id,
                    expected_state_version=second_claim["state_version"],
                    expected_lease_epoch=second_claim["lease_epoch"],
                    package=second_package,
                )
            )
        second_view = await second_lifecycle.GetRun(
            principal_id="principal-formal",
            run_id=second_run_id,
        )

        self.assertEqual(
            raised.exception.reason,
            "RESULT_VERSION_CONFLICT",
        )
        self.assertEqual(second_view.state, "RUNNING")
        self.assertIsNone(second_view.result)
