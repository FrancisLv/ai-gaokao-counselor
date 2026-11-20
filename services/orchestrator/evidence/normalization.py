"""证据校验共享的规范值、hash 与文案边界工具。"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import hashlib
import math
import re

from evidence.models import (
    Claim,
    DocumentEvidence,
    EvidenceRef,
    StructuredEvidence,
)


def canonical_scalar(value) -> tuple[str, str | None]:
    if value is None:
        return ("null", None)
    if isinstance(value, bool):
        return ("bool", "true" if value else "false")
    if isinstance(value, int):
        return ("int", str(value))
    if isinstance(value, Decimal):
        if not value.is_finite():
            return ("unsupported", None)
        return ("decimal", plain_decimal(value))
    if isinstance(value, float):
        if not math.isfinite(value):
            return ("unsupported", None)
        return ("float", plain_decimal(Decimal(repr(value))))
    if isinstance(value, str):
        return ("str", value)
    return ("unsupported", None)


def render_derived_value(value, precision: int | None) -> str:
    canonical_type, canonical_value = canonical_derived_value(value)
    if canonical_type == "number":
        decimal = Decimal(canonical_value or "0")
        if precision is not None:
            return f"{decimal:.{precision}f}"
        return plain_decimal(decimal)
    if canonical_type == "bool":
        return "true" if canonical_value == "true" else "false"
    if canonical_type == "null":
        return "null"
    return canonical_value or ""


def canonical_derived_value(value) -> tuple[str, str | None]:
    canonical_type, canonical_value = canonical_scalar(value)
    if canonical_type in {"int", "decimal", "float"}:
        return ("number", plain_decimal(Decimal(canonical_value or "0")))
    if canonical_type == "str" and canonical_value is not None:
        try:
            decimal = Decimal(canonical_value)
        except InvalidOperation:
            return canonical_type, canonical_value
        if not decimal.is_finite():
            return "unsupported", None
        return "number", plain_decimal(decimal)
    return canonical_type, canonical_value


def render_scalar(value) -> str:
    canonical_type, canonical_value = canonical_scalar(value)
    if canonical_type == "null":
        return "null"
    if canonical_type == "unsupported":
        return canonical_value or ""
    return canonical_value or ""


def plain_decimal(value: Decimal) -> str:
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def claim_exceeds_admission_boundary(claim: Claim) -> bool:
    text = " ".join(
        (
            claim.normalized.subject,
            claim.normalized.predicate,
            str(claim.normalized.object or ""),
            claim.rendered_text,
        )
    )
    return text_exceeds_admission_boundary(text)


def text_exceeds_admission_boundary(text: str) -> bool:
    compact = re.sub(r"[\s，。！？、,.!?：:；;（）()]+", "", text)
    allowed_negations = (
        "不保证录取",
        "不能保证录取",
        "无法保证录取",
        "不确保录取",
        "不构成录取保证",
        "不构成任何录取保证",
        "不构成录取承诺",
        "不构成任何录取承诺",
        "并非录取承诺",
        "不是录取承诺",
        "非录取承诺",
        "不代表保证录取",
        "不承诺录取",
    )
    for phrase in allowed_negations:
        compact = compact.replace(phrase, "")
    forbidden_patterns = (
        r"(?:保证|确保|承诺|必定|一定).{0,6}录取",
        r"(?:100%|百分之百).{0,2}录取",
        r"录取.{0,2}(?:保证|无忧|稳了|稳妥|放心)",
        r"包录取",
    )
    return any(
        re.search(pattern, compact)
        for pattern in forbidden_patterns
    )


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def is_sha256(value: str) -> bool:
    return (
        len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def valid_locator(document: DocumentEvidence) -> bool:
    if not document.section_path.strip() or not document.excerpt:
        return False
    if (document.char_start is None) != (document.char_end is None):
        return False
    if document.char_start is None:
        return True
    return (
        document.char_start >= 0
        and document.char_end is not None
        and document.char_end >= document.char_start
        and document.char_end - document.char_start == len(document.excerpt)
    )


def valid_evidence_ref(ref: EvidenceRef) -> bool:
    return (
        bool(ref.evidence_id)
        and bool(ref.evidence_type)
        and bool(ref.release_id)
        and is_sha256(ref.content_sha256)
        and bool(ref.producer_version)
        and ref.created_at.tzinfo is not None
    )


def evidence_payload_release_matches(
    ref: EvidenceRef,
    release_id: str,
) -> bool:
    if ref.release_id != release_id:
        return False
    return all(
        payload is None or payload.release_id == release_id
        for payload in (ref.document, ref.structured, ref.derived)
    )


def valid_document_identity(document: DocumentEvidence) -> bool:
    required = (
        document.doc_id,
        document.chunk_id,
        document.title,
        document.document_type,
        document.issuer,
        document.issuer_type,
        document.issuer_id,
        document.source_uri,
        document.section_path,
        document.excerpt,
        document.release_id,
        document.document_content_sha256,
        document.raw_storage_uri,
        document.chunk_set_version,
    )
    if not all(value.strip() for value in required):
        return False
    if not is_sha256(document.excerpt_sha256):
        return False
    if not is_sha256(document.document_content_sha256):
        return False
    if document.fetched_at.tzinfo is None:
        return False
    if (
        document.issuer_type == "university"
        or document.document_type in {"charter", "university_notice"}
    ) and not document.school_id:
        return False
    return True


def valid_structured_identity(evidence: StructuredEvidence) -> bool:
    business_key = evidence.business_key
    required = (
        evidence.dataset,
        evidence.table,
        evidence.primary_key,
        evidence.field_path,
        evidence.release_id,
        business_key.province,
        business_key.category,
        business_key.batch,
    )
    if not all(value.strip() for value in required):
        return False
    if business_key.year <= 0:
        return False
    if evidence.unit == "":
        return False
    if evidence.source_year is not None and evidence.source_year <= 0:
        return False
    if evidence.precision is not None and evidence.precision < 0:
        return False
    return is_sha256(evidence.row_or_snapshot_sha256)
