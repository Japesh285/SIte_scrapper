"""DYNAMIC_API detection — Playwright network interception + interaction simulation.

Detects hidden job APIs (POST, GraphQL, lazy-loaded) that only fire after
user interaction on sites like Oracle, Dell, etc.

Two entry points:
1. detect_dynamic_api(url: str) — standalone async detector (primary)
2. detect_dynamic_api_from_probe(probe) — sync detector for pre-captured data (legacy)
"""

from __future__ import annotations

import json
import re

from app.core.logger import logger

# ── URL patterns that indicate NON-job APIs ────────────────────────

_REJECTED_URL_PATTERNS = [
    "translation", "translations", "translate",
    "locale", "locales",
    "language", "languages",
    "label", "labels",
    "config", "configuration", "settings",
    "i18n", "l10n",
    "analytics", "telemetry", "tracking", "pixel",
    "cookie", "consent",
    "geo", "timezone",
    "feature-flag", "featuretoggle", "feature_flag",
    "abtest", "ab-test", "experiment",
    "health", "heartbeat", "ping",
    "captcha", "recaptcha",
]

# ── Job-related keywords ─────────────────────────────────────────────

_JOB_KEYWORDS = ["job", "jobs", "requisition", "posting", "career", "position"]
_CONTENT_KEYWORDS = ["title", "location", "department", "description", "salary"]

# ── Top-level keys that indicate a job list response ────────────────

_JOB_LIST_KEYS = [
    "jobs", "jobPostings", "job_postings", "postings", "positions",
    "requisitions", "reqs", "openings", "vacancies", "opportunities",
    "careerOpportunities", "career_postings",
]

# ── Keys within an individual job object ─────────────────────────────

_JOB_OBJECT_TITLE_KEYS = ["title", "jobTitle", "positionTitle", "job_title", "postingTitle", "name"]
_JOB_OBJECT_LOCATION_KEYS = ["location", "locations", "locationText", "city", "workLocation"]
_JOB_OBJECT_ID_KEYS = ["id", "jobId", "job_id", "requisitionId", "reqId", "postingId", "externalId"]

try:
    from playwright.async_api import async_playwright
    _HAS_PLAYWRIGHT = True
except ImportError:
    _HAS_PLAYWRIGHT = False

_MAX_API_SIZE_BYTES = 200 * 1024  # 200KB

# ── Standalone async detector (PRIMARY) ──────────────────────────────

