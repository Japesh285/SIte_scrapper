import json
from urllib.parse import urlparse

import httpx

from app.core.logger import logger
from app.core.site_utils import absolutize_url, get_origin, normalize_site_url


COMMON_JOB_PATHS = (
    "/jobs",
    "/jobs.json",
    "/careers/jobs",
    "/careers/jobs.json",
    "/api/jobs",
    "/api/jobs.json",
    "/api/careers/jobs",
)
ALLOWED_URL_KEYWORDS = ("job", "jobs", "career", "position", "opening", "search", "listing")
JOB_LIST_KEYS = ("jobs", "positions", "results", "openings")
JOB_TEXT_KEYWORDS = ("title", "job", "position", "location", "apply", "career")
UI_NOISE_KEYWORDS = ("menu", "submenu", "layout", "navigation", "nav", "header", "footer")
PAGINATION_KEYS = ("offset", "page", "cursor", "limit", "pages", "pageSize", "totalPages")
MAX_API_CANDIDATES = 8


async def detect_simple_api(
    url: str,
    client: httpx.AsyncClient | None = None,
    discovered_urls: list[str] | None = None,
) -> dict:
    jobs, api_url, score = await fetch_simple_api_jobs(
        url,
        client=client,
        discovered_urls=discovered_urls,
    )
    return {
        "matched": bool(jobs),
        "jobs_found": len(jobs),
        "api_usable": bool(jobs),
        "api_url": api_url or "",
        "confidence": score,
    }


async def fetch_simple_api_jobs(
    url: str,
    client: httpx.AsyncClient | None = None,
    discovered_urls: list[str] | None = None,
) -> tuple[list[dict], str | None, int]:
    normalized_url = normalize_site_url(url)
    if not normalized_url:
        return [], None, 0

    if client is None:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as local_client:
            return await fetch_simple_api_jobs(
                normalized_url,
                client=local_client,
                discovered_urls=discovered_urls,
            )

    best_jobs: list[dict] = []
    best_url: str | None = None
    best_score = -1

    for candidate in _candidate_urls(normalized_url, discovered_urls or []):
        logger.info("[API Candidate] url=%s", candidate)
        validation = await _validate_candidate_api(client, candidate, normalized_url)
        if not validation["accepted"]:
            logger.info("[API Rejected] url=%s reason=%s", candidate, validation["reason"])
            continue

        score = int(validation["score"])
        jobs = list(validation["jobs"])
        logger.info(
            "[API Accepted] url=%s score=%s jobs=%s",
            candidate,
            score,
            len(jobs),
        )
        if score > best_score:
            best_score = score
            best_url = candidate
            best_jobs = jobs

    if best_url:
        logger.info("[API Selected] url=%s score=%s", best_url, best_score)
        return best_jobs, best_url, best_score

    return [], None, 0


def _candidate_urls(url: str, discovered_urls: list[str]) -> list[str]:
    candidates: list[str] = []
    parsed = urlparse(normalize_site_url(url))
    origin = get_origin(url)

    for discovered_url in discovered_urls:
        lowered = discovered_url.lower()
        if any(keyword in lowered for keyword in ALLOWED_URL_KEYWORDS):
            candidates.append(discovered_url)

    if parsed.path and parsed.path not in ("/", ""):
        path_without_query = parsed.path.rstrip("/")
        for candidate in (f"{origin}{path_without_query}", f"{origin}{path_without_query}.json"):
            if any(keyword in candidate.lower() for keyword in ALLOWED_URL_KEYWORDS):
                candidates.append(candidate)

    for path in COMMON_JOB_PATHS:
        candidates.append(f"{origin}{path}")

    seen: set[str] = set()
    ordered: list[str] = []
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        ordered.append(candidate)
        if len(ordered) >= MAX_API_CANDIDATES:
            break
    return ordered


async def _validate_candidate_api(
    client: httpx.AsyncClient, candidate: str, base_url: str
) -> dict:
    try:
        response = await client.get(candidate)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        return {"accepted": False, "reason": f"http_error:{exc.__class__.__name__}"}

    content_type = response.headers.get("content-type", "").lower()
    if "json" not in content_type:
        return {"accepted": False, "reason": "not_json"}

    try:
        payload = response.json()
    except ValueError:
        return {"accepted": False, "reason": "json_parse_failed"}

    if _is_empty_payload(payload):
        return {"accepted": False, "reason": "empty_payload"}

    jobs, signals = _extract_listing_jobs(payload, base_url)
    if len(jobs) == 1:
        return {"accepted": False, "reason": "single_job_endpoint"}
    if _contains_ui_noise(payload):
        return {"accepted": False, "reason": "ui_navigation_payload"}

    text_keyword_hits = _count_json_keyword_hits(payload)
    if not (
        signals["has_job_list_key"]
        or signals["list_object_jobs"]
        or text_keyword_hits >= 3
    ):
        return {"accepted": False, "reason": "missing_job_structure"}

    if not jobs:
        return {"accepted": False, "reason": "no_job_like_fields"}

    score = 0
    if signals["has_job_list_key"]:
        score += 2
    if len(jobs) > 5:
        score += 2
    if _has_pagination_indicators(payload):
        score += 1
    if signals["has_job_fields"]:
        score += 1

    return {
        "accepted": True,
        "jobs": jobs,
        "score": score,
        "reason": "accepted",
    }


