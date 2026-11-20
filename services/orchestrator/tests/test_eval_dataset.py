"""M5b-03：版本化联合 EvalDataset 的装载与不可变边界。"""
from __future__ import annotations

from pathlib import Path
import json
import shutil
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[3]
DATASET = ROOT / "data" / "golden" / "evaluation-hunan-2025-v0.2.0-draft"


class JointEvalDatasetTests(unittest.TestCase):
    def test_committed_dataset_loads_retrieval_and_end_to_end_cases(self):
        from eval.eval_dataset import load_eval_dataset

        dataset = load_eval_dataset(DATASET)

        self.assertEqual(
            dataset.manifest["dataset_version"],
            "evaluation-hunan-2025-v0.2.0-draft",
        )
        self.assertGreaterEqual(len(dataset.retrieval_cases), 30)
        self.assertEqual(len(dataset.e2e_cases), 10)
        self.assertEqual(
            dataset.manifest["owner_status"],
            "PENDING_SIGNOFF",
        )
        self.assertFalse(dataset.is_authoritative_quality)
        self.assertEqual(
            {case["case_id"] for case in dataset.e2e_cases},
            {f"e2e-{index:03d}" for index in range(1, 11)},
        )

    def test_mutating_a_case_file_invalidates_the_immutable_manifest(self):
        from eval.eval_dataset import load_eval_dataset

        with tempfile.TemporaryDirectory() as temp_dir:
            copied = Path(temp_dir) / DATASET.name
            shutil.copytree(DATASET, copied)
            with (copied / "cases.jsonl").open("a", encoding="utf-8") as stream:
                stream.write("\n")

            with self.assertRaisesRegex(ValueError, "sha256"):
                load_eval_dataset(copied)

    def test_case_count_and_immutable_flag_are_checked(self):
        from eval.eval_dataset import load_eval_dataset

        with tempfile.TemporaryDirectory() as temp_dir:
            copied = Path(temp_dir) / DATASET.name
            shutil.copytree(DATASET, copied)
            manifest_path = copied / "manifest.json"
            original = json.loads(manifest_path.read_text(encoding="utf-8"))

            manifest_path.write_text(
                json.dumps(
                    {**original, "e2e_case_count": 11},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "e2e_case_count"):
                load_eval_dataset(copied)

            manifest_path.write_text(
                json.dumps(
                    {**original, "immutable": False},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "immutable"):
                load_eval_dataset(copied)

            manifest_path.write_text(
                json.dumps(
                    {
                        **original,
                        "dataset_version": "unknown-dataset-version",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "dataset_version"):
                load_eval_dataset(copied)

            manifest_path.write_text(
                json.dumps(
                    {**original, "case_count": 99},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "case_count"):
                load_eval_dataset(copied)

    def test_case_count_is_a_required_manifest_field(self):
        from eval.eval_dataset import load_eval_dataset

        with tempfile.TemporaryDirectory() as temp_dir:
            copied = Path(temp_dir) / DATASET.name
            shutil.copytree(DATASET, copied)
            manifest_path = copied / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest.pop("case_count")
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ValueError,
                "case_count",
            ):
                load_eval_dataset(copied)

    def test_each_end_to_end_case_declares_its_execution_driver(self):
        from eval.eval_dataset import load_eval_dataset

        dataset = load_eval_dataset(DATASET)

        for case in dataset.e2e_cases:
            with self.subTest(case_id=case["case_id"]):
                self.assertIn("driver", case)
                self.assertIn(
                    case["driver"]["resume_action"],
                    {
                        "confirm",
                        "wait_for_interrupt",
                        "edit_score_then_confirm",
                    },
                )
                self.assertIn(
                    case["driver"]["scenario"],
                    {
                        "normal",
                        "missing_required_field",
                        "eligibility_rejected",
                        "conditional_unknown",
                        "no_reliable_data",
                        "fact_tamper",
                        "profile_score_edit",
                        "budget_exhausted",
                        "worker_recovery",
                        "cancel_publish_race",
                    },
                )


if __name__ == "__main__":
    unittest.main()
