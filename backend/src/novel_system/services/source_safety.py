from __future__ import annotations

import json
import os
import unicodedata
from collections.abc import Iterable
from typing import Any

from novel_system.db.models import utcnow as now_iso


# BUG-002 hardening: protected-source-term matching must survive trivial
# evasion of a red-line term — intra-term whitespace ("屠 龙"), inserted
# punctuation ("屠-龙" / "龙·族"), and traditional Chinese ("龍族" / "屠龍").
#
# Traditional→simplified folding is intentionally tiny and exists only to make
# explicitly configured/profile-derived protected terms resilient to cosmetic
# variants.  It is not a general Chinese conversion layer.
_TRADITIONAL_TO_SIMPLIFIED = {
    "龍": "龙",
    "愷": "恺",
    "諾": "诺",
    "陳": "陈",
    "爾": "尔",
    "熱": "热",
    "銅": "铜",
    "與": "与",
    "統": "统",
}
_TRAD_SIMP_TABLE = str.maketrans(_TRADITIONAL_TO_SIMPLIFIED)


def _normalize_for_match(text: str) -> str:
    """Normalize text so red-line terms cannot be evaded by cosmetic variants.

    Steps (all conservative — recall-biased, since a missed leak is the costly
    failure here): NFKC fold (e.g. full-width → half-width) → controlled
    traditional→simplified fold → strip every separator/punctuation/format
    character. Letters, digits, and CJK ideographs are NEVER removed, so
    normalization can only collapse obfuscation between glyphs; it can never
    fabricate a protected term out of unrelated alphanumeric/ideographic text.
    """
    folded = unicodedata.normalize("NFKC", str(text or "")).translate(_TRAD_SIMP_TABLE)
    cleaned: list[str] = []
    for ch in folded:
        category = unicodedata.category(ch)
        # Z* = separators/whitespace, P* = punctuation, Cf/Cc = format/control
        # (zero-width joiners, BOM, bidi marks, etc.).
        if category[0] in ("Z", "P") or category in ("Cf", "Cc"):
            continue
        cleaned.append(ch)
    return "".join(cleaned)


# No named work or author belongs in a process-wide default.  Keeping a
# source-specific list here used to flag ordinary fantasy terms (for example
# "龙王" and "血统") in projects that had never referenced that source.
#
# Backwards-compatible import alias: callers may still import the symbol, but
# the default is intentionally empty.  Configure project-independent terms via
# NOVEL_SYSTEM_PROTECTED_SOURCE_TERMS_JSON or, preferably, pass the active
# reference profile's ``protected_terms`` to the scanner.
PROTECTED_SOURCE_TERMS: tuple[str, ...] = ()
PROTECTED_SOURCE_TERMS_ENV = "NOVEL_SYSTEM_PROTECTED_SOURCE_TERMS_JSON"

SOURCE_PROFILE_REF_KEY_HINTS = (
    "profile",
    "style",
    "banned",
    "narrative",
    "calibration",
    "voice",
    "relation",
)


def scan_source_safety(
    texts: str | Iterable[str | None],
    *,
    source_profile_ids: Iterable[Any] | None = None,
    reference_safety_profiles: Iterable[dict[str, Any] | None] | None = None,
    protected_terms: Iterable[Any] | None = None,
) -> dict[str, Any]:
    content = _coerce_text(texts)
    normalized_content = _normalize_for_match(content)
    normalized_content_folded = normalized_content.casefold()
    configured_terms = (
        _unique_strings(protected_terms)
        if protected_terms is not None
        else configured_protected_source_terms()
    )
    # Match on the normalized form (defeats whitespace/punctuation/traditional
    # variants) but still report the canonical simplified term, in
    # configured order — downstream contracts depend on stable ordering.
    blocked_terms = [
        term
        for term in configured_terms
        if term and _normalize_for_match(term).casefold() in normalized_content_folded
    ]
    refs = _unique_strings(source_profile_ids or [])
    safety_profiles = list(reference_safety_profiles or [])
    risks = _reference_safety_risks(content, safety_profiles)
    payload = {
        "safe": not blocked_terms and not risks,
        "blocked_terms": blocked_terms,
        "source_profile_ids": refs,
        "protected_terms_source": (
            "explicit" if protected_terms is not None else "environment" if configured_terms else "none"
        ),
        "coverage": {
            "configured_exact_terms": True,
            "profile_exact_terms_and_phrases": bool(safety_profiles),
            "profile_scene_bridges": bool(safety_profiles),
            "semantic_paraphrase": {
                "status": "not_evaluated",
                "blocking": False,
                "reason": (
                    "deterministic source safety cannot reliably verify semantic or cross-language paraphrase"
                ),
                "recommended_action": "use independent semantic review as advisory evidence",
            },
        },
        "checked_at": now_iso(),
    }
    if safety_profiles or risks:
        payload["risks"] = risks
        payload["risk_count"] = len(risks)
    return payload


