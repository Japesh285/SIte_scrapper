"""DYNAMIC_API scraper — replay captured API requests to extract job listings.

This scraper uses the request template extracted during the browser probe
(url, method, headers, payload) to call the dynamic API directly and
extract job listings from the JSON response.

It supports:
- GET and POST APIs
- Pagination (offset/limit based)
- Multiple response structures (top-level list, nested lists, etc.)
"""

from __future__ import annotations

import json

import httpx

from app.core.logger import logger
from app.core.site_utils import absolutize_url
from app.detectors.browser_probe import run_browser_probe
from app.detectors.dynamic_api_detector import (
    detect_dynamic_api,
    score_api_response,
    extract_request_template,
)

# Common job list field names
_JOB_LIST_KEYS = [
    "jobs", "results", "data", "positions", "openings", "items",
    "records", "jobPostings", "job_postings", "job_list", "jobs_list",
    "jobOpenings", "opportunities", "careers",
]

# Field name mappings for normalization
_TITLE_KEYS = ["title", "job_title", "name", "position", "jobTitle", "postingTitle"]
_LOCATION_KEYS = [
    "location", "locationsText", "city", "country", "addressLocality",
    "addressRegion", "locationText", "geo", "workLocation",
]
_URL_KEYS = ["url", "job_url", "link", "href", "apply_url", "applyUrl", "detailUrl", "jobUrl"]
_ID_KEYS = ["id", "job_id", "externalPath", "reqId", "requisition_id", "jobId", "postingId"]


async def scrape_dynamic_api(url: str) -> list[dict]:
    """Scrape jobs from a dynamically detected API endpoint.

    Flow:
    1. Run browser probe to capture API responses
    2. Select the best API using scoring
    3. Extract the request template
    4. Replay the request (with pagination)
    5. Normalize the response into job dicts

    Parameters
    ----------
    url : str
        The career page URL.

    Returns
    -------
    list[dict] — normalized job listings with title, location, url, _raw_api.
    """
    # Step 1: Run browser probe to capture API traffic
    probe = await run_browser_probe(url)
    if not probe.available:
        logger.warning("[DynamicAPI] Browser probe unavailable for %s", url)
        return []

    if not probe.responses:
        logger.warning("[DynamicAPI] No JSON responses captured for %s", url)
        return []

    # Step 2: Score all responses and select the best one
    scored = [(score_api_response(resp), resp) for resp in probe.responses]
    best_score, best_resp = max(scored, key=lambda x: x[0])

    if best_score < 5:
        logger.warning("[DynamicAPI] Best API score %d < 5 — not usable", best_score)
        return []

    logger.info(
        "[DynamicAPI] Using best API: %s (score=%d, body_length=%d)",
        best_resp.url, best_score, best_resp.body_length,
    )

    # Step 3: Extract request template
    template = extract_request_template(best_resp, probe.requests)

    # Step 4: Extract jobs from the captured response first
    jobs = _extract_jobs_from_body(best_resp.body, url)
    if not jobs:
        logger.warning("[DynamicAPI] Could not extract jobs from captured response")
        return []

    logger.info("[DynamicAPI] Extracted %d jobs from captured response", len(jobs))

    # Step 5: Try pagination if the API supports it
    if template.get("payload") or _has_pagination(best_resp.body):
        paginated_jobs = await _paginate_api(template, url)
        if paginated_jobs:
            # Merge: paginated results take priority (more complete)
            jobs = paginated_jobs
            logger.info("[DynamicAPI] Paginated fetch returned %d jobs", len(jobs))

    # Attach raw API data for detail extraction
    for job in jobs:
        job["_raw_api"] = job.pop("_raw_entry", {})

    return jobs


async def _paginate_api(
    template: dict,
    base_url: str,
    max_pages: int = 20,
) -> list[dict]:
    """Attempt pagated API calls using offset/limit pattern.

    Parameters
    ----------
    template : dict
        Request template with url, method, headers, payload.
    base_url : str
        Original career page URL (for absolutizing relative URLs).
    max_pages : int
        Maximum number of pages to fetch.

    Returns
    -------
    list[dict] — all extracted jobs.
    """
    api_url = template["url"]
    method = template.get("method", "GET").upper()
    headers = template.get("headers", {})
    base_payload = template.get("payload") or {}

    # Ensure JSON content type for POST
    if method == "POST":
        headers = {**headers, "Content-Type": "application/json"}

    # Detect pagination fields
    has_offset = any(k in base_payload for k in ("offset", "start", "skip"))
    has_limit = any(k in base_payload for k in ("limit", "count", "pageSize", "size"))

    if not has_offset:
        # Try to infer pagination from response
        if isinstance(base_payload, dict):
            base_payload = {**base_payload, "offset": 0, "limit": 20}
        else:
            base_payload = {"offset": 0, "limit": 20}
        has_offset = True
        has_limit = True

    if not has_limit:
        base_payload["limit"] = 20

    all_jobs: list[dict] = []
    seen_urls: set[str] = set()
    offset = base_payload.get("offset", 0)
    limit = base_payload.get("limit", 20)
    empty_rounds = 0

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        for page in range(max_pages):
            payload = {**base_payload, "offset": offset, "limit": limit}

            try:
                if method == "POST":
                    resp = await client.post(api_url, json=payload, headers=headers)
                else:
                    # GET: add params to URL
                    resp = await client.get(
                        api_url,
                        params=payload,
                        headers=headers,
                    )
                resp.raise_for_status()
                body = resp.json()
            except Exception as exc:
                logger.debug("[DynamicAPI] Pagination page %d failed: %s", page + 1, exc)
                break

            page_jobs = _extract_jobs_from_body(body, base_url)
            new_count = 0
            for job in page_jobs:
                job_url = job.get("url", "").lower()
                job_id = job.get("job_id", "").lower()
                key = job_url or job_id or f"{job.get('title')}|{job.get('location')}"
                if key and key.lower() not in seen_urls:
                    seen_urls.add(key.lower())
                    all_jobs.append(job)
                    new_count += 1

            logger.info(
                "[DynamicAPI] Page %d: %d new jobs (total=%d)",
                page + 1, new_count, len(all_jobs),
            )

            if new_count == 0:
                empty_rounds += 1
                if empty_rounds >= 2:
                    break
            else:
                empty_rounds = 0

            offset += limit

    return all_jobs


