"""大区表达解析与 release 目录的离线单元测试。"""
from __future__ import annotations

import unittest


class MacroRegionTests(unittest.TestCase):
    def test_default_catalog_has_all_provinces_and_seven_presets(self):
        from app.run_lifecycle.macro_region import default_catalog

        catalog = default_catalog()
        self.assertEqual(len(catalog.provinces), 31)
        self.assertEqual(len(set(catalog.provinces)), 31)
        self.assertEqual(len(catalog.presets), 7)
        self.assertEqual(
            {
                province
                for preset in catalog.presets
                for province in preset["provinces"]
            },
            set(catalog.provinces),
        )

    def test_explicit_macro_region_is_detected_with_operation(self):
        from app.run_lifecycle.macro_region import detect_macro_region

        exclude = detect_macro_region("我不想去北方读大学")
        suffix_exclude = detect_macro_region("北方不要")
        require = detect_macro_region("只看华南的学校")
        withdraw = detect_macro_region("东北也可以")

        self.assertEqual(exclude.term, "北方")
        self.assertEqual(exclude.operation, "exclude")
        self.assertEqual(suffix_exclude.operation, "exclude")
        self.assertEqual(require.term, "华南")
        self.assertEqual(require.operation, "require")
        self.assertEqual(withdraw.term, "东北")
        self.assertEqual(withdraw.operation, "withdraw")

    def test_university_name_alone_does_not_create_region_constraint(self):
        from app.run_lifecycle.macro_region import detect_macro_region

        self.assertIsNone(detect_macro_region("华北电力大学怎么样？"))
        self.assertIsNone(detect_macro_region("帮我解释华北电力大学"))
        self.assertIsNone(detect_macro_region("我不想去华北电力大学"))

    def test_region_answer_uses_catalog_union_and_rejects_unknown_values(self):
        from app.run_lifecycle.macro_region import (
            MacroRegionError,
            default_catalog,
            validate_region_answer,
        )

        catalog = default_catalog()
        provinces = validate_region_answer(
            catalog=catalog,
            catalog_version=catalog.version,
            provinces=("北京", "天津", "河北", "山西", "内蒙古", "辽宁", "吉林", "黑龙江"),
            preset_ids=("northeast", "north_china"),
            cancel_region_restriction=False,
        )
        self.assertEqual(len(provinces), 8)

        with self.assertRaises(MacroRegionError) as raised:
            validate_region_answer(
                catalog=catalog,
                catalog_version=catalog.version,
                provinces=("火星",),
                preset_ids=(),
                cancel_region_restriction=False,
            )
        self.assertEqual(raised.exception.reason, "REGION_ANSWER_INVALID")

    def test_cancel_requires_an_empty_result(self):
        from app.run_lifecycle.macro_region import (
            MacroRegionError,
            default_catalog,
            validate_region_answer,
        )

        catalog = default_catalog()
        self.assertEqual(
            validate_region_answer(
                catalog=catalog,
                catalog_version=catalog.version,
                provinces=(),
                preset_ids=(),
                cancel_region_restriction=True,
            ),
            (),
        )
        with self.assertRaises(MacroRegionError):
            validate_region_answer(
                catalog=catalog,
                catalog_version=catalog.version,
                provinces=("北京",),
                preset_ids=(),
                cancel_region_restriction=True,
            )


if __name__ == "__main__":
    unittest.main()