async def detect_dynamic_api(url: str) -> dict:
    """Detect hidden job APIs by launching Playwright, simulating interactions,
    and capturing XHR/fetch JSON responses.

    Parameters
    ----------
    url : str
        Career page URL.

    Returns
    -------
    dict with keys:
        matched, api_usable, api_url, method, payload, headers, confidence
    """
    if not _HAS_PLAYWRIGHT:
        logger.warning("[DynamicAPI] Playwright not available")
        return _empty_result()

    captured = []

    async def _handle_response(response):
        try:
            if response.request.resource_type not in ("xhr", "fetch"):
                return
            ct = response.headers.get("content-type", "")
            if "application/json" not in ct.lower():
                return
            data = await response.json()
            captured.append({
                "url": response.url,
                "method": response.request.method,
                "data": data,
                "request_post_data": response.request.post_data,
                "headers": dict(response.request.headers),
            })
        except Exception:
            pass

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
            )
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1920, "height": 1080},
                ignore_https_errors=True,
            )
            page = await context.new_page()
            page.on("response", _handle_response)

            logger.info("[DynamicAPI] Navigating to %s", url)
            await page.goto(url, timeout=60000)

            # ── Interaction simulation ───────────────────────────────
            await _simulate_interactions(page)

            await browser.close()
    except Exception as exc:
        logger.error("[DynamicAPI] Browser session failed: %s", exc)
        return _empty_result()

    logger.info("[DynamicAPI] Captured %d JSON responses from XHR/fetch", len(captured))

    # ── STEP 1: Reject translation/config/locale endpoints ─────────
    url_filtered = []
    for r in captured:
        if _is_rejected_url(r["url"]):
            logger.info(
                "[DynamicAPI] Skipped translation/config endpoint: %s",
                r["url"],
            )
            continue
        url_filtered.append(r)

    if not url_filtered:
        logger.info("[DynamicAPI] All %d responses were non-job URLs", len(captured))
        return _empty_result()

    logger.info("[DynamicAPI] %d URLs passed URL pattern filter", len(url_filtered))

    # ── STEP 2: Reject responses that don't have job structure ──────
    candidates = []
    for r in url_filtered:
        if is_job_api_response(r["data"]):
            candidates.append(r)
            job_count = _count_job_objects(r["data"])
            logger.info(
                "[DynamicAPI] Accepted (job API): %s jobs=%d",
                r["url"], job_count,
            )
        else:
            logger.info("[DynamicAPI] Rejected (not job data): %s", r["url"])

    if not candidates:
        logger.info(
            "[DynamicAPI] No job API candidates found in %d filtered responses",
            len(url_filtered),
        )
        return _empty_result()

    logger.info("[DynamicAPI] %d job API candidates from %d filtered responses", len(candidates), len(url_filtered))

    # ── STEP 3: Score and select best ────────────────────────────────
    best = select_best_api(candidates)
    if best is None:
        logger.info("[DynamicAPI] No valid API after selection validation")
        return _empty_result()

    best_score = score_api_response(best)
    logger.info(
        "[DynamicAPI] Best API: url=%s method=%s score=%d",
        best["url"], best["method"], best_score,
    )

    if best_score < 5:
        logger.info("[DynamicAPI] Best score %d < 5 — not a valid job API", best_score)
        return _empty_result()

    logger.info("[DYNAMIC_API] Found: %s method=%s", best["url"], best["method"])

    confidence = min(0.95, best_score / 10.0)
    job_count = _count_job_objects(best["data"])

    return {
        "matched": True,
        "api_usable": True,
        "api_url": best["url"],
        "method": best["method"],
        "payload": _parse_payload(best["request_post_data"]),
        "headers": best["headers"],
        "confidence": confidence,
        "score": best_score,
        "jobs_found": job_count,
    }


# ── Interaction simulation ───────────────────────────────────────────

async def _simulate_interactions(page) -> None:
    """Simulate user interactions to trigger hidden APIs.

    Total time: ~6-8 seconds max.
    """
    # Wait for initial page load
    await page.wait_for_timeout(2000)

    # Press ENTER — many sites trigger search on Enter
    try:
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(1500)
    except Exception:
        pass

    # Click search/find/jobs buttons
    try:
        buttons = await page.query_selector_all("button")
        for btn in buttons:
            try:
                text = (await btn.inner_text() or "").lower()
                if any(k in text for k in ["search", "find", "apply", "jobs", "load", "show"]):
                    await btn.click()
                    await page.wait_for_timeout(2000)
                    break
            except Exception:
                continue
    except Exception:
        pass

    # Select first option from dropdowns (triggers filter APIs)
    try:
        selects = await page.query_selector_all("select")
        for sel in selects[:2]:
            try:
                options = await sel.query_selector_all("option")
                if len(options) > 1:
                    await sel.select_option(index=1)
                    await page.wait_for_timeout(1500)
            except Exception:
                continue
    except Exception:
        pass

    # Scroll to trigger lazy loading
    try:
        await page.mouse.wheel(0, 3000)
        await page.wait_for_timeout(3000)
    except Exception:
        pass


# ── URL rejection ────────────────────────────────────────────────────

_REJECTED_URL_PATTERNS = [
    "translation", "translations", "translate",
    "locale", "locales",
    "language", "languages",
    "label", "labels",
    "config", "configuration", "settings",
    "i18n", "l10n",
    "analytics", "telemetry", "tracking", "pixel",
    "cookie", "consent",
    "geo", "timezone",
    "feature-flag", "featuretoggle", "feature_flag",
    "abtest", "ab-test", "experiment",
    "health", "heartbeat", "ping",
    "captcha", "recaptcha",
]


