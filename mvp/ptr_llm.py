"""Constrained LLM review contract for PTR ↔ Report manual rows.

The comparison engine does not call a model.  This module only defines the
small JSON contract a future provider may use and validates every returned
quote against the already extracted source text.  A provider result that
cannot be verified is rejected and must remain ``manual``.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any, Mapping, Protocol, Sequence


PTR_LLM_SCHEMA_VERSION = "ptr-llm-review-0.1"
ALLOWED_DECISIONS = {"equivalent", "different", "uncertain"}
ALLOWED_SIDES = {"ptr", "report"}


class PtrLlmReviewError(ValueError):
    """Raised when an LLM review cannot be tied to source evidence."""


class PtrLlmProvider(Protocol):
    """Explicit provider seam; comparison code never instantiates it."""

    provider_id: str
    model_id: str

    def review(self, task: Mapping[str, Any]) -> Mapping[str, Any]:
        """Return the constrained JSON payload for one review task."""


def normalize_review_text(value: Any) -> str:
    """Apply only layout normalization for quote verification."""

    text = unicodedata.normalize("NFKC", str(value or ""))
    replacements = {
        "—": "-",
        "–": "-",
        "－": "-",
        "～": "~",
        "，": ",",
        "；": ";",
        "：": ":",
        "（": "(",
        "）": ")",
        "µ": "μ",
        "Ω": "Ω",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    text = re.sub(r"(?i)(?<=\d)\s*u\s*(?=[gGlLmMnNsSvVaApPΩ])", "μ", text)
    return re.sub(r"\s+", "", text)


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PtrLlmReviewError(f"{field} 必须是非空字符串")
    return value.strip()


def _list(value: Any, field: str) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise PtrLlmReviewError(f"{field} 必须是数组")
    return list(value)


def validate_llm_review(
    payload: Mapping[str, Any],
    *,
    ptr_text: str = "",
    report_text: str = "",
) -> dict[str, Any]:
    """Validate and return a normalized review payload.

    ``text_evidence.quote`` must occur in the corresponding source channel
    after only layout normalization.  No numeric claim is accepted as a
    replacement for the rule engine's numeric signature.
    """

    if not isinstance(payload, Mapping):
        raise PtrLlmReviewError("LLM 结果必须是 JSON 对象")
    decision = _text(payload.get("decision"), "decision")
    if decision not in ALLOWED_DECISIONS:
        raise PtrLlmReviewError("decision 必须是 equivalent、different 或 uncertain")
    missing = _list(payload.get("missing_concepts"), "missing_concepts")
    extra = _list(payload.get("extra_concepts"), "extra_concepts")
    numeric_claims = _list(payload.get("numeric_claims"), "numeric_claims")
    evidence = _list(payload.get("text_evidence"), "text_evidence")
    normalized_evidence: list[dict[str, Any]] = []
    sources = {"ptr": str(ptr_text or ""), "report": str(report_text or "")}
    for index, item in enumerate(evidence):
        if not isinstance(item, Mapping):
            raise PtrLlmReviewError(f"text_evidence[{index}] 必须是对象")
        side = _text(item.get("side"), f"text_evidence[{index}].side")
        if side not in ALLOWED_SIDES:
            raise PtrLlmReviewError(f"text_evidence[{index}].side 必须是 ptr 或 report")
        quote = _text(item.get("quote"), f"text_evidence[{index}].quote")
        page = item.get("page")
        if isinstance(page, bool) or not isinstance(page, int) or page <= 0:
            raise PtrLlmReviewError(f"text_evidence[{index}].page 必须是正整数")
        source = normalize_review_text(sources[side])
        needle = normalize_review_text(quote)
        if not source or not needle or needle not in source:
            raise PtrLlmReviewError(f"text_evidence[{index}] 的 quote 不存在于 {side} 原文")
        normalized_evidence.append({"side": side, "quote": quote, "page": page})
    reason = payload.get("reason", "")
    if reason is not None and not isinstance(reason, str):
        raise PtrLlmReviewError("reason 必须是字符串")
    return {
        "schema_version": PTR_LLM_SCHEMA_VERSION,
        "decision": decision,
        "missing_concepts": missing,
        "extra_concepts": extra,
        "text_evidence": normalized_evidence,
        "numeric_claims": numeric_claims,
        "reason": str(reason or "").strip(),
        "validated": True,
    }


def build_review_task(
    *,
    clause_id: str,
    reason_code: str,
    ptr_text: str,
    report_text: str,
    ptr_pages: Sequence[int] = (),
    report_pages: Sequence[int] = (),
    max_chars: int = 1600,
) -> dict[str, Any]:
    """Build a bounded, source-only task for a future LLM provider."""

    if max_chars < 200:
        raise PtrLlmReviewError("max_chars 太小，无法保留最小证据")
    source_payload = {
        "clause_id": str(clause_id),
        "reason_code": str(reason_code),
        "ptr": str(ptr_text or "")[:max_chars],
        "report": str(report_text or "")[:max_chars],
    }
    input_sha256 = hashlib.sha256(
        json.dumps(source_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return {
        "schema_version": PTR_LLM_SCHEMA_VERSION,
        "input_sha256": input_sha256,
        "clause_id": str(clause_id),
        "reason_code": str(reason_code),
        "ptr": {"text": source_payload["ptr"], "pages": [int(p) for p in ptr_pages]},
        "report": {"text": source_payload["report"], "pages": [int(p) for p in report_pages]},
        "instructions": [
            "只能判断非数值文本的等价、缺项或不确定性。",
            "不得修改条款号、数字、单位、比较符、范围或引用。",
            "每个 text_evidence.quote 必须逐字来自对应原文。",
            "无法确定时返回 uncertain。",
        ],
    }


__all__ = [
    "ALLOWED_DECISIONS",
    "PTR_LLM_SCHEMA_VERSION",
    "PtrLlmReviewError",
    "PtrLlmProvider",
    "build_review_task",
    "validate_llm_review",
]
