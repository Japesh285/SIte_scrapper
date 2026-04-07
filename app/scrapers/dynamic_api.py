"""DYNAMIC_API scraper — replay captured API requests to extract job listings.

Schema-agnostic extraction that works across Oracle, SAP, Workday, custom APIs.
"""

from __future__ import annotations

import json
import re

import httpx

from app.core.logger import logger
from app.core.site_utils import absolutize_url
from app.detectors.browser_probe import run_browser_probe
from app.detectors.dynamic_api_detector import (
    detect_dynamic_api,
    score_api_response,
    extract_request_template,
)

# ── Flexible field mapping (TASK 3) ────────────────────────────────

_TITLE_KEYS = [
    "title", "jobTitle", "job_title", "requisitionTitle", "positionTitle",
    "name", "postingTitle", "jobTitleText", "roleTitle",
]
_LOCATION_KEYS = [
    "location", "locations", "primaryLocation", "locationText", "locationsText",
    "city", "workLocation", "addressLocality", "addressRegion", "addressCountry",
    "geo", "locationDescription", "locationName",
]
_ID_KEYS = [
    "id", "jobId", "job_id", "requisitionId", "reqId", "postingId",
    "externalPath", "externalId", "requisition_id", "jobCode",
]
_URL_KEYS = [
    "url", "job_url", "link", "href", "apply_url", "applyUrl",
    "detailUrl", "jobUrl", "externalUrl", "redirectUrl", "applyLink",
    "postingUrl",
]

# Common job list field names for direct key lookup
_JOB_LIST_KEYS = [
    "jobs", "items", "data", "postings", "requisitions", "positions",
    "openings", "records", "jobPostings", "job_postings", "job_list",
    "jobs_list", "jobOpenings", "opportunities", "careers", "results",
    "jobList", "vacancies",
]


# ── TASK 1: Generic job array detection ────────────────────────────

def find_job_array(json_data) -> list | None:
    """Recursively find the first list that contains job-like objects.

    Check order:
    1. Direct array (top-level list)
    2. Common keys (jobs, items, data, postings, requisitions)
    3. Recursive nested search for first list with job objects

    Returns the list or None.
    """
    if json_data is None:
        return None

    # ── 1. Direct array ──
    if isinstance(json_data, list):
        logger.info("[DEBUG find_job_array] Step 1: Top-level list with %d items", len(json_data))
        if len(json_data) > 0 and _list_has_job_objects(json_data):
            logger.info("[DEBUG find_job_array] Step 1 PASSED — top-level list has job objects")
            return json_data
        logger.info("[DEBUG find_job_array] Step 1 FAILED — top-level list has no job objects")
        return None

    if not isinstance(json_data, dict):
        return None

    # ── 2. Common keys ──
    for key in _JOB_LIST_KEYS:
        val = json_data.get(key)
        if isinstance(val, list) and len(val) > 0:
            has_jobs = _list_has_job_objects(val)
            logger.info(
                "[DEBUG find_job_array] Step 2: key='%s' list_len=%d has_jobs=%s",
                key, len(val), has_jobs,
            )
            if has_jobs:
                logger.info("[DEBUG find_job_array] Step 2 PASSED — key='%s'", key)
                return val

    logger.info("[DEBUG find_job_array] Step 2 FAILED — no matching keys with job objects")

    # ── 3. Recursive nested search ──
    logger.info("[DEBUG find_job_array] Step 3: Starting recursive search (depth<=4)")
    result = _recursive_find_job_array(json_data, depth=0, max_depth=4)
    if result is not None:
        logger.info("[DEBUG find_job_array] Step 3 PASSED — found array at depth<=4")
    else:
        logger.info("[DEBUG find_job_array] Step 3 FAILED — no job array found recursively")
    return result


