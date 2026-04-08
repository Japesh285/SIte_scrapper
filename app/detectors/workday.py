"""Workday API detector — clean, locked, no over-engineering."""

import re
from urllib.parse import parse_qs, urljoin, urlparse

import httpx

from app.core.site_utils import get_origin, normalize_site_url
from app.core.logger import logger

WORKDAY_API_PATTERN = re.compile(r"(https?:)?//[^\"'\s]+/wday/cxs/[^\"'\s]+", re.IGNORECASE)
WORKDAY_COMPANY_PATTERN = re.compile(
    r'"company"\s*:\s*"([^"]+)"|"tenant"\s*:\s*"([^"]+)"',
    re.IGNORECASE,
)


def normalize_external_path(path: str) -> str:
    """Normalize externalPath for detail API call.

    Rules:
    - Remove leading "/"
    - Remove "job/" prefix if present
    """
    if not path:
        return ""
    path = path.strip().lstrip("/")
    if path.startswith("job/"):
        path = path[len("job/"):]
    return path


def parse_workday_config(api_url: str) -> dict | None:
    """Extract tenant + site + base from Workday API URL.

    e.g. https://aig.wd1.myworkdayjobs.com/wday/cxs/aig/aig/jobs
    → {"tenant": "aig", "site": "aig", "base": "https://aig.wd1.myworkdayjobs.com"}
    """
    try:
        parsed = urlparse(api_url)
        host_parts = parsed.netloc.split(".")
        tenant = host_parts[0]
        server = host_parts[1]

        path_parts = [p for p in parsed.path.split("/") if p]
        # Expected: wday / cxs / tenant / site / jobs
        if len(path_parts) < 5 or path_parts[0] != "wday" or path_parts[1] != "cxs":
            return None

        site = path_parts[3]
        base = f"{parsed.scheme}://{tenant}.{server}.myworkdayjobs.com"

        return {"tenant": tenant, "site": site, "base": base}
    except Exception:
        return None


async def detect_workday(
    url: str,
    client: httpx.AsyncClient | None = None,
    html: str | None = None,
    discovered_urls: list[str] | None = None,
) -> dict:
    api_url, jobs = await fetch_workday_jobs(
        url,
        client=client,
        html=html,
        discovered_urls=discovered_urls,
    )
    return {
        "matched": bool(api_url and jobs),
        "jobs_found": len(jobs),
        "api_usable": bool(api_url and jobs),
        "api_url": api_url or "",
    }


async def fetch_workday_jobs(
    url: str,
    client: httpx.AsyncClient | None = None,
    html: str | None = None,
    discovered_urls: list[str] | None = None,
) -> tuple[str | None, list[dict]]:
    normalized_url = normalize_site_url(url)
    if not normalized_url:
        return None, []

    if client is not None:
        return await _fetch_workday_jobs_with_client(
            client, normalized_url, html=html, discovered_urls=discovered_urls,
        )

    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as local_client:
        return await _fetch_workday_jobs_with_client(
            local_client, normalized_url, html=html, discovered_urls=discovered_urls,
        )


async def _fetch_workday_jobs_with_client(
    client: httpx.AsyncClient,
    url: str,
    html: str | None = None,
    discovered_urls: list[str] | None = None,
) -> tuple[str | None, list[dict]]:
    """Fetch Workday jobs. Once a valid API is found, LOCK it — no trying other candidates."""
    page_html = html
    if page_html is None:
        try:
            response = await client.get(url)
            response.raise_for_status()
            page_html = response.text
        except httpx.HTTPError:
            return None, []

    # Build candidate list but try each one ONCE — lock on first success
    candidates = _build_workday_candidates(url, page_html or "", discovered_urls or [])
    for candidate in candidates:
        jobs = await _fetch_workday_postings(client, url, candidate)
        if jobs:
            logger.info("[WORKDAY] API locked: %s (%d jobs)", candidate, len(jobs))
            return candidate, jobs
        # Skip non-working candidates silently

    return None, []


def _build_workday_candidates(url: str, html: str, discovered_urls: list[str]) -> list[str]:
    """Build candidate Workday API URLs. Only /jobs endpoints."""
    candidates: list[str] = []
    seen: set[str] = set()

    for discovered_url in discovered_urls:
        if "workday" in discovered_url.lower():
            _add_candidate(discovered_url, url, candidates, seen)

    for match in WORKDAY_API_PATTERN.finditer(html):
        _add_candidate(match.group(0), url, candidates, seen)

    if "workday" in url.lower():
        parsed_origin = get_origin(url)
        parsed = urlparse(normalize_site_url(url))
        path_segments = [segment for segment in parsed.path.split("/") if segment]
        if path_segments:
            company_segment = path_segments[0]
            fallback = f"{parsed_origin}/wday/cxs/{company_segment}/{company_segment}/jobs"
            if fallback not in seen:
                seen.add(fallback)
                candidates.append(fallback)

    parsed = urlparse(normalize_site_url(url))
    for token in _extract_company_tokens(html):
        fallback = f"{parsed.scheme}://{parsed.netloc}/wday/cxs/{token}/{token}/jobs"
        if fallback not in seen:
            seen.add(fallback)
            candidates.append(fallback)

    return candidates