def _is_rejected_url(url: str) -> bool:
    """Check if URL matches known non-job API patterns."""
    url_lower = url.lower()
    return any(pattern in url_lower for pattern in _REJECTED_URL_PATTERNS)


# ── CMS / page-builder detection ─────────────────────────────────────

_CMS_TOP_KEYS = {"sections", "rows", "columns", "components", "pageParams",
                  "pageLayout", "template", "widgets", "regions", "slots"}

_CMS_NESTED_KEYS = {"rows", "columns", "components", "elements", "children",
                     "blocks", "widgets", "sections", "zones"}


def is_cms_response(json_data) -> bool:
    """Detect CMS/page-builder JSON structure.

    Returns True if response looks like page layout data rather than job data.
    Signals:
    - Top-level keys like sections, rows, components, pageParams
    - Deeply nested rows/columns/components at any level
    """
    if not isinstance(json_data, dict):
        return False

    # Signal 1: Top-level CMS keys
    top_keys = set(json_data.keys())
    cms_top_hits = top_keys & _CMS_TOP_KEYS
    if cms_top_hits:
        logger.info("[CMS] Rejected — top-level CMS keys: %s", cms_top_hits)
        return True

    # Signal 2: Deeply nested layout structures
    if _has_nested_cms_structure(json_data, depth=0, max_depth=4):
        return True

    return False


def _has_nested_cms_structure(obj, depth: int, max_depth: int) -> bool:
    """Recursively check for nested CMS layout patterns."""
    if depth > max_depth:
        return False
    if not isinstance(obj, dict):
        return False

    keys = set(obj.keys())
    # If this dict has ≥2 CMS nested keys, it's likely page layout
    cms_hits = keys & _CMS_NESTED_KEYS
    if len(cms_hits) >= 2:
        logger.info("[CMS] Rejected — nested CMS keys at depth %d: %s", depth, cms_hits)
        return True

    # Recurse into values
    for val in obj.values():
        if isinstance(val, dict) and _has_nested_cms_structure(val, depth + 1, max_depth):
            return True
        if isinstance(val, list) and len(val) > 0 and isinstance(val[0], dict):
            if _has_nested_cms_structure(val[0], depth + 1, max_depth):
                return True

    return False


# ── Positive job API detection ───────────────────────────────────────

# Field groups: need at least 2 groups present
_JOB_FIELD_GROUPS = {
    "title": ["title", "jobTitle", "job_title", "requisitionTitle", "positionTitle", "name", "postingTitle"],
    "location": ["location", "locations", "primaryLocation", "locationText", "city", "workLocation"],
    "id": ["id", "jobId", "job_id", "requisitionId", "reqId", "postingId", "externalId", "requisition_id"],
    "posting_date": ["postingDate", "postedDate", "posted_date", "datePosted", "publishDate", "postedOn"],
}


def is_job_api(json_data) -> bool:
    """Validate whether a JSON response is a real job API.

    Returns True only if response contains an array of objects where
    at least 2 of these field groups are present:
    - title / jobTitle
    - location / locations
    - jobId / requisitionId / id
    - postingDate / postedDate
    """
    if json_data is None:
        return False

    # Find candidate arrays
    arrays = _find_candidate_arrays(json_data)
    for arr in arrays:
        if _array_is_job_list(arr):
            return True

    return False


def _find_candidate_arrays(obj) -> list[list]:
    """Find all lists in the response that could be job lists."""
    results = []

    if isinstance(obj, list) and len(obj) > 0:
        results.append(obj)
        # Also check first few items for nested arrays
        for item in obj[:3]:
            if isinstance(item, dict):
                results.extend(_find_candidate_arrays_in_dict(item))
    elif isinstance(obj, dict):
        results.extend(_find_candidate_arrays_in_dict(obj))

    return results