def _recursive_find_job_array(data, depth: int, max_depth: int) -> list | None:
    """Recursively search for first list containing job-like objects."""
    if depth > max_depth:
        return None

    if isinstance(data, dict):
        for key, val in data.items():
            if isinstance(val, list) and len(val) > 0:
                if _list_has_job_objects(val):
                    return val
            elif isinstance(val, dict):
                result = _recursive_find_job_array(val, depth + 1, max_depth)
                if result is not None:
                    return result
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, (dict, list)):
                result = _recursive_find_job_array(item, depth + 1, max_depth)
                if result is not None:
                    return result

    return None


def _list_has_job_objects(lst: list) -> bool:
    """Check if a list contains at least one job-like object.

    Scans first 10 items for efficiency.
    """
    sample = lst[:min(10, len(lst))]
    for item in sample:
        if isinstance(item, dict):
            result = is_job_object(item)
            if not result:
                # Debug: show why this object failed
                keys_lower = " ".join(str(k).lower() for k in item.keys())
                has_title = any(k.lower() in keys_lower for k in _TITLE_KEYS)
                has_id = any(k.lower() in keys_lower for k in _ID_KEYS)
                has_location = any(k.lower() in keys_lower for k in ["location", "locations", "primaryLocation", "workLocation"])
                logger.debug(
                    "[DEBUG is_job_object] item keys=%s title=%s id=%s loc=%s",
                    list(item.keys())[:10], has_title, has_id, has_location,
                )
            if result:
                return True
    return False


# ── TASK 2: Job object detection ───────────────────────────────────

# Fields that are ONLY valid if paired with a job-specific field.
# "id" and "name" alone are NOT job signals (e.g. [{"id": 123, "name": "Gurugram"}]).
_JOB_SPECIFIC_KEYS = [
    "title", "jobTitle", "job_title", "requisitionTitle", "positionTitle",
    "postingTitle", "roleTitle", "jobTitleText",
    "jobId", "job_id", "requisitionId", "postingId", "requisition_id",
    "jobCode", "apply", "apply_url", "applyUrl", "applyLink",
    "jobUrl", "detailUrl", "externalUrl", "postingUrl",
    "department", "employment_type", "employmentType",
    "posted_date", "datePosted", "postedOn",
    "jobDescription", "description", "summary",
    "experience", "yearsOfExperience", "seniority",
    "skills", "required_skills", "qualifications",
]


def is_job_object(obj: dict) -> bool:
    """Return TRUE if object contains at least 2 of these job-specific signals:
    - title variant (title, jobTitle, positionTitle, etc.)
    - location variant (location, locations, city, workLocation, etc.)
    - job ID variant (jobId, requisitionId, postingId — NOT bare "id")
    - job URL/apply variant (jobUrl, applyUrl, detailUrl, etc.)
    - job metadata variant (department, employment_type, posted_date, skills, etc.)

    Rejects generic dicts like {"id": 123, "name": "Gurugram"}.
    """
    keys = set(obj.keys())
    keys_lower = {k.lower() for k in keys}

    has_title = bool(keys & set(_TITLE_KEYS))
    has_location = bool(keys & {"location", "locations", "primaryLocation", "workLocation",
                                 "locationText", "locationsText", "city", "addressLocality"})
    has_job_id = bool(keys & {"jobId", "job_id", "requisitionId", "postingId",
                               "requisition_id", "jobCode", "externalId", "externalPath"})
    has_job_url = bool(keys & {"url", "job_url", "link", "href", "apply_url", "applyUrl",
                                "detailUrl", "jobUrl", "externalUrl", "redirectUrl", "applyLink",
                                "postingUrl"})
    has_job_meta = bool(keys_lower & {k.lower() for k in _JOB_SPECIFIC_KEYS})

    score = sum([has_title, has_location, has_job_id, has_job_url, has_job_meta])
    return score >= 2


# ── TASK 3: Flexible field mapping ─────────────────────────────────

