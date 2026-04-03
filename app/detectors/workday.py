import re
from urllib.parse import parse_qs, urljoin, urlparse

import httpx

from app.core.site_utils import get_origin, normalize_site_url


WORKDAY_API_PATTERN = re.compile(r"(https?:)?//[^\"'\s]+/wday/cxs/[^\"'\s]+", re.IGNORECASE)
WORKDAY_COMPANY_PATTERN = re.compile(
    r'"company"\s*:\s*"([^"]+)"|"tenant"\s*:\s*"([^"]+)"',
    re.IGNORECASE,
)


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
            client,
            normalized_url,
            html=html,
            discovered_urls=discovered_urls,
        )

    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as local_client:
        return await _fetch_workday_jobs_with_client(
            local_client,
            normalized_url,
            html=html,
            discovered_urls=discovered_urls,
        )


async def _fetch_workday_jobs_with_client(
    client: httpx.AsyncClient,
    url: str,
    html: str | None = None,
    discovered_urls: list[str] | None = None,
) -> tuple[str | None, list[dict]]:
    page_html = html
    if page_html is None:
        try:
            response = await client.get(url)
            response.raise_for_status()
            page_html = response.text
        except httpx.HTTPError:
            return None, []

    candidates = _build_workday_candidates(url, page_html or "", discovered_urls or [])
    for candidate in candidates:
        try:
            jobs = await _fetch_workday_postings(client, url, candidate)
            if jobs:
                return candidate, jobs
        except (ValueError, httpx.HTTPError):
            continue

    return None, []


def _build_workday_candidates(url: str, html: str, discovered_urls: list[str]) -> list[str]:
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


def _extract_workday_jobs(payload: dict, source_url: str) -> list[dict]:
    postings = payload.get("jobPostings", [])
    jobs: list[dict] = []

    if not isinstance(postings, list):
        return jobs

    for posting in postings:
        if not isinstance(posting, dict):
            continue

        title = str(posting.get("title", "")).strip()
        relative_url = posting.get("externalPath") or posting.get("bulletFields", {}).get("url") or ""
        location = posting.get("locationsText") or posting.get("location") or ""

        if not title or not relative_url:
            continue

        jobs.append(
            {
                "title": title,
                "location": str(location).strip(),
                "url": urljoin(source_url, str(relative_url)),
            }
        )

    return jobs


async def _fetch_workday_postings(
    client: httpx.AsyncClient, source_url: str, api_url: str
) -> list[dict]:
    parsed = urlparse(normalize_site_url(source_url))
    tenant = parsed.netloc.split(".")[0]
    site = next((part for part in parsed.path.split("/") if part), "")
    if not tenant or not site:
        return []

    applied_facets = _build_workday_applied_facets(source_url)
    jobs: list[dict] = []
    seen_urls: set[str] = set()
    offset = 0
    limit = 20

    while True:
        response = await client.post(
            api_url,
            json={
                "limit": limit,
                "offset": offset,
                "searchText": "",
                "appliedFacets": applied_facets,
            },
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        response.raise_for_status()
        payload = response.json()
        postings = payload.get("jobPostings") or []
        if not postings:
            break

        added = 0
        for posting in postings:
            normalized = _normalize_workday_job(posting, tenant, site)
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


def _normalize_workday_job(posting: dict, tenant: str, site: str) -> dict | None:
    title = str(posting.get("title") or "").strip()
    external_path = str(posting.get("externalPath") or "").strip().lstrip("/")
    if not title or not external_path:
        return None

    location = str(posting.get("locationsText") or posting.get("location") or "").strip()
    job_url = f"https://{tenant}.wd1.myworkdayjobs.com/en-US/{site}/job/{external_path}"
    return {
        "title": title,
        "location": location,
        "url": job_url,
    }


def _add_candidate(candidate: str, base_url: str, candidates: list[str], seen: set[str]) -> None:
    normalized_candidate = candidate
    if normalized_candidate.startswith("//"):
        normalized_candidate = f"https:{normalized_candidate}"
    elif not normalized_candidate.startswith("http"):
        normalized_candidate = urljoin(get_origin(base_url), normalized_candidate)

    normalized_candidate = normalized_candidate.rstrip("/")
    jobs_candidate = (
        normalized_candidate
        if normalized_candidate.endswith("/jobs")
        else f"{normalized_candidate}/jobs"
    )
    for item in (normalized_candidate, jobs_candidate):
        if item not in seen:
            seen.add(item)
            candidates.append(item)


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