async def _fetch_workday_postings(
    client: httpx.AsyncClient, source_url: str, api_url: str
) -> list[dict]:
    """POST /wday/cxs/{tenant}/{site}/jobs — with JSON validation."""
    applied_facets = _build_workday_applied_facets(source_url)
    jobs: list[dict] = []
    seen_urls: set[str] = set()
    offset = 0
    limit = 20

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    while True:
        try:
            response = await client.post(
                api_url,
                json={
                    "limit": limit,
                    "offset": offset,
                    "searchText": "",
                    "appliedFacets": applied_facets,
                },
                headers=headers,
            )
            # Validate status code
            if response.status_code != 200:
                logger.warning("[WORKDAY] Listing fetch failed status=%d", response.status_code)
                break

            # Validate content-type before .json()
            content_type = response.headers.get("content-type", "")
            if "application/json" not in content_type:
                logger.warning("[WORKDAY] Non-JSON response (content-type=%s)", content_type)
                break

            payload = response.json()

        except (ValueError, httpx.HTTPError) as e:
            logger.warning("[WORKDAY] Request failed: %s", e)
            break

        postings = payload.get("jobPostings") or []
        if not postings:
            break

        added = 0
        for posting in postings:
            normalized = _normalize_workday_job(posting, source_url)
            if not normalized:
                continue
            job_url = normalized["url"].lower()
            if job_url in seen_urls:
                continue
            seen_urls.add(job_url)
            jobs.append(normalized)
            added += 1

        if added == 0:
            break
        offset += limit

    return jobs


def _normalize_workday_job(posting: dict, source_url: str) -> dict | None:
    """Normalize a single job posting from listing response."""
    title = str(posting.get("title") or "").strip()
    external_path = str(posting.get("externalPath") or "").strip()

    if not title or not external_path:
        return None

    location = str(
        posting.get("locationsText") or posting.get("location") or ""
    ).strip()

    # Build public URL using source_url
    job_url = urljoin(source_url, external_path)

    return {
        "title": title,
        "location": location,
        "url": job_url,
        "external_path": external_path,
    }


def _add_candidate(candidate: str, base_url: str, candidates: list[str], seen: set[str]) -> None:
    normalized_candidate = candidate
    if normalized_candidate.startswith("//"):
        normalized_candidate = f"https:{normalized_candidate}"
    elif not normalized_candidate.startswith("http"):
        normalized_candidate = urljoin(get_origin(base_url), normalized_candidate)

    normalized_candidate = normalized_candidate.rstrip("/")
    # Only add /jobs endpoint — skip sidebarimage, userprofile, approot, etc.
    jobs_candidate = normalized_candidate if normalized_candidate.endswith("/jobs") else f"{normalized_candidate}/jobs"
    if jobs_candidate not in seen:
        seen.add(jobs_candidate)
        candidates.append(jobs_candidate)


def _extract_company_tokens(html: str) -> list[str]:
    tokens: list[str] = []
    seen: set[str] = set()
    for match in WORKDAY_COMPANY_PATTERN.finditer(html):
        candidate = (match.group(1) or match.group(2) or "").strip()
        if candidate and candidate not in seen:
            seen.add(candidate)
            tokens.append(candidate)
    return tokens


def _build_workday_applied_facets(url: str) -> dict[str, list[str]]:
    ignored_keys = {"limit", "offset", "page", "sortby", "sortorder"}
    parsed = urlparse(url)
    query_params = parse_qs(parsed.query, keep_blank_values=False)
    applied_facets: dict[str, list[str]] = {}

    for key, values in query_params.items():
        if key.lower() in ignored_keys:
            continue
        cleaned_values = [str(value).strip() for value in values if str(value).strip()]
        if cleaned_values:
            applied_facets[key] = cleaned_values

    return applied_facets


async def fetch_workday_job_detail(
    client: httpx.AsyncClient,
    config: dict,
    external_path: str,
) -> dict | None:
    """GET /wday/cxs/{tenant}/{site}/job/{clean_path} — with JSON validation.

    NEVER mutates the path beyond normalization.
    NEVER adds "job/job".
    """
    clean_path = normalize_external_path(external_path)
    if not clean_path:
        return None

    detail_url = f"{config['base']}/wday/cxs/{config['tenant']}/{config['site']}/job/{clean_path}"
    logger.info("[API] Fetching detail: %s", detail_url)

    headers = {
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0",
        "Referer": f"{config['base']}/en-US/{config['site']}",
    }

    try:
        res = await client.get(detail_url, headers=headers)

        # Validate status code
        if res.status_code != 200:
            logger.warning("[API] Skipped (status=%d): %s", res.status_code, detail_url)
            return None

        # Validate content-type before .json()
        content_type = res.headers.get("content-type", "")
        if "application/json" not in content_type:
            logger.warning("[API] Skipped (not JSON, content-type=%s): %s", content_type, detail_url)
            return None

        detail_json = res.json()
        logger.info("[API] Success: %s", detail_url)
        return detail_json

    except (ValueError, httpx.HTTPError) as e:
        logger.warning("[API] Error: %s — %s", detail_url, e)
        return None
