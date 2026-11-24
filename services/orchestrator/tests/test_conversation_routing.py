"""报告后继续对话的意图路由与筛选条件解析。"""
from __future__ import annotations

import unittest


class ConversationRoutingTests(unittest.TestCase):
    def test_explanation_question_does_not_create_a_revision(self):
        from app.run_lifecycle.service import RunLifecycle

        self.assertEqual(
            RunLifecycle._conversation_decision("为什么这所学校排在前面？"),
            "EXPLAIN_RESULT",
        )

    def test_question_word_alone_does_not_mean_report_explanation(self):
        from app.run_lifecycle.service import RunLifecycle

        self.assertEqual(
            RunLifecycle._conversation_decision("今天天气怎么样？"),
            "CLARIFY",
        )
        self.assertEqual(
            RunLifecycle._conversation_decision(
                "湖南2025本科批平行志愿规则是什么？"
            ),
            "CLARIFY",
        )

    def test_filter_language_creates_a_revision(self):
        from app.run_lifecycle.service import RunLifecycle

        self.assertEqual(
            RunLifecycle._conversation_decision("不要武汉，只看计算机"),
            "REVISE_PLAN",
        )
        self.assertEqual(
            RunLifecycle._conversation_decision("再稳一点，增加保底"),
            "REVISE_PLAN",
        )
        self.assertEqual(
            RunLifecycle._conversation_decision(
                "我不想去北京上大学，太远了"
            ),
            "REVISE_PLAN",
        )
        self.assertEqual(
            RunLifecycle._conversation_decision("我不想学物理学专业呢？"),
            "REVISE_PLAN",
        )

    def test_ambiguous_message_requests_clarification(self):
        from app.run_lifecycle.service import RunLifecycle

        self.assertEqual(
            RunLifecycle._conversation_decision("我再想想"),
            "CLARIFY",
        )

    def test_constraint_parser_updates_hard_filters_and_risk(self):
        from app.run_lifecycle import StructuredProfileInput
        from app.run_lifecycle.service import RunLifecycle

        updated = RunLifecycle._apply_conversation_constraints(
            StructuredProfileInput(
                province="湖南",
                application_year=2025,
                category="物理类",
                batch="本科批",
                score=612,
                subjects=("物理", "化学", "生物"),
                preferred_cities=("武汉",),
            ),
            "不要武汉，只看长沙和计算机，更稳一点",
        )

        self.assertIn("武汉", updated.excluded_cities)
        self.assertIn("长沙", updated.required_cities)
        self.assertIn("计算机", updated.required_major_keywords)
        self.assertEqual(updated.risk_appetite, "conservative")

    def test_exact_major_negation_does_not_become_a_broad_keyword(self):
        from app.run_lifecycle import StructuredProfileInput
        from app.run_lifecycle.service import RunLifecycle

        updated = RunLifecycle._apply_conversation_constraints(
            StructuredProfileInput(
                province="湖南",
                application_year=2025,
                category="物理类",
                batch="本科批",
                score=600,
                subjects=("物理", "化学", "生物"),
            ),
            "我不想学物理学专业呢？",
        )

        self.assertEqual(updated.excluded_major_names, ("物理学",))
        self.assertEqual(updated.excluded_major_keywords, ())

    def test_teacher_keyword_negation_uses_major_keyword_exclusion(self):
        from app.run_lifecycle import StructuredProfileInput
        from app.run_lifecycle.service import RunLifecycle

        updated = RunLifecycle._apply_conversation_constraints(
            StructuredProfileInput(
                province="湖南",
                application_year=2025,
                category="历史类",
                batch="本科批",
                score=600,
                subjects=("历史", "政治", "地理"),
            ),
            "不想读师范",
        )

        self.assertEqual(updated.excluded_major_keywords, ("师范",))

    def test_city_negation_accepts_natural_wording(self):
        from app.run_lifecycle import StructuredProfileInput
        from app.run_lifecycle.service import RunLifecycle

        updated = RunLifecycle._apply_conversation_constraints(
            StructuredProfileInput(
                province="湖南",
                application_year=2025,
                category="物理类",
                batch="本科批",
                score=600,
                subjects=("物理", "化学", "生物"),
                preferred_cities=("北京", "长沙"),
            ),
            "我不想去北京上大学，太远了",
        )

        self.assertEqual(updated.excluded_cities, ("北京",))
        self.assertNotIn("北京", updated.preferred_cities)

    def test_province_negation_updates_province_hard_filter(self):
        from app.run_lifecycle import StructuredProfileInput
        from app.run_lifecycle.service import RunLifecycle

        updated = RunLifecycle._apply_conversation_constraints(
            StructuredProfileInput(
                province="湖南",
                application_year=2025,
                category="历史类",
                batch="本科批",
                score=600,
                subjects=("历史", "政治", "地理"),
                preferred_cities=("广州",),
            ),
            "我不想去广东上大学",
        )

        self.assertEqual(updated.excluded_provinces, ("广东",))
        self.assertEqual(updated.preferred_cities, ())
        self.assertEqual(updated.excluded_cities, ())

    def test_province_llm_patch_is_validated_and_applied(self):
        from app.run_lifecycle import StructuredProfileInput
        from app.run_lifecycle.service import RunLifecycle

        updated = RunLifecycle._apply_conversation_patch(
            StructuredProfileInput(
                province="湖南",
                application_year=2025,
                category="历史类",
                batch="本科批",
                score=600,
                subjects=("历史", "政治", "地理"),
                preferred_cities=("广州",),
            ),
            {"excluded_provinces": ["广东"]},
        )

        self.assertEqual(updated.excluded_provinces, ("广东",))
        self.assertEqual(updated.preferred_cities, ())

    def test_explicit_province_requirement_updates_hard_filter(self):
        from app.run_lifecycle import StructuredProfileInput
        from app.run_lifecycle.service import RunLifecycle

        updated = RunLifecycle._apply_conversation_constraints(
            StructuredProfileInput(
                province="湖南",
                application_year=2025,
                category="历史类",
                batch="本科批",
                score=600,
                subjects=("历史", "政治", "地理"),
                excluded_provinces=("广东",),
            ),
            "只看广东",
        )

        self.assertEqual(updated.required_provinces, ("广东",))
        self.assertEqual(updated.excluded_provinces, ())

    def test_ambiguous_region_does_not_create_a_guessed_patch(self):
        from app.run_lifecycle import StructuredProfileInput
        from app.run_lifecycle.service import RunLifecycle

        profile = StructuredProfileInput(
            province="湖南",
            application_year=2025,
            category="历史类",
            batch="本科批",
            score=600,
            subjects=("历史", "政治", "地理"),
        )
        updated = RunLifecycle._apply_conversation_constraints(
            profile,
            "我不想去华南上大学",
        )

        self.assertEqual(updated, profile)

    def test_llm_patch_is_validated_and_applied_deterministically(self):
        from app.run_lifecycle import StructuredProfileInput
        from app.run_lifecycle.service import RunLifecycle

        updated = RunLifecycle._apply_conversation_patch(
            StructuredProfileInput(
                province="湖南",
                application_year=2025,
                category="物理类",
                batch="本科批",
                score=600,
                subjects=("物理", "化学", "生物"),
                preferred_cities=("北京", "长沙"),
            ),
            {
                "excluded_cities": ["北京"],
                "risk_appetite": "conservative",
            },
        )

        self.assertEqual(updated.excluded_cities, ("北京",))
        self.assertEqual(updated.preferred_cities, ("长沙",))
        self.assertEqual(updated.risk_appetite, "conservative")

    def test_llm_patch_rejects_unknown_fields(self):
        from app.run_lifecycle import (
            RunLifecycle,
            RunLifecycleError,
            StructuredProfileInput,
        )

        with self.assertRaises(RunLifecycleError) as raised:
            RunLifecycle._apply_conversation_patch(
                StructuredProfileInput(
                    province="湖南",
                    application_year=2025,
                    category="物理类",
                    batch="本科批",
                    score=600,
                    subjects=("物理", "化学", "生物"),
                ),
                {"candidate_identities": ["U001"]},
            )

        self.assertEqual(
            raised.exception.reason,
            "CONVERSATION_PATCH_INVALID",
        )


if __name__ == "__main__":
    unittest.main()