def _extract_listing_jobs(payload, base_url: str) -> tuple[list[dict], dict]:
    signals = {
        "has_job_list_key": False,
        "list_object_jobs": False,
        "has_job_fields": False,
    }

    if isinstance(payload, dict):
        for key in JOB_LIST_KEYS:
            value = payload.get(key)
            if isinstance(value, list):
                signals["has_job_list_key"] = True
                jobs = _extract_jobs_from_items(value, base_url)
                if jobs:
                    signals["list_object_jobs"] = True
                    signals["has_job_fields"] = True
                    return jobs, signals

    if isinstance(payload, list):
        jobs = _extract_jobs_from_items(payload, base_url)
        if jobs:
            signals["list_object_jobs"] = True
            signals["has_job_fields"] = True
            return jobs, signals

    jobs = _extract_jobs_from_json(payload, base_url)
    if jobs:
        signals["has_job_fields"] = True
    return jobs, signals


def _extract_jobs_from_items(items: list, base_url: str) -> list[dict]:
    jobs: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = item.get("title") or item.get("job_title")
        location = item.get("location") or item.get("city") or item.get("country")
        url = (
            item.get("url")
            or item.get("link")
            or item.get("absolute_url")
            or item.get("apply_url")
            or item.get("jobUrl")
            or item.get("details_url")
            or ""
        )
        if isinstance(title, str) and title.strip() and location and str(location).strip():
            jobs.append(
                {
                    "title": title.strip(),
                    "location": _normalize_location(location),
                    "url": absolutize_url(base_url, str(url).strip()) if str(url).strip() else "",
                }
            )
    return _dedupe_jobs(jobs)


def _extract_jobs_from_json(data, base_url: str) -> list[dict]:
    jobs: list[dict] = []

    if isinstance(data, list):
        for item in data:
            jobs.extend(_extract_jobs_from_json(item, base_url))
        return _dedupe_jobs(jobs)

    if not isinstance(data, dict):
        return jobs

    title = data.get("title") or data.get("job_title")
    location = data.get("location") or data.get("city") or data.get("country")
    url = (
        data.get("url")
        or data.get("link")
        or data.get("absolute_url")
        or data.get("apply_url")
        or data.get("jobUrl")
        or data.get("details_url")
        or ""
    )

    if isinstance(title, str) and title.strip() and location and str(location).strip():
        jobs.append(
            {
                "title": title.strip(),
                "location": _normalize_location(location),
                "url": absolutize_url(base_url, str(url).strip()) if str(url).strip() else "",
            }
        )

    for key, value in data.items():
        if key not in {
            "title",
            "job_title",
            "location",
            "city",
            "country",
            "url",
            "link",
            "absolute_url",
            "apply_url",
            "jobUrl",
            "details_url",
        }:
            jobs.extend(_extract_jobs_from_json(value, base_url))

    return _dedupe_jobs(jobs)


def _count_json_keyword_hits(payload) -> int:
    json_text = json.dumps(payload, default=str).lower()
    return sum(1 for keyword in JOB_TEXT_KEYWORDS if keyword in json_text)


def _contains_ui_noise(payload) -> bool:
    json_text = json.dumps(payload, default=str).lower()
    return sum(1 for keyword in UI_NOISE_KEYWORDS if keyword in json_text) >= 2


def _has_pagination_indicators(payload) -> bool:
    json_text = json.dumps(payload, default=str)
    lowered = json_text.lower()
    return any(key.lower() in lowered for key in PAGINATION_KEYS)


def _is_empty_payload(payload) -> bool:
    if payload is None:
        return True
    if isinstance(payload, (list, dict)) and not payload:
        return True
    return False


def _dedupe_jobs(jobs: list[dict]) -> list[dict]:
    deduped: list[dict] = []
    seen: set[str] = set()
    for job in jobs:
        key = job.get("url") or f"{job.get('title', '')}|{job.get('location', '')}"
        if key in seen:
            continue
        seen.add(key)
        deduped.append(job)
    return deduped


def _normalize_location(location) -> str:
    if isinstance(location, dict):
        for key in ("name", "city", "label", "location", "country"):
            value = location.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""
    if isinstance(location, list):
        values = [str(item).strip() for item in location if str(item).strip()]
        return ", ".join(values)
    return str(location).strip() if location else ""