def _map_job_fields(obj: dict, base_url: str) -> dict | None:
    """Extract and normalize job fields from an API object.

    Returns dict with title, location, url, job_id — or None if invalid.
    """
    title = _first(obj, _TITLE_KEYS)
    if not title or len(title.strip()) < 2:
        return None

    # Location: try direct, then build from parts
    location = _first(obj, _LOCATION_KEYS)
    if not location:
        parts = [
            _first(obj, ["city", "addressLocality"]),
            _first(obj, ["state", "addressRegion", "province"]),
            _first(obj, ["country", "addressCountry", "countryCode"]),
        ]
        location = ", ".join(p for p in parts if p)

    # URL: try direct, then construct from ID
    job_url = _build_job_url(obj, base_url)

    job_id = _first(obj, _ID_KEYS)

    result = {
        "title": title.strip(),
        "location": (location or "").strip(),
        "url": job_url,
        "job_id": job_id,
        "_raw_entry": obj,
    }

    # Enrich with extra fields if available
    for key in ("department", "employment_type", "employmentType", "posted_date",
                 "postedOn", "salary", "remote_type", "remoteType", "workType"):
        val = obj.get(key)
        if val and isinstance(val, str):
            result[key] = val

    return result


def _build_job_url(obj: dict, base_url: str) -> str:
    """Construct job URL from available fields."""
    raw_url = _first(obj, _URL_KEYS)
    if raw_url:
        return absolutize_url(base_url, raw_url)

    job_id = _first(obj, _ID_KEYS)
    if job_id:
        return absolutize_url(base_url, f"/job/{job_id}")

    return base_url


def _first(obj: dict, keys: list[str]) -> str:
    """Get first non-empty string value from dict for given keys."""
    for key in keys:
        val = obj.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
        if isinstance(val, dict):
            nested = val.get("value") or val.get("text") or val.get("label")
            if isinstance(nested, str) and nested.strip():
                return nested.strip()
    return ""


# ── Direct API scraper (NO browser) ─────────────────────────────────

async def scrape_dynamic_api_direct(
    api_url: str,
    method: str,
    payload: dict | str | None,
    headers: dict,
    base_url: str,
    max_pages: int = 20,
) -> list[dict]:
    """Call the detected API directly with httpx — NO browser.

    Parameters
    ----------
    api_url : str
        The detected API endpoint URL.
    method : str
        HTTP method (GET or POST).
    payload : dict | str | None
        POST body (for POST APIs). If dict, sent as JSON.
    headers : dict
        Request headers from the captured request.
    base_url : str
        Original career page URL (for absolutizing relative job URLs).
    max_pages : int
        Maximum pagination rounds.

    Returns
    -------
    list[dict] — normalized job listings with title, location, url, _raw_entry.
    """
    logger.info(
        "[DYNAMIC_API_DIRECT] Calling %s method=%s",
        api_url, method,
    )

    method = method.upper()
    # Strip browser-specific headers
    clean_headers = {
        k: v for k, v in (headers or {}).items()
        if k.lower() not in ("host", "origin", "referer", "sec-fetch-dest",
                              "sec-fetch-mode", "sec-fetch-site", "sec-ch-ua",
                              "sec-ch-ua-mobile", "sec-ch-ua-platform", "accept-encoding")
    }
    clean_headers.setdefault("Accept", "application/json")
    clean_headers.setdefault("Content-Type", "application/json")

    all_jobs: list[dict] = []
    seen_urls: set[str] = set()
    offset = 0
    limit = 20
    empty_rounds = 0

    # Detect pagination fields from payload
    has_pagination = False
    if isinstance(payload, dict):
        if any(k in payload for k in ("offset", "start", "page", "skip")):
            has_pagination = True
        if any(k in payload for k in ("limit", "count", "pageSize", "size")):
            limit = payload.get("limit", payload.get("count", payload.get("pageSize", 20)))

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        for page in range(max_pages):
            # Build request
            if method == "POST":
                req_payload = dict(payload) if isinstance(payload, dict) else {}
                if has_pagination:
                    req_payload["offset"] = offset
                    req_payload["limit"] = limit
                try:
                    resp = await client.post(api_url, json=req_payload, headers=clean_headers)
                except Exception as exc:
                    logger.debug("[DYNAMIC_API_DIRECT] POST page %d failed: %s", page + 1, exc)
                    break
            else:
                params = {}
                if has_pagination:
                    params["offset"] = offset
                    params["limit"] = limit
                try:
                    resp = await client.get(api_url, params=params, headers=clean_headers)
                except Exception as exc:
                    logger.debug("[DYNAMIC_API_DIRECT] GET page %d failed: %s", page + 1, exc)
                    break

            if resp.status_code != 200:
                logger.debug("[DYNAMIC_API_DIRECT] HTTP %d on page %d", resp.status_code, page + 1)
                break

            try:
                body = resp.json()
            except Exception:
                logger.debug("[DYNAMIC_API_DIRECT] Non-JSON response on page %d", page + 1)
                break

            # Extract jobs
            page_jobs = _extract_jobs_from_body(body, base_url)
            new_count = 0
            for job in page_jobs:
                job_url = job.get("url", "").lower()
                job_id = job.get("job_id", "").lower()
                key = job_url or job_id or f"{job.get('title')}|{job.get('location')}"
                if key and key not in seen_urls:
                    seen_urls.add(key)
                    all_jobs.append(job)
                    new_count += 1

            logger.info(
                "[DYNAMIC_API_DIRECT] Page %d: %d new jobs (total=%d)",
                page + 1, new_count, len(all_jobs),
            )

            if new_count == 0:
                empty_rounds += 1
                if empty_rounds >= 2:
                    break
            else:
                empty_rounds = 0

            offset += limit

    logger.info("[DYNAMIC_API_DIRECT] Total jobs extracted: %d", len(all_jobs))

    # Attach raw API data for detail extraction
    for job in all_jobs:
        job["_raw_api"] = job.pop("_raw_entry", {})

    return all_jobs