def _find_candidate_arrays_in_dict(d: dict) -> list[list]:
    """Find list values inside a dict that could be job lists."""
    results = []
    for key, val in d.items():
        if isinstance(val, list) and len(val) > 0:
            results.append(val)
        elif isinstance(val, dict):
            results.extend(_find_candidate_arrays_in_dict(val))
    return results


def _array_is_job_list(arr: list) -> bool:
    """Check if a list contains job objects (≥2 field groups)."""
    if len(arr) == 0:
        return False

    sample = arr[:min(5, len(arr))]
    for item in sample:
        if isinstance(item, dict) and _count_job_field_groups(item) >= 2:
            return True

    return False


def _count_job_field_groups(obj: dict) -> int:
    """Count how many job field groups are present in an object."""
    all_keys = " ".join(str(k) for k in obj.keys())
    score = 0
    for group_name, group_keys in _JOB_FIELD_GROUPS.items():
        if any(k in all_keys for k in group_keys):
            score += 1
    return score


# ── Job object detection (backward compat) ───────────────────────────

def _is_job_object(obj: dict) -> bool:
    """Legacy: check if single dict looks like a job posting."""
    return _count_job_field_groups(obj) >= 2


def is_job_api_response(json_data) -> bool:
    """Alias for backward compatibility."""
    return is_job_api(json_data)


def _count_job_objects(json_data) -> int:
    """Count job-like objects in the response."""
    if isinstance(json_data, list):
        return sum(1 for item in json_data if isinstance(item, dict) and _is_job_object(item))

    if isinstance(json_data, dict):
        for key in _JOB_LIST_KEYS:
            val = json_data.get(key)
            if isinstance(val, list):
                return sum(1 for item in val if isinstance(item, dict) and _is_job_object(item))

        data_val = json_data.get("data")
        if isinstance(data_val, list):
            return sum(1 for item in data_val if isinstance(item, dict) and _is_job_object(item))

        if _is_job_object(json_data):
            return 1

    return 0


# ── Scoring ─────────────────────────────────────────────────────────

def score_api_response(resp: dict) -> int:
    """Score an API response for job-API likelihood.

    +10 if job array detected
    +5  if flat JSON with consistent schema
    +2  if small JSON size (<100KB)
    +2  if multiple job objects found
    +2  if title in data
    +2  if location in data
    +1  if complex dict (many keys)
    +1  if POST method

    -10 if CMS structure detected
    -10 if URL contains translation/config keywords
    -5  if deeply nested (>4 levels)
    -5  if no job structure
    """
    score = 0
    data = resp["data"]
    data_str = json.dumps(data).lower()
    url_lower = resp["url"].lower()

    # ── URL penalties ──
    for pattern in _REJECTED_URL_PATTERNS:
        if pattern in url_lower:
            score -= 10
            break

    # ── CMS detection (HARD reject) ──
    if is_cms_response(data):
        logger.info("[SCORING] Rejected: %s — CMS/page-builder structure", resp["url"])
        score -= 10

    # ── Job structure detection ──
    has_job_array = is_job_api(data)
    if has_job_array:
        score += 10
        logger.info("[SCORING] +10 job array detected: %s", resp["url"])
    else:
        score -= 5
        logger.info("[SCORING] -5 no job structure: %s", resp["url"])

    # ── Nesting depth penalty ──
    depth = _max_depth(data)
    if depth > 4:
        score -= 5
        logger.info("[SCORING] -5 deeply nested (depth=%d): %s", depth, resp["url"])

    # ── Flat JSON bonus ──
    if _is_flat_schema(data):
        score += 5

    # ── Size bonus ──
    json_size = len(data_str.encode("utf-8"))
    if json_size < 100 * 1024:
        score += 2

    # ── Job count bonus ──
    job_count = _count_job_objects(data)
    if job_count >= 3:
        score += 2

    # ── Content bonuses ──
    if any(k in data_str for k in ["title", "jobtitle", "positiontitle"]):
        score += 2
    if "location" in data_str:
        score += 2

    # ── Complexity ──
    if isinstance(data, dict) and len(data) > 3:
        score += 1

    # ── Method bonus ──
    if resp["method"] == "POST":
        score += 1

    return score


