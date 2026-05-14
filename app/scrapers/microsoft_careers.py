from urllib.parse import urlparse, urlencode, parse_qs, urlunparse

import httpx

from app.core.logger import logger
from app.core.site_utils import normalize_site_url

_PAGE_SIZE = 20
_MAX_JOBS = 10_000
_TOTAL_KEYS = ("totalCount", "total", "count", "totalResults", "totalJobs")
_LIST_KEYS = ("operationResult", "jobs", "results", "data", "items", "postings", "value")


async def scrape_microsoft(
    url: str,
    client: httpx.AsyncClient | None = None,
    api_url: str = "",
) -> list[dict]:
    if not api_url:
        from app.detectors.microsoft_careers import detect_microsoft
        result = await detect_microsoft(url, client=client)
        api_url = result.get("api_url", "")
    if not api_url:
        return []

    base_url = normalize_site_url(url) or url
    if client is None:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10, read=90, write=10, pool=10),
            follow_redirects=True,
        ) as c:
            return await _paginate(c, api_url, base_url)
    return await _paginate(client, api_url, base_url)


async def _paginate(client: httpx.AsyncClient, api_url: str, base_url: str) -> list[dict]:
    base = _strip_pagination_params(api_url)
    all_jobs: list[dict] = []
    seen: set[str] = set()

    def add_batch(jobs: list[dict]) -> int:
        added = 0
        for j in jobs:
            key = j.get("url") or f"{j.get('title', '')}|{j.get('location', '')}"
            if key not in seen:
                seen.add(key)
                all_jobs.append(j)
                added += 1
        return added

    page = 1
    while (page - 1) * _PAGE_SIZE < _MAX_JOBS:
        try:
            resp = await client.get(base, params={"pg": page, "pgSz": _PAGE_SIZE})
            if resp.status_code != 200 or "json" not in resp.headers.get("content-type", ""):
                break
            data = resp.json()
        except Exception as exc:
            logger.warning("[Microsoft] page=%d failed: %s", page, exc)
            break

        items = _get_items(data)
        jobs = _extract_jobs(items)
        if add_batch(jobs) == 0:
            break

        total = _get_total(data)
        if total and len(all_jobs) >= total:
            break
        page += 1

    logger.info("[Microsoft] total extracted: %d", len(all_jobs))
    return all_jobs


def _extract_jobs(items: list) -> list[dict]:
    jobs = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = item.get("title") or item.get("Title") or item.get("name") or ""
        location = (
            item.get("location") or item.get("Location") or
            item.get("primaryLocation") or item.get("PrimaryLocation") or ""
        )
        if isinstance(location, dict):
            location = location.get("name") or location.get("displayName") or ""
        job_url = item.get("url") or item.get("applyUrl") or item.get("externalUrl") or ""
        if isinstance(title, str) and title.strip():
            jobs.append({
                "title": title.strip(),
                "location": str(location).strip(),
                "url": str(job_url).strip(),
            })
    return jobs


def _strip_pagination_params(api_url: str) -> str:
    parsed = urlparse(api_url)
    qs = {
        k: v for k, v in parse_qs(parsed.query, keep_blank_values=True).items()
        if k.lower() not in ("pg", "page", "offset", "start", "from", "skip", "pgsz", "pagesize")
    }
    return urlunparse(parsed._replace(query=urlencode(qs, doseq=True)))


def _get_items(data) -> list:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in _LIST_KEYS:
            val = data.get(key)
            if isinstance(val, list):
                return val
    return []


def _get_total(data) -> int:
    if isinstance(data, dict):
        for key in _TOTAL_KEYS:
            val = data.get(key)
            if isinstance(val, int) and val > 0:
                return val
    return 0
