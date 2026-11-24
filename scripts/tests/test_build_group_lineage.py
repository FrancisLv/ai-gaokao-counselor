from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "build_group_lineage.py"
SPEC = importlib.util.spec_from_file_location(
    "build_group_lineage",
    SCRIPT_PATH,
)
assert SPEC is not None and SPEC.loader is not None
lineage = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = lineage
SPEC.loader.exec_module(lineage)


class SelectLineageMatchTests(unittest.TestCase):
    def profile(
        self,
        major_group: str,
        names: set[str],
        subjects: set[str] | None = None,
    ) -> object:
        return lineage.GroupProfile(
            university_code="6201",
            major_group=major_group,
            subjects=frozenset(subjects or {"物理"}),
            names=frozenset(names),
        )

    def test_exact_code_with_disjoint_majors_is_not_mapped(self):
        target = self.profile(
            "（110）",
            {"数学类", "计算机类", "电子信息类", "物理学类"},
        )
        reused_code = self.profile(
            "（110）",
            {"临床医学", "口腔医学", "药学", "预防医学"},
        )
        low_overlap = self.profile(
            "（109）",
            {"数学类", "计算机类", "生物科学类"},
        )

        match = lineage.select_lineage_match(
            target,
            [reused_code, low_overlap],
            name_threshold=0.6,
        )

        self.assertIsNone(match)

    def test_reused_code_maps_to_the_semantically_matching_group(self):
        target = self.profile(
            "（110）",
            {"数学类", "计算机类", "电子信息类", "物理学类"},
        )
        reused_code = self.profile(
            "（110）",
            {"临床医学", "口腔医学", "药学", "预防医学"},
        )
        semantic_match = self.profile(
            "（109）",
            {"数学类", "计算机类", "电子信息类", "物理学类", "化学类"},
        )

        match = lineage.select_lineage_match(
            target,
            [reused_code, semantic_match],
            name_threshold=0.6,
        )

        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match.historical.major_group, "（109）")
        self.assertEqual(match.method, "MAJOR_NAME_JACCARD")
        self.assertEqual(match.confidence, 0.8)

    def test_exact_code_with_strong_overlap_remains_exact(self):
        target = self.profile("（110）", {"数学类", "计算机类", "物理学类"})
        exact = self.profile("（110）", {"数学类", "计算机类", "物理学类"})

        match = lineage.select_lineage_match(
            target,
            [exact],
            name_threshold=0.6,
        )

        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match.method, "EXACT_CODE")
        self.assertEqual(match.confidence, 1.0)

    def test_ambiguous_semantic_candidates_are_not_mapped(self):
        target = self.profile("（110）", {"数学类", "计算机类"})
        first = self.profile("（109）", {"数学类", "计算机类", "化学类"})
        second = self.profile("（311）", {"数学类", "计算机类", "物理学类"})

        match = lineage.select_lineage_match(
            target,
            [first, second],
            name_threshold=0.6,
        )

        self.assertIsNone(match)


if __name__ == "__main__":
    unittest.main()
