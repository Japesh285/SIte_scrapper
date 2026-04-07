"""DYNAMIC_API detection — identifies hidden job APIs via network interception.

This detector analyzes captured JSON responses from the browser probe to find
valid job APIs that load content dynamically (after user interaction).

Detection is deterministic via keyword scoring — NO AI, NO brittle selectors.
"""

from __future__ import annotations

import json

from app.core.logger import logger
from app.detectors.browser_probe import BrowserProbeResult, NetworkResponse

# ── Job-related keyword scoring ──────────────────────────────────────────

_JOB_KEYWORDS = [
    "job", "title", "location", "description", "position", "career",
    "department", "team", "posting", "requisition", "vacancy",
]

_PAGINATION_KEYWORDS = ["page", "limit", "offset", "cursor", "next", "total"]


def score_api_response(response: NetworkResponse) -> int:
    """Score a single JSON response for job-API likelihood.

    Scoring rules (deterministic, transparent):
    - +3 if ≥3 job keywords present
    - +3 if "description" in text (strong signal)
    - +2 if "title" in text
    - +2 if "location" in text
    - +2 if pagination markers present
    - -3 if body < 500 chars (too small to be a job list)

    Returns integer score (higher = more likely a job API).
    """
    if response.body is None:
        return 0

    text = json.dumps(response.body).lower()
    score = 0

    # Keyword hit counting
    keyword_hits = sum(1 for kw in _JOB_KEYWORDS if kw in text)
    if keyword_hits >= 3:
        score += 3

    # Strong individual signals
    if "description" in text:
        score += 3
    if "title" in text:
        score += 2
    if "location" in text:
        score += 2

    # Pagination signals (indicates list endpoint)
    if any(kw in text for kw in _PAGINATION_KEYWORDS):
        score += 2

    # Size penalty — tiny responses unlikely to be job lists
    if len(text) < 500:
        score -= 3

    return score


def extract_request_template(response: NetworkResponse, requests: list) -> dict:
    """Extract reusable request template for the best API response.

    Attempts to match the response URL back to its originating request
    to capture method, headers, and post_data.

    Returns dict with: url, method, headers, payload
    """
    template = {
        "url": response.url,
        "method": "GET",
        "headers": {},
        "payload": None,
    }

    # Find matching request by URL prefix
    for req in requests:
        if response.url.startswith(req.url) or req.url.startswith(response.url):
            template["method"] = req.method
            template["headers"] = req.headers
            if req.post_data:
                try:
                    template["payload"] = json.loads(req.post_data)
                except (json.JSONDecodeError, TypeError):
                    template["payload"] = req.post_data
            break

    return template


def detect_dynamic_api(probe: BrowserProbeResult) -> dict:
    """Detect DYNAMIC_API from browser probe results.

    Analyzes all captured JSON responses and selects the best candidate
    using the scoring function.

    Parameters
    ----------
    probe : BrowserProbeResult
        Result from run_browser_probe().

    Returns
    -------
    dict with keys:
        - matched: bool — True if valid DYNAMIC_API found
        - jobs_found: int — estimated job count from best response
        - api_usable: bool — True if score >= 5 (reusable for scraping)
        - best_api: dict — request template (url, method, headers, payload)
        - best_score: int — API response score
        - all_scores: list[dict] — scores for all JSON responses (debug)
        - confidence: float — 0-1 confidence score
    """
    if not probe.available:
        logger.info("[DynamicAPI] Browser probe not available")
        return _empty_result()

    if not probe.responses:
        logger.info("[DynamicAPI] No JSON responses captured")
        return _empty_result()

    # Score all JSON responses
    scored = []
    for resp in probe.responses:
        s = score_api_response(resp)
        scored.append((s, resp))

    # Log all scores for debugging
    all_scores = [
        {"url": resp.url, "score": s, "body_length": resp.body_length}
        for s, resp in scored
    ]

    # Select best response
    best_score, best_response = max(scored, key=lambda x: x[0])

    logger.info(
        "[DynamicAPI] Best API score: %d (url=%s, body_length=%d)",
        best_score,
        best_response.url,
        best_response.body_length,
    )
    logger.info("[DynamicAPI] All scores: %s", all_scores)

    # Classification threshold
    if best_score < 5:
        logger.info("[DynamicAPI] Score %d < 5 — not a valid job API", best_score)
        return {
            **_empty_result(),
            "all_scores": all_scores,
            "best_score": best_score,
        }

    # Estimate job count from best response
    jobs_found = _estimate_job_count(best_response.body)

    # Extract reusable request template
    best_api = extract_request_template(best_response, probe.requests)

    # Confidence formula: score-based, capped at 0.95
    confidence = min(0.95, 0.50 + (best_score * 0.05))

    logger.info(
        "[DynamicAPI] MATCH — score=%d, jobs_found=%d, confidence=%.2f, url=%s",
        best_score,
        jobs_found,
        confidence,
        best_api["url"],
    )

    return {
        "matched": True,
        "jobs_found": jobs_found,
        "api_usable": True,
        "best_api": best_api,
        "best_score": best_score,
        "all_scores": all_scores,
        "confidence": confidence,
    }


def _estimate_job_count(body) -> int:
    """Estimate job count from API response body.

    Tries common list patterns:
    - Top-level array → len(body)
    - body["jobs"], body["results"], body["data"] → len of nested list
    - body["total"], body["count"] → direct integer
    """
    if isinstance(body, list):
        return len(body)

    if isinstance(body, dict):
        # Try common list keys
        for key in ("jobs", "results", "data", "positions", "openings", "items", "records"):
            val = body.get(key)
            if isinstance(val, list):
                return len(val)

        # Try total/count fields
        for key in ("total", "count", "total_results", "num_jobs", "job_count"):
            val = body.get(key)
            if isinstance(val, int):
                return val

        # Count objects with job-like fields
        job_like = 0
        for key, val in body.items():
            if isinstance(val, dict):
                fields = set(val.keys())
                if {"title", "location"} & fields or {"job_title", "location"} & fields:
                    job_like += 1
            elif isinstance(val, list) and len(val) > 0 and isinstance(val[0], dict):
                fields = set(val[0].keys())
                if {"title", "location"} & fields:
                    job_like += len(val)

        if job_like > 0:
            return job_like

    return 0


def _empty_result() -> dict:
    return {
        "matched": False,
        "jobs_found": 0,
        "api_usable": False,
        "best_api": {},
        "best_score": 0,
        "all_scores": [],
        "confidence": 0.0,
    }