def configured_protected_source_terms() -> list[str]:
    """Load optional global safety terms from an explicit JSON-array setting.

    Invalid configuration fails closed with respect to *configuration scope*:
    it contributes no hidden blocklist.  Reference-profile safety remains
    active independently and is the preferred project-scoped mechanism.
    """
    raw = str(os.getenv(PROTECTED_SOURCE_TERMS_ENV, "") or "").strip()
    if not raw:
        return list(PROTECTED_SOURCE_TERMS)
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(payload, list):
        return []
    return _unique_strings(payload)


def source_profile_ids_from_snapshot(snapshot: dict[str, Any] | None) -> list[str]:
    if not isinstance(snapshot, dict):
        return []
    refs = snapshot.get("source_version_refs")
    if not isinstance(refs, dict):
        return []

    values: list[Any] = []
    for key, value in refs.items():
        normalized_key = str(key or "").lower()
        if normalized_key.endswith("_row_id") or normalized_key.endswith("_version"):
            continue
        if normalized_key.endswith("_contract"):
            continue
        if not (
            normalized_key.endswith("_id")
            or normalized_key.endswith("_ids")
            or any(hint in normalized_key for hint in SOURCE_PROFILE_REF_KEY_HINTS)
        ):
            continue
        values.extend(_flatten(value))
    return _unique_strings(values)


def _coerce_text(texts: str | Iterable[str | None]) -> str:
    if isinstance(texts, str):
        return texts
    return "\n".join(str(item or "") for item in texts)


def _flatten(value: Any) -> list[Any]:
    if isinstance(value, dict):
        values: list[Any] = []
        for item in value.values():
            values.extend(_flatten(item))
        return values
    if isinstance(value, (list, tuple, set)):
        values = []
        for item in value:
            values.extend(_flatten(item))
        return values
    return [value]


def _unique_strings(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            continue
        text = value.strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _reference_safety_risks(content: str, profiles: Iterable[dict[str, Any] | None]) -> list[dict[str, Any]]:
    risks: list[dict[str, Any]] = []
    # Same hardening as the fixed term list: normalize away cosmetic variants,
    # then casefold. This is a strict superset of the old `term.lower() in
    # content.lower()` — separators are stripped from both needle and haystack,
    # so anything that matched before still matches.
    lowered = _normalize_for_match(content).lower()
    for profile in profiles:
        if not isinstance(profile, dict):
            continue
        profile_id = str(profile.get("profile_id") or "").strip()
        for term in _unique_strings(profile.get("protected_terms") or []):
            if _normalize_for_match(term).lower() in lowered:
                risks.append(
                    {
                        "risk_type": "exact_term",
                        "profile_id": profile_id,
                        "matched": term,
                        "severity": "high",
                        "recommendation": "Replace the protected term with an original name, object, or setting.",
                    }
                )
        for phrase in _unique_strings(profile.get("distinctive_phrases") or []):
            if _normalize_for_match(phrase).lower() in lowered and not any(
                risk.get("matched") == phrase for risk in risks
            ):
                risks.append(
                    {
                        "risk_type": "distinctive_phrase",
                        "profile_id": profile_id,
                        "matched": phrase,
                        "severity": "medium",
                        "recommendation": "Keep the craft function but change the phrase, object field, and scene context.",
                    }
                )
        for bridge in profile.get("scene_bridges") or []:
            if not isinstance(bridge, dict):
                continue
            tokens = _unique_strings(bridge.get("tokens") or [])
            matched = [token for token in tokens if _normalize_for_match(token).lower() in lowered]
            if len(matched) >= 2:
                risks.append(
                    {
                        "risk_type": "fuzzy_bridge",
                        "profile_id": profile_id,
                        "bridge_id": bridge.get("bridge_id"),
                        "matched": matched[:6],
                        "severity": "high" if len(matched) >= 3 else "medium",
                        "evidence_preview": bridge.get("evidence_preview") or "",
                        "recommendation": "Break the recognizable bridge: change at least two of entity, object, setting, action, and payoff.",
                    }
                )
    return risks
