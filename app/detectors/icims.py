"""iCIMS detector."""

import json
import re
from urllib.parse import urlparse

import httpx

from app.core.site_utils import get_origin, normalize_site_url
from app.core.logger import logger

ICIMS_HTML_PATTERNS = [
    re.compile(r'icims\.com', re.IGNORECASE),
    re.compile(r'"iCIMS"', re.IGNORECASE),
    re.compile(r"icims-job", re.IGNORECASE),
]

# Pattern to extract customer + portal IDs from HTML (REST API path)
ICIMS_IDS_PATTERN = re.compile(
    r'customers[/\\](\d+)[/\\]jobportals[/\\](\d+)',
    re.IGNORECASE,
)

# Pattern to find iCIMS subdomain used as a career portal
ICIMS_SUBDOMAIN_PATTERN = re.compile(
    r'https?://([a-zA-Z0-9_-]+)\.icims\.com',
    re.IGNORECASE,
)


async def detect_icims(
    url: str,
    client: httpx.AsyncClient | None = None,
    html: str = "",
    discovered_urls: list[str] | None = None,
) -> dict:
    normalized_url = normalize_site_url(url)

    icims_in_html = any(p.search(html) for p in ICIMS_HTML_PATTERNS)

    if not icims_in_html and discovered_urls:
        icims_in_html = any("icims.com" in u.lower() for u in discovered_urls)

    if not icims_in_html:
        return {"matched": False, "jobs_found": 0, "api_usable": False, "api_url": ""}

    close_client = client is None
    if close_client:
        client = httpx.AsyncClient(timeout=20, follow_redirects=True)

    try:
        # Strategy 1: iCIMS subdomain found in HTML or discovered URLs
        all_text = html + " " + " ".join(discovered_urls or [])
        subdomain_match = ICIMS_SUBDOMAIN_PATTERN.search(all_text)
        if subdomain_match:
            subdomain = subdomain_match.group(1)
            search_url = f"https://{subdomain}.icims.com/jobs/search"
            jobs = await _fetch_icims_search_jobs(client, search_url, normalized_url)
            if jobs:
                logger.info("[iCIMS] Search endpoint locked: %s (%d jobs)", search_url, len(jobs))
                return {"matched": True, "jobs_found": len(jobs), "api_usable": True, "api_url": search_url}

        # Strategy 2: Try /jobs/search on the same origin — only if icims.com appears in HTML
        # (not just a CSS class like icims-job, which can appear on non-iCIMS sites)
        strong_signal = bool(
            re.search(r'icims\.com', html, re.IGNORECASE)
            or re.search(r'"iCIMS"', html, re.IGNORECASE)
            or (discovered_urls and any("icims.com" in u.lower() for u in discovered_urls))
        )
        if strong_signal:
            origin = get_origin(normalized_url)
            search_url = f"{origin}/jobs/search"
            jobs = await _fetch_icims_search_jobs(client, search_url, normalized_url)
            if jobs:
                logger.info("[iCIMS] Origin search locked: %s (%d jobs)", search_url, len(jobs))
                return {"matched": True, "jobs_found": len(jobs), "api_usable": True, "api_url": search_url}

        return {"matched": True, "jobs_found": 0, "api_usable": False, "api_url": ""}
    finally:
        if close_client:
            await client.aclose()


async def fetch_icims_jobs(
    client: httpx.AsyncClient,
    api_url: str,
    base_url: str = "",
) -> list[dict]:
    return await _fetch_icims_search_jobs(client, api_url, base_url)


async def _fetch_icims_search_jobs(
    client: httpx.AsyncClient,
    search_url: str,
    base_url: str,
) -> list[dict]:
    try:
        res = await client.get(
            search_url,
            params={"ss": 1, "searchKeyword": "", "searchLocation": "", "in_iframe": 1},
            headers={"User-Agent": "Mozilla/5.0", "Accept": "text/html,application/json"},
        )
        if res.status_code != 200:
            return []

        content_type = res.headers.get("content-type", "")

        # JSON response
        if "json" in content_type:
            try:
                return _parse_icims_data(res.json(), base_url)
            except Exception:
                pass

        # HTML response with embedded JSON state
        html = res.text
        for pattern in [
            r'window\.__INITIAL_STATE__\s*=\s*(\{.*?\});',
            r'var\s+jobData\s*=\s*(\{.*?\});',
            r'<script[^>]+type="application/json"[^>]*>(\{.*?\})</script>',
        ]:
            match = re.search(pattern, html, re.DOTALL)
            if match:
                try:
                    state = json.loads(match.group(1))
                    jobs = _parse_icims_data(state, base_url)
                    if jobs:
                        return jobs
                except Exception:
                    pass

        return []
    except Exception as exc:
        logger.debug("[iCIMS] Search fetch failed: %s", exc)
        return []


def _parse_icims_data(data, base_url: str) -> list[dict]:
    if isinstance(data, dict):
        items = (
            data.get("jobs")
            or data.get("results")
            or data.get("items")
            or data.get("postings")
            or []
        )
    elif isinstance(data, list):
        items = data
    else:
        return []

    jobs = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = (item.get("title") or item.get("jobtitle") or item.get("jobTitle") or "").strip()
        if not title:
            continue

        location = item.get("location") or item.get("city") or item.get("locationName") or ""
        if isinstance(location, dict):
            location = location.get("name") or location.get("text") or ""

        job_url = (
            item.get("url")
            or item.get("jobUrl")
            or item.get("applyUrl")
            or item.get("absolute_url")
            or ""
        )
        job_id = str(item.get("id") or item.get("jobId") or item.get("requisitionId") or "")

        jobs.append({
            "title": title,
            "location": str(location),
            "url": str(job_url),
            "job_id": job_id,
            "_raw_api": item,
        })

    return jobs
