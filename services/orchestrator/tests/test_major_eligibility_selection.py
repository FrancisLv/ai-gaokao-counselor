"""资格裁决后重新绑定默认拟填专业。"""
from __future__ import annotations

import unittest

from api.algo.v1 import algo_pb2


class MajorEligibilitySelectionTests(unittest.TestCase):
    def test_failed_default_is_replaced_by_next_eligible_major(self):
        from app.run_lifecycle.service import RunLifecycle

        majors = [
            algo_pb2.MajorDetail(
                major_code=f"M{index:02d}",
                major_name=f"专业{index}",
                recommended=index <= 6,
                recommendation_rank=index if index <= 6 else 0,
            )
            for index in range(1, 8)
        ]
        wishlist = algo_pb2.WishlistResult(
            candidates=[
                algo_pb2.CandidateWithScore(
                    cwp=algo_pb2.CandidateWithProb(
                        candidate=algo_pb2.Candidate(
                            university_code="U001",
                            major_group="01",
                            majors=majors,
                        ),
                        prob=algo_pb2.ProbEstimate(probability=0.67),
                    ),
                    final_rank=1,
                )
            ]
        )
        eligibility = algo_pb2.EvaluateEligibilityReply(
            groups=[
                algo_pb2.GroupEligibility(
                    group_key="U001|01",
                    status=(
                        algo_pb2.GroupEligibilityStatus
                        .GROUP_ELIGIBILITY_STATUS_PASS_PARTIAL
                    ),
                    majors=[
                        algo_pb2.MajorEligibility(
                            major_code=major.major_code,
                            status=(
                                algo_pb2.MajorEligibilityStatus
                                .MAJOR_ELIGIBILITY_STATUS_FAIL
                                if major.major_code == "M01"
                                else algo_pb2.MajorEligibilityStatus
                                .MAJOR_ELIGIBILITY_STATUS_PASS
                            ),
                            failure_rule_keys=(
                                ["SUBJECT_REQUIREMENT"]
                                if major.major_code == "M01"
                                else []
                            ),
                        )
                        for major in majors
                    ],
                )
            ]
        )

        RunLifecycle._apply_major_eligibility_to_wishlist(
            wishlist,
            eligibility,
        )

        updated = wishlist.candidates[0].cwp.candidate.majors
        self.assertFalse(updated[0].selectable)
        self.assertIn("FAIL", updated[0].unavailable_reason)
        recommended = [
            major.major_code
            for major in updated
            if major.recommended
        ]
        self.assertEqual(
            recommended,
            ["M02", "M03", "M04", "M05", "M06", "M07"],
        )