# ── Main scraper (browser fallback) ─────────────────────────────────

async def scrape_dynamic_api(url: str) -> list[dict]:
    """Scrape jobs from a dynamically detected API endpoint.

    Flow:
    1. Run browser probe to capture API responses
    2. Select the best API using scoring
    3. Extract the request template
    4. Extract jobs from captured response (schema-agnostic)
    5. Try pagination if supported

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

    # Step 4: Extract jobs from the captured response (schema-agnostic)
    jobs = _extract_jobs_from_body(best_resp.body, url)

    if not jobs:
        logger.warning("[DynamicAPI] Extraction failed — falling back to DOM")
        return []

    logger.info("[DynamicAPI] Extracted %d jobs from captured response", len(jobs))

    # Step 5: Try pagination if the API supports it
    if template.get("payload") or _has_pagination(best_resp.body):
        paginated_jobs = await _paginate_api(template, url)
        if paginated_jobs:
            jobs = paginated_jobs
            logger.info("[DynamicAPI] Paginated fetch returned %d jobs", len(jobs))

    # Attach raw API data for detail extraction
    for job in jobs:
        job["_raw_api"] = job.pop("_raw_entry", {})

    return jobs


# ── Pagination ──────────────────────────────────────────────────────

async def _paginate_api(
    template: dict,
    base_url: str,
    max_pages: int = 20,
) -> list[dict]:
    """Attempt paginated API calls using offset/limit pattern."""
    api_url = template["url"]
    method = template.get("method", "GET").upper()
    headers = template.get("headers", {})
    base_payload = template.get("payload") or {}

    if method == "POST":
        headers = {**headers, "Content-Type": "application/json"}

    # Infer pagination fields
    has_offset = any(k in base_payload for k in ("offset", "start", "skip"))
    has_limit = any(k in base_payload for k in ("limit", "count", "pageSize", "size"))

    if not has_offset:
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
                    resp = await client.get(api_url, params=payload, headers=headers)
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


# ── Extraction (TASK 1 + TASK 2 + TASK 3) ──────────────────────────

def _extract_jobs_from_body(body, base_url: str) -> list[dict]:
    """Extract normalized job dicts from ANY API response structure.

    Schema-agnostic: handles top-level arrays, nested lists, object maps,
    deeply nested structures (Oracle, SAP, etc).
    """
    if body is None:
        return []

    # ── DEBUG: Log full response structure ──
    _debug_log_response_structure(body)

    # TASK 1: Find the job array
    job_array = find_job_array(body)
    if job_array is None:
        logger.warning("[DynamicAPI] No job array found in response")
        return []

    logger.info("[DynamicAPI] Job array found: size=%d", len(job_array))

    # TASK 2 + 3: Map each object to a normalized job dict
    jobs: list[dict] = []
    for obj in job_array:
        if not isinstance(obj, dict):
            continue
        job = _map_job_fields(obj, base_url)
        if job:
            jobs.append(job)

    if not jobs:
        logger.warning("[DynamicAPI] Extraction failed — no valid job objects")
        return []

    logger.info("[DynamicAPI] Extracted jobs: %d", len(jobs))
    return jobs


def _debug_log_response_structure(body) -> None:
    """Debug: log full response structure to understand JSON shape."""
    try:
        response_str = json.dumps(body, indent=2, ensure_ascii=False)
        logger.info("[DEBUG API RESPONSE] %s", response_str[:2000])
    except Exception:
        logger.warning("[DEBUG API RESPONSE] Failed to serialize response")

    # Log top-level keys
    if isinstance(body, dict):
        top_keys = list(body.keys())
        logger.info("[DEBUG] Top-level keys: %s", top_keys)
    elif isinstance(body, list):
        logger.info("[DEBUG] Top-level is list with %d items", len(body))
        if len(body) > 0 and isinstance(body[0], dict):
            logger.info("[DEBUG] First item keys: %s", list(body[0].keys()))

    # Recursively find all arrays
    arrays = find_arrays(body)
    for path, arr in arrays:
        sample_type = ""
        if len(arr) > 0 and isinstance(arr[0], dict):
            sample_type = f" first_keys={list(arr[0].keys())[:8]}"
        logger.info("[DEBUG] Found array at %s (length=%d)%s", path, len(arr), sample_type)


def find_arrays(obj, path: str = "root", max_depth: int = 6, _depth: int = 0) -> list[tuple[str, list]]:
    """Recursively find all arrays in a JSON structure.

    Returns list of (path, array) tuples.

    Parameters
    ----------
    obj : JSON value (dict, list, str, int, etc.)
    path : Current dot-path string
    max_depth : Maximum recursion depth
    _depth : Internal depth counter
    """
    if _depth > max_depth:
        return []

    results = []

    if isinstance(obj, dict):
        for key, val in obj.items():
            child_path = f"{path}.{key}"
            if isinstance(val, list):
                results.append((child_path, val))
                # Also recurse into list items to find nested arrays
                if len(val) > 0 and isinstance(val[0], dict):
                    for i, item in enumerate(val[:3]):
                        nested = find_arrays(item, f"{child_path}[{i}]", max_depth, _depth + 1)
                        results.extend(nested)
            elif isinstance(val, dict):
                results.extend(find_arrays(val, child_path, max_depth, _depth + 1))
    elif isinstance(obj, list):
        for i, item in enumerate(obj[:3]):
            child_path = f"{path}[{i}]"
            if isinstance(item, list):
                results.append((child_path, item))
            elif isinstance(item, dict):
                results.extend(find_arrays(item, child_path, max_depth, _depth + 1))

    return results


def _has_pagination(body) -> bool:
    """Check if the response suggests pagination support."""
    if isinstance(body, dict):
        pagination_keys = [
            "total", "count", "totalResults", "total_jobs",
            "hasMore", "nextPage", "pageInfo",
        ]
        return any(k in body for k in pagination_keys)
    return False