def _extract_jobs_from_body(body, base_url: str) -> list[dict]:
    """Extract normalized job dicts from various API response structures.

    Handles:
    - Top-level array: [{"title": ..., "location": ...}, ...]
    - Nested list: {"jobs": [...], "total": 100}
    - Nested objects: {"0": {...}, "1": {...}}
    """
    if body is None:
        return []

    # Find the list of job objects
    job_objects = _find_job_list(body)
    if not job_objects:
        return []

    jobs: list[dict] = []
    for obj in job_objects:
        if not isinstance(obj, dict):
            continue
        job = _normalize_job_entry(obj, base_url)
        if job:
            jobs.append(job)

    return jobs


def _find_job_list(body) -> list[dict]:
    """Find the list of job objects in the API response.

    Tries common patterns in order of preference.
    """
    # Top-level array
    if isinstance(body, list):
        return body

    if not isinstance(body, dict):
        return []

    # Common nested list keys
    for key in _JOB_LIST_KEYS:
        val = body.get(key)
        if isinstance(val, list) and len(val) > 0:
            return val

    # Try to find any list value that contains job-like objects
    for key, val in body.items():
        if isinstance(val, list) and len(val) > 0 and isinstance(val[0], dict):
            # Check if first item has job fields
            fields = set(val[0].keys())
            if _TITLE_KEYS[0] in fields or _LOCATION_KEYS[0] in fields or "title" in fields:
                return val

    # Try nested dict with numeric/string keys (object map)
    all_dicts = []
    for key, val in body.items():
        if isinstance(val, dict):
            all_dicts.append(val)
        elif isinstance(val, list):
            for item in val:
                if isinstance(item, dict):
                    all_dicts.append(item)

    if all_dicts:
        return all_dicts

    return []


def _normalize_job_entry(obj: dict, base_url: str) -> dict | None:
    """Normalize a single job API object into a standard dict."""
    title = _first(obj, _TITLE_KEYS)
    if not title or len(title.strip()) < 2:
        return None

    location = _first(obj, _LOCATION_KEYS)
    if not location:
        # Try to build from parts
        parts = [
            _first(obj, ["city", "addressLocality"]),
            _first(obj, ["state", "addressRegion"]),
            _first(obj, ["country", "addressCountry"]),
        ]
        location = ", ".join(p for p in parts if p)

    raw_url = _first(obj, _URL_KEYS)
    if raw_url:
        job_url = absolutize_url(base_url, raw_url)
    else:
        # Try to construct from ID
        job_id = _first(obj, _ID_KEYS)
        if job_id:
            job_url = absolutize_url(base_url, f"/job/{job_id}")
        else:
            job_url = ""

    if not job_url:
        # Fallback: use a detail URL if available
        job_url = base_url

    result = {
        "title": title.strip(),
        "location": (location or "").strip(),
        "url": job_url,
        "job_id": _first(obj, _ID_KEYS) or "",
        "_raw_entry": obj,
    }

    # Enrich with additional fields if available
    for key in ("department", "employment_type", "posted_date", "salary", "remote_type"):
        val = obj.get(key)
        if val and isinstance(val, str):
            result[key] = val

    return result


def _first(obj: dict, keys: list[str]) -> str:
    """Get the first non-empty string value from the dict for the given keys."""
    for key in keys:
        val = obj.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
        if isinstance(val, dict):
            nested = val.get("value") or val.get("text") or val.get("label")
            if isinstance(nested, str) and nested.strip():
                return nested.strip()
    return ""


def _has_pagination(body) -> bool:
    """Check if the response suggests pagination support."""
    if isinstance(body, dict):
        pagination_keys = [
            "total", "count", "totalResults", "total_jobs",
            "hasMore", "nextPage", "pageInfo",
        ]
        return any(k in body for k in pagination_keys)
    return False