def _max_depth(obj, _depth: int = 0) -> int:
    """Calculate maximum nesting depth of a JSON structure."""
    if isinstance(obj, dict):
        if not obj:
            return _depth
        return max(_max_depth(v, _depth + 1) for v in obj.values())
    if isinstance(obj, list):
        if not obj:
            return _depth
        return max(_max_depth(v, _depth + 1) for v in obj)
    return _depth


def _is_flat_schema(obj) -> bool:
    """Check if data has a flat, consistent schema (dict with simple values)."""
    if not isinstance(obj, dict):
        return False
    # Count how many values are simple (str, int, bool, None) vs complex
    simple = sum(1 for v in obj.values() if isinstance(v, (str, int, bool, type(None))))
    total = len(obj)
    if total == 0:
        return False
    return (simple / total) > 0.6


# ── Selection with validation ────────────────────────────────────────

def select_best_api(responses: list[dict]) -> dict | None:
    """Select the best API response with post-scoring validation.

    Steps:
    1. Score all candidates
    2. Sort by score descending
    3. Validate top candidate has actual job array
    4. If not, try next candidate
    5. Return best valid candidate or None

    Parameters
    ----------
    responses : list of captured response dicts with url, method, data, etc.

    Returns
    -------
    dict or None — the best valid API response, or None if all fail validation.
    """
    if not responses:
        return None

    scored = [(score_api_response(r), r) for r in responses]
    scored.sort(key=lambda x: x[0], reverse=True)

    # Log all scores
    for score, resp in scored:
        is_cms = is_cms_response(resp["data"])
        has_jobs = is_job_api(resp["data"])
        schema_type = "CMS" if is_cms else ("JOB_API" if has_jobs else "UNKNOWN")
        logger.info(
            "[SELECT] score=%d url=%s schema=%s cms=%s jobs=%s",
            score, resp["url"], schema_type, is_cms, has_jobs,
        )

    # Validate each candidate in score order
    for score, resp in scored:
        # HARD reject: CMS structure
        if is_cms_response(resp["data"]):
            logger.info("[SELECT] Rejected (CMS): %s", resp["url"])
            continue

        # HARD reject: no job array
        if not is_job_api(resp["data"]):
            logger.info("[SELECT] Rejected (no job array): %s", resp["url"])
            continue

        # Accepted
        job_count = _count_job_objects(resp["data"])
        logger.info(
            "[SELECT] Accepted: %s score=%d jobs=%d",
            resp["url"], score, job_count,
        )
        return resp

    logger.info("[SELECT] All %d candidates rejected", len(responses))
    return None


def _parse_payload(raw: str | None):
    """Parse POST data into a dict, or return as-is."""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw


# ── Legacy: detect from pre-captured BrowserProbeResult ─────────────

