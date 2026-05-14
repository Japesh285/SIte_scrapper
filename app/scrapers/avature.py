from urllib.parse import urlparse, urlencode, parse_qs, urlunparse

import httpx

from app.core.logger import logger
from app.core.site_utils import normalize_site_url
from app.detectors.simple_api import _extract_jobs_from_items

_PAGE_SIZE = 50
_MAX_JOBS = 10_000
_TOTAL_KEYS = ("totalCount", "total", "count", "totalResults", "totalItems")
_LIST_KEYS = ("jobs", "positions", "results", "data", "items", "content", "requisitions")


async def scrape_avature(
    url: str,
    client: httpx.AsyncClient | None = None,
    api_url: str = "",
) -> list[dict]:
    if not api_url:
        from app.detectors.avature import detect_avature
        result = await detect_avature(url, client=client)
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

    try:
        resp = await client.get(base, params={"start": 0, "count": _PAGE_SIZE})
        if resp.status_code != 200 or "json" not in resp.headers.get("content-type", ""):
            resp = await client.get(base)
        if resp.status_code != 200:
            return []
        first_data = resp.json()
    except Exception as exc:
        logger.warning("[Avature] first page failed: %s", exc)
        return []

    first_items = _get_items(first_data)
    first_jobs = _extract_jobs_from_items(first_items, base_url)
    add_batch(first_jobs)

    total = _get_total(first_data)
    per_page = max(len(first_jobs), 1)
    if total <= per_page:
        return all_jobs

    pages_needed = min((total + per_page - 1) // per_page, _MAX_JOBS // _PAGE_SIZE)
    logger.info("[Avature] paginating: total=%d pages=%d", total, pages_needed)

    for page_num in range(1, pages_needed):
        offset = page_num * _PAGE_SIZE
        try:
            resp = await client.get(base, params={"start": offset, "count": _PAGE_SIZE})
            if resp.status_code != 200 or "json" not in resp.headers.get("content-type", ""):
                break
            data = resp.json()
        except Exception as exc:
            logger.warning("[Avature] offset=%d failed: %s", offset, exc)
            break

        items = _get_items(data)
        jobs = _extract_jobs_from_items(items, base_url)
        if add_batch(jobs) == 0:
            break

    return all_jobs


def _strip_pagination_params(api_url: str) -> str:
    parsed = urlparse(api_url)
    qs = {
        k: v for k, v in parse_qs(parsed.query, keep_blank_values=True).items()
        if k.lower() not in ("start", "offset", "limit", "page", "count", "from", "skip")
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
