import httpx

from app.core.logger import logger
from app.core.site_utils import normalize_site_url

_PAGE_SIZE = 100
_MAX_JOBS = 10_000


async def scrape_smartrecruiters(
    url: str,
    client: httpx.AsyncClient | None = None,
    api_url: str = "",
) -> list[dict]:
    if not api_url:
        from app.detectors.smartrecruiters import detect_smartrecruiters
        result = await detect_smartrecruiters(url, client=client)
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

    offset = 0
    while offset < _MAX_JOBS:
        try:
            resp = await client.get(api_url, params={"limit": _PAGE_SIZE, "offset": offset})
            if resp.status_code != 200 or "json" not in resp.headers.get("content-type", ""):
                break
            data = resp.json()
        except Exception as exc:
            logger.warning("[SmartRecruiters] offset=%d failed: %s", offset, exc)
            break

        raw_items = data.get("content", data if isinstance(data, list) else [])
        jobs = _extract_jobs(raw_items, base_url)
        if add_batch(jobs) == 0:
            break

        total = data.get("totalFound", 0) if isinstance(data, dict) else 0
        offset += _PAGE_SIZE
        if total and offset >= total:
            break

    logger.info("[SmartRecruiters] total extracted: %d", len(all_jobs))
    return all_jobs


def _extract_jobs(items: list, base_url: str) -> list[dict]:
    jobs = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = item.get("name") or item.get("title", "")
        loc = item.get("location", {})
        location = (
            loc.get("city") or loc.get("country") or loc.get("region") or ""
            if isinstance(loc, dict) else str(loc)
        )
        apply_url = item.get("applyUrl") or item.get("apply_url") or item.get("url") or ""
        if isinstance(title, str) and title.strip():
            jobs.append({
                "title": title.strip(),
                "location": location.strip(),
                "url": apply_url.strip(),
            })
    return jobs
