"""大区表达到受控省份集合的唯一解析与校验入口。"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

import psycopg


PROVINCES = (
    "北京", "天津", "河北", "山西", "内蒙古", "辽宁", "吉林",
    "黑龙江", "上海", "江苏", "浙江", "安徽", "福建", "江西",
    "山东", "河南", "湖北", "湖南", "广东", "广西", "海南",
    "重庆", "四川", "贵州", "云南", "西藏", "陕西", "甘肃",
    "青海", "宁夏", "新疆",
)

REGION_PRESETS: tuple[dict[str, Any], ...] = (
    {
        "id": "northeast",
        "name": "东北",
        "provinces": ["辽宁", "吉林", "黑龙江"],
    },
    {
        "id": "north_china",
        "name": "华北",
        "provinces": ["北京", "天津", "河北", "山西", "内蒙古"],
    },
    {
        "id": "east_china",
        "name": "华东",
        "provinces": ["上海", "江苏", "浙江", "安徽", "福建", "江西", "山东"],
    },
    {
        "id": "central_china",
        "name": "华中",
        "provinces": ["河南", "湖北", "湖南"],
    },
    {
        "id": "south_china",
        "name": "华南",
        "provinces": ["广东", "广西", "海南"],
    },
    {
        "id": "southwest_china",
        "name": "西南",
        "provinces": ["重庆", "四川", "贵州", "云南", "西藏"],
    },
    {
        "id": "northwest_china",
        "name": "西北",
        "provinces": ["陕西", "甘肃", "青海", "宁夏", "新疆"],
    },
)

DEFAULT_CATALOG_VERSION = "region-seven-v1"
MACRO_REGION_SCOPE_UNRESOLVED = "MACRO_REGION_SCOPE_UNRESOLVED"
MACRO_REGION_CATALOG_UNAVAILABLE = "MACRO_REGION_CATALOG_UNAVAILABLE"

_MACRO_TERMS = (
    "北方地区",
    "南方地区",
    "北方",
    "南方",
    "东北地区",
    "东北",
    "华北地区",
    "华北",
    "华东地区",
    "华东",
    "华中地区",
    "华中",
    "华南地区",
    "华南",
    "西南地区",
    "西南",
    "西北地区",
    "西北",
    "东部地区",
    "东部",
    "西部地区",
    "西部",
)

_EXCLUDE_ACTION = re.compile(
    r"(?:不想去|不想在|不要去|不去|不想报|不考虑|不要|排除|避开)"
)
_REQUIRE_ACTION = re.compile(
    r"(?:只看|只要|仅限|只考虑|必须(?:在|去)|只去)"
)
_WITHDRAW_ACTION = re.compile(
    r"(?:也可以|能接受|可以考虑|可以接受|可以了|不限制了)"
)


@dataclass(frozen=True, slots=True)
class MacroRegionExpression:
    term: str
    operation: str


@dataclass(frozen=True, slots=True)
class RegionCatalog:
    version: str
    provinces: tuple[str, ...]
    presets: tuple[dict[str, Any], ...]

    def payload(self) -> dict[str, Any]:
        return {
            "catalog_version": self.version,
            "provinces": list(self.provinces),
            "presets": [
                {
                    "id": preset["id"],
                    "name": preset["name"],
                    "provinces": list(preset["provinces"]),
                }
                for preset in self.presets
            ],
        }


class MacroRegionError(RuntimeError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def default_catalog() -> RegionCatalog:
    return RegionCatalog(
        version=DEFAULT_CATALOG_VERSION,
        provinces=PROVINCES,
        presets=tuple(
            {
                "id": preset["id"],
                "name": preset["name"],
                "provinces": tuple(preset["provinces"]),
            }
            for preset in REGION_PRESETS
        ),
    )


def detect_macro_region(text: str) -> MacroRegionExpression | None:
    """只识别带明确筛选动作的大区表达，避免把校名中的“华北”误判为地域条件。"""
    normalized = text.strip()
    if not normalized:
        return None
    for term in _MACRO_TERMS:
        escaped = re.escape(term)
        if re.search(
            rf"{escaped}(?!地区|的|省|市|自治区|读|上|去|在|想)"
            r"[\u4e00-\u9fff]{0,8}(?:大学|学院|学校)",
            normalized,
        ):
            continue
        patterns = (
            (_EXCLUDE_ACTION, "exclude"),
            (_REQUIRE_ACTION, "require"),
            (_WITHDRAW_ACTION, "withdraw"),
        )
        for action, operation in patterns:
            if re.search(
                rf"{action.pattern}\s*{escaped}(?:地区)?",
                normalized,
            ):
                return MacroRegionExpression(term=term, operation=operation)
        if re.search(
            rf"{escaped}(?:地区)?.{{0,10}}(?:太远|不想去|不去|不要|不考虑|排除|可以了|不限制了)",
            normalized,
        ):
            return MacroRegionExpression(term=term, operation="exclude")
        if re.search(
            rf"{escaped}(?:地区)?.{{0,10}}(?:也可以|能接受|可以考虑|可以接受)",
            normalized,
        ):
            return MacroRegionExpression(term=term, operation="withdraw")
    return None


def mentions_macro_region(text: str) -> bool:
    return any(term in text for term in _MACRO_TERMS)


def clarification_payload(
    *,
    clarification_id: str,
    expression: MacroRegionExpression,
    catalog: RegionCatalog,
) -> dict[str, Any]:
    return {
        "clarification_id": clarification_id,
        "clarification_kind": "SELECT_PROVINCES",
        "reason_code": MACRO_REGION_SCOPE_UNRESOLVED,
        "term": expression.term,
        "operation": expression.operation,
        "catalog_version": catalog.version,
        "provinces": list(catalog.provinces),
        "presets": [
            {
                "id": preset["id"],
                "name": preset["name"],
                "provinces": list(preset["provinces"]),
            }
            for preset in catalog.presets
        ],
    }


def validate_region_answer(
    *,
    catalog: RegionCatalog,
    catalog_version: str,
    provinces: tuple[str, ...],
    preset_ids: tuple[str, ...],
    cancel_region_restriction: bool,
) -> tuple[str, ...]:
    if catalog_version != catalog.version:
        raise MacroRegionError(MACRO_REGION_CATALOG_UNAVAILABLE)
    if cancel_region_restriction:
        if provinces or preset_ids:
            raise MacroRegionError("REGION_ANSWER_INVALID")
        return ()
    if not provinces:
        raise MacroRegionError("REGION_ANSWER_INVALID")
    allowed = set(catalog.provinces)
    if len(set(provinces)) != len(provinces) or any(
        province not in allowed for province in provinces
    ):
        raise MacroRegionError("REGION_ANSWER_INVALID")
    by_id = {preset["id"]: preset for preset in catalog.presets}
    if len(set(preset_ids)) != len(preset_ids) or any(
        preset_id not in by_id for preset_id in preset_ids
    ):
        raise MacroRegionError("REGION_ANSWER_INVALID")
    preset_provinces = {
        province
        for preset_id in preset_ids
        for province in by_id[preset_id]["provinces"]
    }
    if not preset_provinces.issubset(provinces):
        raise MacroRegionError("REGION_ANSWER_INVALID")
    return provinces


async def load_region_catalog(
    conn: psycopg.AsyncConnection,
    release_id: str,
) -> RegionCatalog:
    row = await (
        await conn.execute(
            """
            SELECT catalog_version, provinces, presets
            FROM region_preset_catalog
            WHERE release_id = %s
            """,
            (release_id,),
        )
    ).fetchone()
    if row is None:
        raise MacroRegionError(MACRO_REGION_CATALOG_UNAVAILABLE)
    provinces = tuple(row["provinces"] or ())
    presets = tuple(
        {
            "id": str(preset.get("id") or ""),
            "name": str(preset.get("name") or ""),
            "provinces": tuple(preset.get("provinces") or ()),
        }
        for preset in (row["presets"] or ())
    )
    if (
        not provinces
        or len(set(provinces)) != len(provinces)
        or not presets
    ):
        raise MacroRegionError(MACRO_REGION_CATALOG_UNAVAILABLE)
    return RegionCatalog(
        version=str(row["catalog_version"]),
        provinces=provinces,
        presets=presets,
    )
