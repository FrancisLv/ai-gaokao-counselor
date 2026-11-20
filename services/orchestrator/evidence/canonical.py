"""证据领域共享的规范值与内容摘要。"""
from __future__ import annotations

from dataclasses import fields, is_dataclass
from datetime import date, datetime
from decimal import Decimal
import hashlib
import json
from typing import Any
from uuid import UUID


def normalize_sample_years(years: tuple[int, ...]) -> tuple[int, ...]:
    """样本年份是去重升序集合，顺序不是内容身份。"""
    return tuple(sorted(set(years)))


def canonical_payload(value: Any) -> Any:
    if is_dataclass(value):
        return {
            field.name: canonical_payload(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, dict):
        return {
            str(key): canonical_payload(item)
            for key, item in value.items()
        }
    if isinstance(value, (tuple, list)):
        return [canonical_payload(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Decimal):
        return str(value)
    return value


def sha256_payload(value: Any) -> str:
    encoded = json.dumps(
        canonical_payload(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
