"""服务端对话记录、历史列表与报告后解释。"""
from __future__ import annotations

from dataclasses import replace
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from run_lifecycle_support import RunLifecyclePostgresTest


class ConversationHistoryTests(RunLifecyclePostgresTest):
    async def test_messages_are_persisted_and_listed_for_the_owner(self):
        from app.run_lifecycle import (
            ChatMessageInput,
            RunLifecycle,
            StructuredProfileInput,
            SubmitRunCommand,
        )

        await self.seed_release()
        lifecycle = RunLifecycle(self.dsn)
        receipt = await lifecycle.SubmitRun(
            SubmitRunCommand(
                principal_id="principal-conversation",
                session_id=None,
                task_kind="GENERATE_PLAN",
                input=self.profile(),
                idempotency_key="conversation-submit",
                messages=(
                    ChatMessageInput(
                        client_message_id="client-message-1",
                        role="USER",
                        kind="TEXT",
                        content="我物理类 612 分，选的是物化生",
                    ),
                    ChatMessageInput(
                        client_message_id="client-message-2",
                        role="ASSISTANT",
                        kind="TEXT",
                        content="再补充一下偏好。",
                    ),
                ),
            )
        )

        sessions = await lifecycle.ListSessions(
            principal_id="principal-conversation",
        )
        session = await lifecycle.GetSession(
            principal_id="principal-conversation",
            session_id=receipt.session_id,
        )

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].session_id, receipt.session_id)
        self.assertEqual(sessions[0].title, "物理类 612 分志愿方案")
        self.assertEqual(
            [message.content for message in session.messages],
            [
                "我物理类 612 分，选的是物化生",
                "再补充一下偏好。",
            ],
        )
        self.assertEqual(session.profile_fields, {})

    async def test_history_hides_another_principals_session(self):
        from app.run_lifecycle import (
            RunLifecycle,
            RunLifecycleError,
            StructuredProfileInput,
            SubmitRunCommand,
        )

        await self.seed_release()
        lifecycle = RunLifecycle(self.dsn)
        receipt = await lifecycle.SubmitRun(
            SubmitRunCommand(
                principal_id="principal-a",
                session_id=None,
                task_kind="GENERATE_PLAN",
                input=self.profile(),
                idempotency_key="private-conversation",
            )
        )

        with self.assertRaises(RunLifecycleError) as raised:
            await lifecycle.GetSession(
                principal_id="principal-b",
                session_id=receipt.session_id,
            )

        self.assertEqual(raised.exception.reason, "SESSION_NOT_FOUND")
        self.assertEqual(
            await lifecycle.ListSessions(principal_id="principal-b"),
            (),
        )

    async def test_explain_result_binds_parent_and_appends_answer(self):
        from app.run_lifecycle import RunLifecycle, SubmitRunCommand

        await self.seed_release()
        lifecycle = RunLifecycle(self.dsn, worker_owner="explain-worker")
        parent = await lifecycle.SubmitRun(
            SubmitRunCommand(
                principal_id="principal-explain",
                session_id=None,
                task_kind="GENERATE_PLAN",
                input=self.profile(),
                idempotency_key="parent-plan",
            )
        )
        parent_result_version = await self.publish_minimal_formal(
            parent.run_id,
            parent.session_id,
        )

        submission = await lifecycle.SubmitConversationMessage(
            principal_id="principal-explain",
            session_id=parent.session_id,
            profile=self.profile(),
            parent_result_version=parent_result_version,
            message_text="为什么中南大学排在前面？",
            idempotency_key="explain-report",
        )
        self.assertEqual(submission.task_kind, "EXPLAIN_RESULT")
        self.assertIsNotNone(submission.run_id)

        await lifecycle.run_due_work_once()

        view = await lifecycle.GetRun(
            principal_id="principal-explain",
            run_id=submission.run_id,
        )
        session = await lifecycle.GetSession(
            principal_id="principal-explain",
            session_id=parent.session_id,
        )
        self.assertEqual(view.state, "SUCCEEDED")
        self.assertEqual(view.result.result_kind, "EXPLAIN_RESULT")
        self.assertIn("确定性算法", view.conversation_answer or "")
        self.assertTrue(
            any(
                message.role == "ASSISTANT"
                and message.run_id == submission.run_id
                and "确定性算法" in message.content
                for message in session.messages
            )
        )

    async def test_llm_fallback_can_patch_an_ambiguous_revision(self):
        from app.run_lifecycle import RunLifecycle, SubmitRunCommand
        from app.run_lifecycle.conversation_intent import ConversationIntent

        class FakeIntentClassifier:
            async def classify(self, request):
                self.request = request
                return ConversationIntent(
                    decision="REVISE_PLAN",
                    patch={"excluded_cities": ["北京"]},
                )

        await self.seed_release()
        classifier = FakeIntentClassifier()
        lifecycle = RunLifecycle(
            self.dsn,
            worker_owner="llm-intent-worker",
            conversation_intent_classifier=classifier,
        )
        parent = await lifecycle.SubmitRun(
            SubmitRunCommand(
                principal_id="principal-intent",
                session_id=None,
                task_kind="GENERATE_PLAN",
                input=self.profile(),
                idempotency_key="parent-intent-plan",
            )
        )
        parent_result_version = await self.publish_minimal_formal(
            parent.run_id,
            parent.session_id,
        )

        submission = await lifecycle.SubmitConversationMessage(
            principal_id="principal-intent",
            session_id=parent.session_id,
            profile=self.profile(),
            parent_result_version=parent_result_version,
            message_text="北京还是不太合适",
            idempotency_key="llm-intent-revision",
        )
        self.assertEqual(submission.task_kind, "REVISE_PLAN")
        self.assertEqual(classifier.request.release_id, "rel_legacy_v1")
        self.assertEqual(classifier.request.run_id, str(parent.run_id))

        await lifecycle.run_due_work_once()
        revised = await lifecycle.GetRun(
            principal_id="principal-intent",
            run_id=submission.run_id,
        )
        fields = revised.current_interrupt.payload["fields"]
        self.assertEqual(
            fields["preferences.excluded_cities"]["value"],
            ["北京"],
        )

    async def test_llm_fallback_can_patch_a_soft_major_interest(self):
        from app.run_lifecycle import RunLifecycle, SubmitRunCommand
        from app.run_lifecycle.conversation_intent import ConversationIntent

        class FakeIntentClassifier:
            async def classify(self, request):
                return ConversationIntent(
                    decision="REVISE_PLAN",
                    patch={"major_interests": ["物理学"]},
                )

        await self.seed_release()
        lifecycle = RunLifecycle(
            self.dsn,
            worker_owner="llm-major-interest-worker",
            conversation_intent_classifier=FakeIntentClassifier(),
        )
        parent = await lifecycle.SubmitRun(
            SubmitRunCommand(
                principal_id="principal-major-interest",
                session_id=None,
                task_kind="GENERATE_PLAN",
                input=self.profile(),
                idempotency_key="parent-major-interest-plan",
            )
        )
        parent_result_version = await self.publish_minimal_formal(
            parent.run_id,
            parent.session_id,
        )

        submission = await lifecycle.SubmitConversationMessage(
            principal_id="principal-major-interest",
            session_id=parent.session_id,
            profile=self.profile(),
            parent_result_version=parent_result_version,
            message_text="专业方向再偏理科一些",
            idempotency_key="major-interest-message",
        )

        self.assertEqual(submission.task_kind, "REVISE_PLAN")
        await lifecycle.run_due_work_once()
        revised = await lifecycle.GetRun(
            principal_id="principal-major-interest",
            run_id=submission.run_id,
        )
        fields = revised.current_interrupt.payload["fields"]
        self.assertEqual(
            fields["preferences.major_interests"]["value"],
            ["物理学"],
        )

    async def test_wanting_to_study_physics_creates_exact_major_interest_revision(
        self,
    ):
        from app.run_lifecycle import RunLifecycle, SubmitRunCommand

        await self.seed_release()
        lifecycle = RunLifecycle(self.dsn, worker_owner="physics-interest-worker")
        parent = await lifecycle.SubmitRun(
            SubmitRunCommand(
                principal_id="principal-physics-interest",
                session_id=None,
                task_kind="GENERATE_PLAN",
                input=self.profile(),
                idempotency_key="parent-physics-interest-plan",
            )
        )
        parent_result_version = await self.publish_minimal_formal(
            parent.run_id,
            parent.session_id,
        )

        submission = await lifecycle.SubmitConversationMessage(
            principal_id="principal-physics-interest",
            session_id=parent.session_id,
            profile=self.profile(),
            parent_result_version=parent_result_version,
            message_text="我想学物理学",
            idempotency_key="physics-interest-revision",
        )

        self.assertEqual(submission.task_kind, "REVISE_PLAN")
        await lifecycle.run_due_work_once()
        revised = await lifecycle.GetRun(
            principal_id="principal-physics-interest",
            run_id=submission.run_id,
        )
        fields = revised.current_interrupt.payload["fields"]
        self.assertEqual(
            fields["preferences.major_interests"]["value"],
            ["物理学"],
        )

    async def test_only_wanting_physics_creates_exact_major_restriction(self):
        from app.run_lifecycle import RunLifecycle, SubmitRunCommand

        await self.seed_release()
        lifecycle = RunLifecycle(self.dsn, worker_owner="physics-restriction-worker")
        parent = await lifecycle.SubmitRun(
            SubmitRunCommand(
                principal_id="principal-physics-restriction",
                session_id=None,
                task_kind="GENERATE_PLAN",
                input=self.profile(),
                idempotency_key="parent-physics-restriction-plan",
            )
        )
        parent_result_version = await self.publish_minimal_formal(
            parent.run_id,
            parent.session_id,
        )

        submission = await lifecycle.SubmitConversationMessage(
            principal_id="principal-physics-restriction",
            session_id=parent.session_id,
            profile=self.profile(),
            parent_result_version=parent_result_version,
            message_text="我只想看物理学",
            idempotency_key="physics-restriction-revision",
        )

        self.assertEqual(submission.task_kind, "REVISE_PLAN")
        await lifecycle.run_due_work_once()
        revised = await lifecycle.GetRun(
            principal_id="principal-physics-restriction",
            run_id=submission.run_id,
        )
        fields = revised.current_interrupt.payload["fields"]
        self.assertEqual(
            fields["preferences.required_major_names"]["value"],
            ["物理学"],
        )
        self.assertEqual(
            fields["preferences.major_interests"]["value"],
            ["物理学"],
        )

    async def test_accepting_physics_related_majors_expands_only_the_interest_scope(
        self,
    ):
        from app.run_lifecycle import RunLifecycle, SubmitRunCommand

        await self.seed_release()
        lifecycle = RunLifecycle(self.dsn, worker_owner="physics-related-worker")
        parent = await lifecycle.SubmitRun(
            SubmitRunCommand(
                principal_id="principal-physics-related",
                session_id=None,
                task_kind="GENERATE_PLAN",
                input=replace(self.profile(), interests=("物理学",)),
                idempotency_key="parent-physics-related-plan",
            )
        )
        parent_result_version = await self.publish_minimal_formal(
            parent.run_id,
            parent.session_id,
        )

        submission = await lifecycle.SubmitConversationMessage(
            principal_id="principal-physics-related",
            session_id=parent.session_id,
            profile=replace(self.profile(), interests=("物理学",)),
            parent_result_version=parent_result_version,
            message_text="也接受相关专业",
            idempotency_key="physics-related-revision",
        )

        self.assertEqual(submission.task_kind, "REVISE_PLAN")
        await lifecycle.run_due_work_once()
        revised = await lifecycle.GetRun(
            principal_id="principal-physics-related",
            run_id=submission.run_id,
        )
        fields = revised.current_interrupt.payload["fields"]
        self.assertEqual(
            fields["preferences.major_interests"]["value"],
            [
                "物理学",
                "物理学类",
                "应用物理学",
                "核物理",
                "材料物理",
                "地球物理学",
                "工程物理",
            ],
        )
        self.assertEqual(
            fields["preferences.required_major_names"]["value"],
            [],
        )

    async def test_ambiguous_risk_revision_uses_llm_with_report_context(self):
        from app.run_lifecycle import RunLifecycle, SubmitRunCommand
        from app.run_lifecycle.conversation_intent import ConversationIntent

        class FakeIntentClassifier:
            async def classify(self, request):
                self.request = request
                return ConversationIntent(
                    decision="REVISE_PLAN",
                    patch={"risk_appetite": "aggressive"},
                )

        await self.seed_release()
        classifier = FakeIntentClassifier()
        lifecycle = RunLifecycle(
            self.dsn,
            worker_owner="rush-intent-worker",
            conversation_intent_classifier=classifier,
        )
        parent = await lifecycle.SubmitRun(
            SubmitRunCommand(
                principal_id="principal-rush",
                session_id=None,
                task_kind="GENERATE_PLAN",
                input=self.profile(),
                idempotency_key="parent-rush-plan",
            )
        )
        parent_result_version = await self.publish_minimal_formal(
            parent.run_id,
            parent.session_id,
        )

        submission = await lifecycle.SubmitConversationMessage(
            principal_id="principal-rush",
            session_id=parent.session_id,
            profile=self.profile(),
            parent_result_version=parent_result_version,
            message_text="现在的选择还可以更大胆一些",
            idempotency_key="rush-intent-revision",
        )

        self.assertEqual(submission.task_kind, "REVISE_PLAN")
        self.assertEqual(classifier.request.risk_appetite, "balanced")
        self.assertEqual(classifier.request.rush_count, 1)
        self.assertEqual(classifier.request.stable_count, 0)
        self.assertEqual(classifier.request.safe_count, 0)
        await lifecycle.run_due_work_once()
        revised = await lifecycle.GetRun(
            principal_id="principal-rush",
            run_id=submission.run_id,
        )
        self.assertEqual(
            revised.current_interrupt.payload["fields"][
                "preferences.risk_appetite"
            ]["value"],
            "aggressive",
        )

    async def test_more_rush_revision_fails_when_rush_count_cannot_increase(self):
        from app.run_lifecycle import (
            ConfirmProfileAnswer,
            ResumeRunCommand,
            RunLifecycle,
            SubmitRunCommand,
        )
        from app.run_lifecycle.conversation_intent import ConversationIntent
        from test_run_lifecycle_algorithm_preview import FakeAlgoClient

        class FakeIntentClassifier:
            async def classify(self, request):
                return ConversationIntent(
                    decision="REVISE_PLAN",
                    patch={"risk_appetite": "aggressive"},
                )

        await self.seed_release()
        lifecycle = RunLifecycle(
            self.dsn,
            worker_owner="rush-capacity-worker",
            algo_client=FakeAlgoClient(),
            conversation_intent_classifier=FakeIntentClassifier(),
        )
        parent = await lifecycle.SubmitRun(
            SubmitRunCommand(
                principal_id="principal-rush-capacity",
                session_id=None,
                task_kind="GENERATE_PLAN",
                input=self.profile(),
                idempotency_key="parent-rush-capacity",
            )
        )
        parent_result_version = await self.publish_minimal_formal(
            parent.run_id,
            parent.session_id,
        )
        submission = await lifecycle.SubmitConversationMessage(
            principal_id="principal-rush-capacity",
            session_id=parent.session_id,
            profile=self.profile(),
            parent_result_version=parent_result_version,
            message_text="冲的比例太少了，增加多几个",
            idempotency_key="rush-capacity-revision",
        )
        self.assertEqual(submission.task_kind, "REVISE_PLAN")

        await lifecycle.run_due_work_once()
        waiting = await lifecycle.GetRun(
            principal_id="principal-rush-capacity",
            run_id=submission.run_id,
        )
        self.assertIsNotNone(waiting.current_interrupt)
        await lifecycle.ResumeRun(
            ResumeRunCommand(
                principal_id="principal-rush-capacity",
                run_id=submission.run_id,
                interrupt_id=waiting.current_interrupt.interrupt_id,
                expected_state_version=waiting.state_version,
                profile_version=UUID(
                    waiting.current_interrupt.payload["profile_version"]
                ),
                answer=ConfirmProfileAnswer(),
                idempotency_key="rush-capacity-confirm",
            )
        )
        for _ in range(3):
            await lifecycle.run_due_work_once()

        failed = await lifecycle.GetRun(
            principal_id="principal-rush-capacity",
            run_id=submission.run_id,
        )
        self.assertEqual(failed.state, "FAILED")
        self.assertEqual(
            failed.terminal_reason,
            "TARGET_ALLOCATION_UNSATISFIED",
        )
        self.assertIsNone(failed.result)

    async def test_explicit_more_rush_stays_gated_when_profile_is_already_aggressive(
        self,
    ):
        from app.run_lifecycle import (
            ConfirmProfileAnswer,
            ResumeRunCommand,
            RunLifecycle,
            SubmitRunCommand,
        )
        from test_run_lifecycle_algorithm_preview import FakeAlgoClient

        await self.seed_release()
        lifecycle = RunLifecycle(
            self.dsn,
            worker_owner="rush-already-aggressive-worker",
            algo_client=FakeAlgoClient(),
        )
        aggressive_profile = replace(
            self.profile(),
            risk_appetite="aggressive",
            preferred_cities=("长沙",),
            excluded_provinces=("湖南",),
        )
        parent = await lifecycle.SubmitRun(
            SubmitRunCommand(
                principal_id="principal-rush-already-aggressive",
                session_id=None,
                task_kind="GENERATE_PLAN",
                input=aggressive_profile,
                idempotency_key="parent-rush-already-aggressive",
            )
        )
        parent_result_version = await self.publish_minimal_formal(
            parent.run_id,
            parent.session_id,
        )

        submission = await lifecycle.SubmitConversationMessage(
            principal_id="principal-rush-already-aggressive",
            session_id=parent.session_id,
            profile=aggressive_profile,
            parent_result_version=parent_result_version,
            message_text="冲的比例太少了，增加多几个",
            idempotency_key="rush-already-aggressive-revision",
        )

        self.assertEqual(submission.task_kind, "REVISE_PLAN")
        self.assertIsNotNone(submission.run_id)
        await lifecycle.run_due_work_once()
        waiting = await lifecycle.GetRun(
            principal_id="principal-rush-already-aggressive",
            run_id=submission.run_id,
        )
        self.assertIsNotNone(waiting.current_interrupt)
        await lifecycle.ResumeRun(
            ResumeRunCommand(
                principal_id="principal-rush-already-aggressive",
                run_id=submission.run_id,
                interrupt_id=waiting.current_interrupt.interrupt_id,
                expected_state_version=waiting.state_version,
                profile_version=UUID(
                    waiting.current_interrupt.payload["profile_version"]
                ),
                answer=ConfirmProfileAnswer(),
                idempotency_key="rush-already-aggressive-confirm",
            )
        )
        for _ in range(3):
            await lifecycle.run_due_work_once()

        failed = await lifecycle.GetRun(
            principal_id="principal-rush-already-aggressive",
            run_id=submission.run_id,
        )
        self.assertEqual(failed.state, "FAILED")
        self.assertEqual(
            failed.terminal_reason,
            "TARGET_ALLOCATION_UNSATISFIED",
        )
        self.assertIsNone(failed.result)

    async def test_off_topic_and_out_of_scope_do_not_create_runs(self):
        from app.run_lifecycle import RunLifecycle, SubmitRunCommand
        from app.run_lifecycle.conversation_intent import ConversationIntent

        class ScopeIntentClassifier:
            def __init__(self):
                self.messages = []

            async def classify(self, request):
                self.messages.append(request.message_text)
                decision = (
                    "OFF_TOPIC"
                    if "天气" in request.message_text
                    else "OUT_OF_SCOPE"
                )
                return ConversationIntent(decision=decision)

        await self.seed_release()
        classifier = ScopeIntentClassifier()
        lifecycle = RunLifecycle(
            self.dsn,
            worker_owner="scope-boundary-worker",
            conversation_intent_classifier=classifier,
        )
        parent = await lifecycle.SubmitRun(
            SubmitRunCommand(
                principal_id="principal-scope",
                session_id=None,
                task_kind="GENERATE_PLAN",
                input=self.profile(),
                idempotency_key="parent-scope-plan",
            )
        )
        parent_result_version = await self.publish_minimal_formal(
            parent.run_id,
            parent.session_id,
        )

        off_topic = await lifecycle.SubmitConversationMessage(
            principal_id="principal-scope",
            session_id=parent.session_id,
            profile=self.profile(),
            parent_result_version=parent_result_version,
            message_text="今天天气怎么样？",
            idempotency_key="scope-off-topic",
        )
        out_of_scope = await lifecycle.SubmitConversationMessage(
            principal_id="principal-scope",
            session_id=parent.session_id,
            profile=self.profile(),
            parent_result_version=parent_result_version,
            message_text="我想申请国外研究生",
            idempotency_key="scope-out-of-scope",
        )

        self.assertIsNone(off_topic.run_id)
        self.assertEqual(off_topic.status, "off_topic")
        self.assertIn("湖南 2025 本科批", off_topic.assistant_message)
        self.assertIsNone(out_of_scope.run_id)
        self.assertEqual(out_of_scope.status, "out_of_scope")
        self.assertIn("当前仅支持", out_of_scope.assistant_message)
        self.assertEqual(
            classifier.messages,
            ["今天天气怎么样？", "我想申请国外研究生"],
        )
        session = await lifecycle.GetSession(
            principal_id="principal-scope",
            session_id=parent.session_id,
        )
        self.assertEqual(
            [message.content for message in session.messages[-4:]],
            [
                "今天天气怎么样？",
                off_topic.assistant_message,
                "我想申请国外研究生",
                out_of_scope.assistant_message,
            ],
        )

    async def test_crisis_message_uses_safety_response_without_classifier(self):
        from app.run_lifecycle import RunLifecycle, SubmitRunCommand

        class FailingClassifier:
            async def classify(self, request):
                raise AssertionError("crisis text must not call classifier")

        await self.seed_release()
        lifecycle = RunLifecycle(
            self.dsn,
            worker_owner="safety-boundary-worker",
            conversation_intent_classifier=FailingClassifier(),
        )
        parent = await lifecycle.SubmitRun(
            SubmitRunCommand(
                principal_id="principal-safety",
                session_id=None,
                task_kind="GENERATE_PLAN",
                input=self.profile(),
                idempotency_key="parent-safety-plan",
            )
        )
        parent_result_version = await self.publish_minimal_formal(
            parent.run_id,
            parent.session_id,
        )
        response = await lifecycle.SubmitConversationMessage(
            principal_id="principal-safety",
            session_id=parent.session_id,
            profile=self.profile(),
            parent_result_version=parent_result_version,
            message_text="我不想活了",
            idempotency_key="scope-safety",
        )

        self.assertIsNone(response.run_id)
        self.assertEqual(response.status, "safety_support")
        self.assertIn("立即联系", response.assistant_message)
        self.assertIn("120", response.assistant_message)

    async def test_province_negation_creates_revision_with_hard_filter(self):
        from app.run_lifecycle import RunLifecycle, SubmitRunCommand

        await self.seed_release()
        lifecycle = RunLifecycle(
            self.dsn,
            worker_owner="province-revision-worker",
        )
        parent = await lifecycle.SubmitRun(
            SubmitRunCommand(
                principal_id="principal-province",
                session_id=None,
                task_kind="GENERATE_PLAN",
                input=self.profile(),
                idempotency_key="parent-province-plan",
            )
        )
        parent_result_version = await self.publish_minimal_formal(
            parent.run_id,
            parent.session_id,
        )

        submission = await lifecycle.SubmitConversationMessage(
            principal_id="principal-province",
            session_id=parent.session_id,
            profile=replace(
                self.profile(),
                preferred_cities=("广州",),
            ),
            parent_result_version=parent_result_version,
            message_text="我不想去广东上大学",
            idempotency_key="province-revision-message",
        )
        self.assertEqual(submission.task_kind, "REVISE_PLAN")
        self.assertIsNotNone(submission.run_id)

        await lifecycle.run_due_work_once()
        revised = await lifecycle.GetRun(
            principal_id="principal-province",
            run_id=submission.run_id,
        )
        fields = revised.current_interrupt.payload["fields"]
        self.assertEqual(
            fields["preferences.excluded_provinces"]["value"],
            ["广东"],
        )
        self.assertEqual(
            fields["preferences.preferred_cities"]["value"],
            [],
        )

    async def test_macro_region_requires_provinces_before_revision(self):
        from app.run_lifecycle import (
            RegionAnswer,
            RunLifecycle,
            SubmitRunCommand,
        )
        from app.run_lifecycle.macro_region import default_catalog

        class FailingClassifier:
            async def classify(self, request):
                raise AssertionError("macro-region clarification must stay deterministic")

        await self.seed_release()
        lifecycle = RunLifecycle(
            self.dsn,
            worker_owner="macro-region-worker",
            conversation_intent_classifier=FailingClassifier(),
        )
        parent = await lifecycle.SubmitRun(
            SubmitRunCommand(
                principal_id="principal-macro-region",
                session_id=None,
                task_kind="GENERATE_PLAN",
                input=self.profile(),
                idempotency_key="parent-macro-region-plan",
            )
        )
        parent_result_version = await self.publish_minimal_formal(
            parent.run_id,
            parent.session_id,
        )

        clarification = await lifecycle.SubmitConversationMessage(
            principal_id="principal-macro-region",
            session_id=parent.session_id,
            profile=self.profile(),
            parent_result_version=parent_result_version,
            message_text="我不想去北方读大学",
            idempotency_key="macro-region-clarify",
        )

        self.assertEqual(clarification.status, "clarification_required")
        self.assertIsNone(clarification.run_id)
        self.assertIsNotNone(clarification.region_clarification)
        payload = clarification.region_clarification
        self.assertEqual(payload["reason_code"], "MACRO_REGION_SCOPE_UNRESOLVED")
        self.assertEqual(payload["operation"], "exclude")
        repeated = await lifecycle.SubmitConversationMessage(
            principal_id="principal-macro-region",
            session_id=parent.session_id,
            profile=self.profile(),
            parent_result_version=parent_result_version,
            message_text="就北方，不要问了",
            idempotency_key="macro-region-repeat",
        )
        self.assertEqual(repeated.status, "clarification_required")
        self.assertIsNone(repeated.run_id)
        self.assertEqual(
            repeated.region_clarification["clarification_id"],
            payload["clarification_id"],
        )
        catalog = default_catalog()
        selected_presets = ("northeast", "north_china")
        provinces = ("辽宁", "吉林", "黑龙江", "北京", "天津", "河北", "山西", "内蒙古")

        submission = await lifecycle.SubmitConversationMessage(
            principal_id="principal-macro-region",
            session_id=parent.session_id,
            profile=self.profile(),
            parent_result_version=parent_result_version,
            message_text="确认排除以上省份",
            idempotency_key="macro-region-resolve",
            region_answer=RegionAnswer(
                clarification_id=payload["clarification_id"],
                provinces=provinces,
                preset_ids=selected_presets,
                catalog_version=catalog.version,
            ),
        )

        self.assertEqual(submission.task_kind, "REVISE_PLAN")
        self.assertIsNotNone(submission.run_id)
        await lifecycle.run_due_work_once()
        revised = await lifecycle.GetRun(
            principal_id="principal-macro-region",
            run_id=submission.run_id,
        )
        fields = revised.current_interrupt.payload["fields"]
        self.assertEqual(
            set(fields["preferences.excluded_provinces"]["value"]),
            set(provinces),
        )

    async def test_macro_region_can_be_explicitly_cancelled(self):
        from app.run_lifecycle import RunLifecycle, SubmitRunCommand

        await self.seed_release()
        lifecycle = RunLifecycle(self.dsn, worker_owner="macro-cancel-worker")
        parent = await lifecycle.SubmitRun(
            SubmitRunCommand(
                principal_id="principal-macro-cancel",
                session_id=None,
                task_kind="GENERATE_PLAN",
                input=self.profile(),
                idempotency_key="parent-macro-cancel-plan",
            )
        )
        parent_result_version = await self.publish_minimal_formal(
            parent.run_id,
            parent.session_id,
        )
        asking = await lifecycle.SubmitConversationMessage(
            principal_id="principal-macro-cancel",
            session_id=parent.session_id,
            profile=self.profile(),
            parent_result_version=parent_result_version,
            message_text="我不想去北方读大学",
            idempotency_key="macro-cancel-clarify",
        )
        self.assertIsNone(asking.run_id)

        cancelled = await lifecycle.SubmitConversationMessage(
            principal_id="principal-macro-cancel",
            session_id=parent.session_id,
            profile=self.profile(),
            parent_result_version=parent_result_version,
            message_text="先不管这个地域限制",
            idempotency_key="macro-cancel-confirm",
        )
        self.assertEqual(cancelled.status, "clarification_resolved")
        self.assertIsNone(cancelled.run_id)
        session = await lifecycle.GetSession(
            principal_id="principal-macro-cancel",
            session_id=parent.session_id,
        )
        self.assertEqual(
            session.profile_fields.get("preferences.excluded_provinces", {}).get(
                "value",
                [],
            ),
            [],
        )

    async def test_resolved_initial_region_does_not_reopen_for_major_exclusion(self):
        from app.run_lifecycle import RunLifecycle, SubmitRunCommand
        from app.run_lifecycle.macro_region import default_catalog

        await self.seed_release()
        lifecycle = RunLifecycle(self.dsn, worker_owner="stale-region-worker")
        parent = await lifecycle.SubmitRun(
            SubmitRunCommand(
                principal_id="principal-stale-region",
                session_id=None,
                task_kind="GENERATE_PLAN",
                input=self.profile(),
                idempotency_key="parent-stale-region-plan",
            )
        )
        parent_result_version = await self.publish_minimal_formal(
            parent.run_id,
            parent.session_id,
        )
        clarification_id = str(uuid4())
        clarification = {
            "clarification_id": clarification_id,
            "clarification_kind": "SELECT_PROVINCES",
            "reason_code": "MACRO_REGION_SCOPE_UNRESOLVED",
            "term": "北方",
            "operation": "exclude",
            "catalog_version": default_catalog().version,
            "provinces": list(default_catalog().provinces),
            "presets": [],
        }
        conn = await self._connect()
        try:
            await conn.execute(
                """
                INSERT INTO chat_message (
                    message_id, session_id, run_id, client_message_id,
                    role, kind, content, metadata
                )
                VALUES (
                    %s, %s, %s, 'region-prompt',
                    'ASSISTANT', 'TEXT',
                    '“北方”的覆盖范围并不唯一。',
                    %s
                )
                """,
                (
                    uuid4(),
                    parent.session_id,
                    parent.run_id,
                    Jsonb(
                        {
                            "clarification_kind": "SELECT_PROVINCES",
                            "region_clarification": clarification,
                        }
                    ),
                ),
            )
            await conn.execute(
                """
                INSERT INTO run_interrupt (
                    interrupt_id, run_id, kind, state, question_key,
                    payload_version, payload, answer_schema_version,
                    checkpoint_id, expires_at, answer_payload, applied_at
                )
                VALUES (
                    %s, %s, 'INTAKE_ASK', 'CONSUMED',
                    'profile.region.select_provinces', 1, %s, 1,
                    'test-checkpoint', NOW() + INTERVAL '1 hour', %s, NOW()
                )
                """,
                (
                    uuid4(),
                    parent.run_id,
                    Jsonb({"region_clarification": clarification}),
                    Jsonb(
                        {
                            "type": "SELECT_PROVINCES",
                            "clarification_id": clarification_id,
                            "provinces": [
                                "北京",
                                "天津",
                                "河北",
                                "山西",
                                "内蒙古",
                            ],
                            "selected_preset_ids": ["north_china"],
                            "region_catalog_version": default_catalog().version,
                            "cancel_region_restriction": False,
                        }
                    ),
                ),
            )
        finally:
            await conn.close()

        submission = await lifecycle.SubmitConversationMessage(
            principal_id="principal-stale-region",
            session_id=parent.session_id,
            profile=self.profile(),
            parent_result_version=parent_result_version,
            message_text="不想读师范",
            idempotency_key="stale-region-major-exclusion",
        )

        self.assertEqual(submission.status, "accepted")
        self.assertEqual(submission.task_kind, "REVISE_PLAN")
        self.assertIsNone(submission.region_clarification)
        await lifecycle.run_due_work_once()
        revised = await lifecycle.GetRun(
            principal_id="principal-stale-region",
            run_id=submission.run_id,
        )
        fields = revised.current_interrupt.payload["fields"]
        self.assertEqual(
            fields["preferences.excluded_major_keywords"]["value"],
            ["师范"],
        )

    async def test_llm_unavailable_clarification_records_degradation(self):
        from app.run_lifecycle import RunLifecycle, SubmitRunCommand

        class UnavailableIntentClassifier:
            async def classify(self, request):
                raise RuntimeError("provider unavailable")

        await self.seed_release()
        lifecycle = RunLifecycle(
            self.dsn,
            worker_owner="llm-unavailable-worker",
            conversation_intent_classifier=UnavailableIntentClassifier(),
        )
        parent = await lifecycle.SubmitRun(
            SubmitRunCommand(
                principal_id="principal-intent-down",
                session_id=None,
                task_kind="GENERATE_PLAN",
                input=self.profile(),
                idempotency_key="parent-intent-down-plan",
            )
        )
        parent_result_version = await self.publish_minimal_formal(
            parent.run_id,
            parent.session_id,
        )

        submission = await lifecycle.SubmitConversationMessage(
            principal_id="principal-intent-down",
            session_id=parent.session_id,
            profile=self.profile(),
            parent_result_version=parent_result_version,
            message_text="北京还是不太合适",
            idempotency_key="intent-down-message",
        )
        self.assertEqual(submission.status, "clarification_required")
        session = await lifecycle.GetSession(
            principal_id="principal-intent-down",
            session_id=parent.session_id,
        )
        assistant = [
            message
            for message in session.messages
            if message.role == "ASSISTANT"
        ][-1]
        self.assertEqual(
            assistant.metadata["generation_mode"],
            "DETERMINISTIC_FALLBACK",
        )
        self.assertEqual(
            assistant.metadata["degradation_reason"],
            "LLM_UNAVAILABLE",
        )

    async def publish_minimal_formal(self, run_id, session_id):
        profile_version = uuid4()
        result_version = uuid4()
        conn = await self._connect()
        try:
            await conn.execute(
                """
                INSERT INTO profile_revision (
                    profile_version, session_id, run_id, revision_no,
                    lifecycle, fields, confirmed_at
                )
                VALUES (%s, %s, %s, 1, 'CONFIRMED', %s, NOW())
                """,
                (
                    profile_version,
                    session_id,
                    run_id,
                    Jsonb(
                        {
                            "province": {
                                "state": "PROVIDED",
                                "source": "USER_FORM",
                                "value": "湖南",
                            },
                            "application_year": {
                                "state": "PROVIDED",
                                "source": "USER_FORM",
                                "value": 2025,
                            },
                            "category": {
                                "state": "PROVIDED",
                                "source": "USER_FORM",
                                "value": "物理类",
                            },
                            "batch": {
                                "state": "DEFAULT",
                                "source": "SYSTEM_DEFAULT",
                                "value": "本科批",
                            },
                            "score": {
                                "state": "PROVIDED",
                                "source": "USER_FORM",
                                "value": 612,
                            },
                            "subjects": {
                                "state": "PROVIDED",
                                "source": "USER_FORM",
                                "value": ["物理", "化学", "生物"],
                            },
                        }
                    ),
                ),
            )
            await conn.execute(
                """
                INSERT INTO run_result (
                    result_version, run_id, release_id,
                    confirmed_profile_version, result_kind, payload,
                    verification_summary, unverified_conditions,
                    source_package_sha256, evidence_package_sha256,
                    verification_report_sha256
                )
                VALUES (
                    %s, %s, 'rel_legacy_v1', %s, 'V1_FORMAL', %s,
                    '[]'::jsonb, '[]'::jsonb, %s, %s, %s
                )
                """,
                (
                    result_version,
                    run_id,
                    profile_version,
                    Jsonb(
                        {
                            "report": {
                                "sections": [
                                    {
                                        "section_key": "candidate:1001|03",
                                        "title": "1. 中南大学 03专业组",
                                        "body": "录取概率约 34%，策略分档：冲。",
                                    }
                                ]
                            },
                            "risk_notice": "概率为历史估计。",
                        }
                    ),
                    "a" * 64,
                    "b" * 64,
                    "c" * 64,
                ),
            )
            await conn.execute(
                """
                INSERT INTO run_result_state_event (
                    result_version, status, reason
                )
                VALUES (%s, 'ACTIVE', NULL)
                """,
                (result_version,),
            )
            await conn.execute(
                """
                UPDATE run
                SET state = 'SUCCEEDED',
                    stage = 'FORMAL_READY',
                    confirmed_profile_version = %s,
                    result_version = %s,
                    result_kind = 'V1_FORMAL',
                    state_version = state_version + 1
                WHERE run_id = %s
                """,
                (profile_version, result_version, run_id),
            )
        finally:
            await conn.close()
        return result_version

    @staticmethod
    def profile():
        from app.run_lifecycle import StructuredProfileInput

        return StructuredProfileInput(
            province="湖南",
            application_year=2025,
            category="物理类",
            batch="本科批",
            score=612,
            subjects=("物理", "化学", "生物"),
        )


if __name__ == "__main__":
    import unittest

    unittest.main()
