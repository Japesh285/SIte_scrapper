"""Data normalization — clean and standardize extracted job data."""

import re
from typing import Any


def normalize_job_data(data: dict) -> dict:
    """Clean and normalize extracted job fields.

    - locations → always list of strings
    - experience → clean string (strip JSON objects like MonetaryAmount)
    - Strip dicts/lists where strings expected
    - Deduplicate skill arrays
    """
    if not data or not isinstance(data, dict):
        return data or {}

    # ── locations: always array of strings ────────────────────
    loc = data.get("location")
    if loc is None:
        data["location"] = []
    elif isinstance(loc, str):
        data["location"] = [loc] if loc else []
    elif isinstance(loc, list):
        data["location"] = [_safe_str(item) for item in loc if _safe_str(item)]
    elif isinstance(loc, dict):
        # Handle Address objects from JSON-LD
        parts = [
            _safe_str(loc.get("addressLocality")),
            _safe_str(loc.get("addressRegion")),
            _safe_str(loc.get("addressCountry")),
            _safe_str(loc.get("name")),
        ]
        combined = ", ".join(p for p in parts if p)
        data["location"] = [combined] if combined else []
    else:
        data["location"] = [_safe_str(loc)]

    # ── experience: clean string, strip JSON objects ──────────
    exp = data.get("experience", "")
    if isinstance(exp, dict):
        # e.g. MonetaryAmount → try to extract value
        exp_str = (
            _safe_str(exp.get("value", {}).get("minValue", ""))
            or _safe_str(exp.get("value"))
            or _safe_str(exp.get("description", ""))
        )
        data["experience"] = exp_str
    elif isinstance(exp, list):
        data["experience"] = ", ".join(_safe_str(e) for e in exp if _safe_str(e))
    else:
        data["experience"] = _safe_str(exp)

    # ── salary: clean string ──────────────────────────────────
    sal = data.get("salary", "")
    if isinstance(sal, dict):
        val = sal.get("value", {})
        if isinstance(val, dict):
            mn = _safe_str(val.get("minValue", ""))
            mx = _safe_str(val.get("maxValue", ""))
            cur = _safe_str(sal.get("currency", ""))
            parts = [p for p in [mn, mx] if p]
            data["salary"] = f"{cur} {'-'.join(parts)}".strip() if parts else ""
        else:
            data["salary"] = _safe_str(val) or _safe_str(sal.get("text", ""))
    elif isinstance(sal, list):
        data["salary"] = ", ".join(_safe_str(s) for s in sal if _safe_str(s))
    else:
        data["salary"] = _safe_str(sal)

    # ── education: clean string ───────────────────────────────
    edu = data.get("education", "")
    if isinstance(edu, list):
        data["education"] = ", ".join(_safe_str(e) for e in edu if _safe_str(e))
    elif isinstance(edu, dict):
        data["education"] = _safe_str(edu.get("credentialCategory", "")) or _safe_str(edu.get("description", ""))
    else:
        data["education"] = _safe_str(edu)

    # ── Deduplicate all skill/qualification arrays ────────────
    for key in [
        "required_skills",
        "preferred_skills",
        "soft_skills",
        "inferred_skills",
        "skills",
        "tools_and_technologies",
        "qualifications",
        "certifications",
        "benefits",
        "key_responsibilities",
    ]:
        arr = data.get(key)
        if isinstance(arr, list):
            cleaned = []
            seen: set[str] = set()
            for item in arr:
                s = _safe_str(item)
                if s and s.lower() not in seen:
                    seen.add(s.lower())
                    cleaned.append(s)
            # Limit to 15 items max to keep output lean
            data[key] = cleaned[:15]
        elif arr:
            data[key] = [_safe_str(arr)]
        else:
            data[key] = []

    # ── additional_sections: ensure list of dicts ─────────────
    sections = data.get("additional_sections", [])
    if not isinstance(sections, list):
        data["additional_sections"] = []
    else:
        cleaned_sections = []
        for sec in sections:
            if isinstance(sec, dict):
                cleaned_sections.append({
                    "section_title": _safe_str(sec.get("section_title", "")),
                    "content": _safe_str(sec.get("content", "")),
                })
        data["additional_sections"] = cleaned_sections

    # ── String fields: ensure clean strings ───────────────────
    for key in [
        "title", "company_name", "employment_type", "posted_on",
        "job_description", "description", "about_company",
    ]:
        data[key] = _safe_str(data.get(key, ""))

    return data


def _safe_str(val: Any) -> str:
    """Convert any value to a clean string."""
    if val is None:
        return ""
    if isinstance(val, str):
        return val.strip()
    if isinstance(val, (int, float)):
        return str(val)
    # For dicts/lists that shouldn't have leaked through
    return ""