def detect_dynamic_api_from_probe(probe) -> dict:
    """Detect DYNAMIC_API from pre-captured browser probe results.

    This is the legacy entry point used by the orchestrator's browser-assisted path.
    Kept for backward compatibility.
    """
    from app.detectors.browser_probe import BrowserProbeResult, NetworkResponse

    if not isinstance(probe, BrowserProbeResult) or not probe.available:
        logger.info("[DynamicAPI] Browser probe not available")
        return _empty_result()

    if not probe.responses:
        logger.info("[DynamicAPI] No JSON responses captured")
        return _empty_result()

    # ── Filter: reject translation/config URLs ──
    url_filtered = []
    for resp in probe.responses:
        if _is_rejected_url(resp.url):
            logger.info(
                "[DynamicAPI] Skipped translation/config endpoint: %s",
                resp.url,
            )
            continue
        url_filtered.append(resp)

    # ── Wrap NetworkResponse into dict format for shared functions ──
    wrapped = [
        {
            "url": r.url,
            "method": "GET",
            "data": r.body,
            "request_post_data": None,
            "headers": {},
            "_legacy_response": r,
        }
        for r in url_filtered
    ]

    # ── Filter: reject CMS and non-job responses ──
    candidates = []
    for r in wrapped:
        if is_cms_response(r["data"]):
            logger.info("[DynamicAPI] Rejected (CMS): %s", r["url"])
            continue
        if is_job_api(r["data"]):
            candidates.append(r)
            job_count = _count_job_objects(r["data"])
            logger.info(
                "[DynamicAPI] Accepted (job API): %s jobs=%d",
                r["url"], job_count,
            )
        else:
            logger.info("[DynamicAPI] Rejected (not job data): %s", r["url"])

    if not candidates:
        logger.info(
            "[DynamicAPI] No job API candidates from %d filtered responses",
            len(url_filtered),
        )
        return _empty_result()

    # ── Score and select with validation ──
    best = select_best_api(candidates)
    if best is None:
        return _empty_result()

    best_score = score_api_response(best)
    best_resp = best["_legacy_response"]
    all_scores = [
        {"url": r["url"], "score": score_api_response(r), "body_length": len(json.dumps(r["data"]))}
        for r in candidates
    ]

    logger.info(
        "[DynamicAPI] Best API score: %d (url=%s, body_length=%d)",
        best_score, best_resp.url, best_resp.body_length,
    )

    if best_score < 5:
        logger.info("[DynamicAPI] Score %d < 5 — not a valid job API", best_score)
        return {
            **_empty_result(),
            "all_scores": all_scores,
            "best_score": best_score,
        }

    jobs_found = _count_job_objects(best_resp.body)
    best_api = _extract_request_template_legacy(best_resp, probe.requests)
    confidence = min(0.95, 0.50 + (best_score * 0.05))

    logger.info(
        "[DynamicAPI] MATCH — score=%d, jobs_found=%d, confidence=%.2f, url=%s",
        best_score, jobs_found, confidence, best_api["url"],
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


def score_api_response_legacy(response) -> int:
    """Score a NetworkResponse for job-API likelihood (legacy compat)."""
    if response.body is None:
        return 0

    score = 0
    data_str = json.dumps(response.body).lower()
    url_lower = response.url.lower()

    # URL penalties
    for pattern in _REJECTED_URL_PATTERNS:
        if pattern in url_lower:
            score -= 10
            break

    # Structure bonus
    if is_job_api_response(response.body):
        score += 5
    else:
        score -= 5

    keyword_hits = sum(1 for kw in _JOB_KEYWORDS + _CONTENT_KEYWORDS if kw in data_str)
    if keyword_hits >= 3:
        score += 3
    if "description" in data_str:
        score += 3
    if "title" in data_str:
        score += 2
    if "location" in data_str:
        score += 2

    _PAGINATION_KEYWORDS = ["page", "limit", "offset", "cursor", "next", "total"]
    if any(kw in data_str for kw in _PAGINATION_KEYWORDS):
        score += 2

    if len(data_str) < 500:
        score -= 3

    return score


def _extract_request_template_legacy(response, requests: list) -> dict:
    """Extract reusable request template from captured response (legacy compat)."""
    template = {"url": response.url, "method": "GET", "headers": {}, "payload": None}

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


# ── Public aliases for backward compatibility ────────────────────────
# Used by scrapers/dynamic_api.py
score_api_response_legacy = score_api_response_legacy  # keep for external callers
extract_request_template = _extract_request_template_legacy


def _empty_result() -> dict:
    return {
        "matched": False,
        "api_usable": False,
        "api_url": "",
        "method": "",
        "payload": None,
        "headers": {},
        "confidence": 0.0,
        "score": 0,
        "jobs_found": 0,
    }
