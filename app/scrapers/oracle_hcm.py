import httpx

from app.core.logger import logger
from app.core.site_utils import normalize_site_url

_PAGE_SIZE = 100
_MAX_JOBS = 10_000


async def scrape_oracle_hcm(
    url: str,
    client: httpx.AsyncClient | None = None,
    api_url: str = "",
    site_name: str = "",
    location_facet: str = "",
) -> list[dict]:
    if not api_url:
        from app.detectors.oracle_hcm import detect_oracle_hcm
        result = await detect_oracle_hcm(url, client=client)
        api_url = result.get("api_url", "")
        site_name = result.get("oracle_site", "")
        location_facet = result.get("oracle_location", "")
    if not api_url:
        return []

    base_url = normalize_site_url(url) or url
    if client is None:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10, read=90, write=10, pool=10),
            follow_redirects=True,
        ) as c:
            return await _paginate(c, api_url, base_url, site_name, location_facet)
    return await _paginate(client, api_url, base_url, site_name, location_facet)


async def _paginate(
    client: httpx.AsyncClient,
    api_url: str,
    base_url: str,
    site_name: str = "",
    location_facet: str = "",
) -> list[dict]:
    from app.detectors.oracle_hcm import _build_finder
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

    finder = _build_finder(site_name, location_facet)
    use_ce_api = bool(site_name)
    offset = 0
    while offset < _MAX_JOBS:
        params: dict = {"limit": _PAGE_SIZE, "onlyData": "true", "offset": offset}
        if use_ce_api:
            params["expand"] = "requisitionList"
            params["finder"] = finder

        try:
            resp = await client.get(api_url, params=params)
            if resp.status_code != 200 or "json" not in resp.headers.get("content-type", ""):
                break
            data = resp.json()
        except Exception as exc:
            logger.warning("[Oracle_HCM] offset=%d failed: %s", offset, exc)
            break

        if use_ce_api:
            # CE API: items[0].requisitionList contains actual jobs
            items_outer = data.get("items", [])
            if not items_outer:
                break
            ctx = items_outer[0]
            req_list = ctx.get("requisitionList", [])
            if not req_list:
                break
            total = ctx.get("TotalJobsCount", 0)
            batch_size = len(req_list)
            jobs = _extract_jobs(req_list, base_url)
            if add_batch(jobs) == 0:
                break
            offset += batch_size  # advance by actual items received
            if total and offset >= total:  # TotalJobsCount is the global count
                break
        else:
            # Legacy path: direct items list
            items = data.get("items", [])
            if not items:
                break
            jobs = _extract_jobs(items, base_url)
            if add_batch(jobs) == 0:
                break
            if not data.get("hasMore", False):
                break
            offset += _PAGE_SIZE

    logger.info("[Oracle_HCM] total extracted: %d", len(all_jobs))
    return all_jobs


def _extract_jobs(items: list, base_url: str) -> list[dict]:
    jobs = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = (
            item.get("Title") or item.get("Name") or
            item.get("title") or item.get("name") or ""
        )
        location = (
            item.get("PrimaryLocation") or item.get("primaryLocation") or
            item.get("Location") or item.get("location") or ""
        )
        if isinstance(location, dict):
            location = location.get("Name") or location.get("name") or ""
        job_id = item.get("Id") or item.get("id") or item.get("RequisitionId") or ""
        job_url = (
            item.get("ExternalUrl") or item.get("externalUrl") or
            item.get("ApplyUrl") or item.get("applyUrl") or ""
        )
        if not job_url and job_id:
            job_url = f"{base_url}/recruiting/jobdetails?job={job_id}"
        if isinstance(title, str) and title.strip():
            jobs.append({
                "title": title.strip(),
                "location": str(location).strip(),
                "url": job_url.strip(),
            })
    return jobs
