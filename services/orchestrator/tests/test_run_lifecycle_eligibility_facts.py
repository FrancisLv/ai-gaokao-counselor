"""M4b conditional profile facts preserve confirmed revision state."""
from __future__ import annotations

import unittest


class EligibilityProfileFactsTests(unittest.TestCase):
    def test_confirmed_fields_become_deterministic_typed_facts(self):
        from app.run_lifecycle.profile import ProfileRuntime

        fields = {
            "subjects": {
                "state": "PROVIDED",
                "source": "USER_FORM",
                "value": ["物理", "化学", "生物"],
            },
            "category": {
                "state": "PROVIDED",
                "source": "USER_FORM",
                "value": "物理类",
            },
            "single_subject_scores.数学": {
                "state": "PROVIDED",
                "source": "USER_FORM",
                "value": 132,
            },
            "physical_exam.color_vision": {
                "state": "PROVIDED",
                "source": "USER_FORM",
                "value": "NORMAL",
            },
            "gender": {
                "state": "DECLINED",
                "source": "USER_FORM",
                "value": None,
            },
            "household_province": {
                "state": "PROVIDED",
                "source": "USER_FORM",
                "value": "湖南",
            },
            "school_registration_province": {
                "state": "PROVIDED",
                "source": "USER_FORM",
                "value": "湖南",
            },
        }

        facts = ProfileRuntime().ToEligibilityFacts(
            lifecycle="CONFIRMED",
            fields=fields,
        )

        self.assertEqual(
            facts,
            [
                {
                    "field_key": "category",
                    "state": "PROVIDED",
                    "value": "物理类",
                },
                {
                    "field_key": "gender",
                    "state": "DECLINED",
                    "value": None,
                },
                {
                    "field_key": "household_province",
                    "state": "PROVIDED",
                    "value": "湖南",
                },
                {
                    "field_key": "physical_exam.color_vision",
                    "state": "PROVIDED",
                    "value": "NORMAL",
                },
                {
                    "field_key": "school_registration_province",
                    "state": "PROVIDED",
                    "value": "湖南",
                },
                {
                    "field_key": "single_subject_scores.数学",
                    "state": "PROVIDED",
                    "value": 132,
                },
                {
                    "field_key": "subjects",
                    "state": "PROVIDED",
                    "value": ["物理", "化学", "生物"],
                },
            ],
        )

    def test_draft_fields_cannot_be_exposed_to_eligibility(self):
        from app.run_lifecycle.models import RunLifecycleError
        from app.run_lifecycle.profile import ProfileRuntime

        with self.assertRaises(RunLifecycleError) as raised:
            ProfileRuntime().ToEligibilityFacts(
                lifecycle="DRAFT",
                fields={
                    "gender": {
                        "state": "PROVIDED",
                        "source": "USER_FORM",
                        "value": "FEMALE",
                    }
                },
            )
        self.assertEqual(raised.exception.reason, "PROFILE_NOT_CONFIRMED")

    def test_profile_snapshot_digest_is_cross_language_canonical(self):
        from app.run_lifecycle.service import RunLifecycle

        facts = [
            {
                "field_key": "subjects",
                "state": "PROVIDED",
                "value": ["物理", "化学", "生物"],
            },
            {
                "field_key": "category",
                "state": "PROVIDED",
                "value": "物理类",
            },
        ]
        digest = RunLifecycle._eligibility_profile_snapshot_sha256(
            "profile-v1",
            facts,
        )
        self.assertEqual(
            digest,
            "f60bc119283ebe04b7ce6df5d3a5cc4edefa4535e2481ac3a9a1444981a465c4",
        )


if __name__ == "__main__":
    unittest.main()
