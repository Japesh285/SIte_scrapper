"""Taleo / Oracle HCM detector."""

import re
from urllib.parse import urljoin, urlparse

import httpx

from app.core.site_utils import get_origin, normalize_site_url
from app.core.logger import logger

TALEO_HTML_PATTERNS = [
    re.compile(r'taleo\.net', re.IGNORECASE),
    re.compile(r'careersection', re.IGNORECASE),
    re.compile(r'taleoCandidatePortal', re.IGNORECASE),
    re.compile(r'oracle.*recruit', re.IGNORECASE),
    # Note: oraclecloud.com/hcm is handled by the dedicated oracle_hcm detector
]

# Common Taleo listing API path patterns
TALEO_API_PATHS = [
    "/careersection/rest/jobboard/visible/job-search.json",
    "/en/talent/taleo/web/en/careersection/rest/jobboard/visible/job-search.json",
    "/talent/taleo/web/en/careersection/rest/jobboard/visible/job-search.json",
]

TALEO_API_PARAMS = {
    "multiline": "true",
    "radialLocationComparison": "WITHIN",
    "target": "APPLY",
    "lang": "en",
    "startRow": 0,
    "numItems": 100,
}


async def detect_taleo(
    url: str,
    client: httpx.AsyncClient | None = None,
    html: str = "",
    discovered_urls: list[str] | None = None,
) -> dict:
    normalized_url = normalize_site_url(url)
    origin = get_origin(normalized_url)

    taleo_in_html = any(p.search(html) for p in TALEO_HTML_PATTERNS)

    # Also check discovered URLs (browser probe results)
    if not taleo_in_html and discovered_urls:
        taleo_in_html = any("taleo.net" in u.lower() or "careersection" in u.lower() for u in discovered_urls)

    if not taleo_in_html:
        return {"matched": False, "jobs_found": 0, "api_usable": False, "api_url": ""}

    close_client = client is None
    if close_client:
        client = httpx.AsyncClient(timeout=20, follow_redirects=True)

    try:
        # Try API paths on the origin
        api_candidates = [f"{origin}{path}" for path in TALEO_API_PATHS]

        # Also check discovered URLs for Taleo API patterns
        if discovered_urls:
            for du in discovered_urls:
                if "taleo.net" in du.lower() and "job-search" in du.lower():
                    api_candidates.insert(0, du)

        for api_url in api_candidates:
            jobs = await fetch_taleo_jobs(client, api_url)
            if jobs:
                logger.info("[TALEO] Locked API: %s (%d jobs)", api_url, len(jobs))
                return {
                    "matched": True,
                    "jobs_found": len(jobs),
                    "api_usable": True,
                    "api_url": api_url,
                }

        return {"matched": True, "jobs_found": 0, "api_usable": False, "api_url": ""}
    finally:
        if close_client:
            await client.aclose()


async def fetch_taleo_jobs(
    client: httpx.AsyncClient,
    api_url: str,
) -> list[dict]:
    """Paginate through Taleo listing API and return all jobs."""
    jobs: list[dict] = []
    seen_ids: set[str] = set()
    start_row = 0
    num_items = 100
    max_rows = 10000

    while start_row < max_rows:
        params = {**TALEO_API_PARAMS, "startRow": start_row, "numItems": num_items}
        try:
            res = await client.get(
                api_url,
                params=params,
                headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"},
            )
            if res.status_code != 200:
                break
            if "json" not in res.headers.get("content-type", ""):
                break
            data = res.json()
        except Exception as exc:
            logger.debug("[TALEO] Fetch failed at %s: %s", api_url, exc)
            break

        batch = _parse_taleo_response(data, api_url)
        if not batch:
            break

        added = 0
        for job in batch:
            key = job.get("job_id") or job.get("url") or job.get("title", "")
            if key in seen_ids:
                continue
            seen_ids.add(key)
            jobs.append(job)
            added += 1

        if added == 0:
            break
        start_row += num_items

    return jobs


def _parse_taleo_response(data: dict, base_url: str) -> list[dict]:
    reqs = (
        data.get("requisitionList")
        or data.get("jobs")
        or data.get("results")
        or []
    )
    if not isinstance(reqs, list):
        return []

    origin = get_origin(base_url)
    jobs = []

    for req in reqs:
        if not isinstance(req, dict):
            continue
        title = (
            req.get("title")
            or req.get("jobTitle")
            or req.get("PositionTitle")
            or ""
        ).strip()
        if not title:
            continue

        location = req.get("primaryLocation") or req.get("location") or req.get("city") or ""
        if isinstance(location, dict):
            location = location.get("descriptor") or location.get("name") or ""

        job_id = str(req.get("requisitionId") or req.get("jobId") or req.get("id") or "")

        job_url = req.get("applyUrl") or req.get("jobUrl") or req.get("detailUrl") or ""
        if not job_url and job_id:
            job_url = f"{origin}/careersection/jobdetail.ftl?lang=en&job={job_id}"

        jobs.append({
            "title": title,
            "location": str(location),
            "url": str(job_url),
            "job_id": job_id,
            "_raw_api": req,
        })

    return jobs
