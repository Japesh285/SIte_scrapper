import httpx

from app.core.logger import logger
from app.detectors.greenhouse import fetch_greenhouse_jobs, resolve_greenhouse_slug


async def scrape_greenhouse(url: str, client: httpx.AsyncClient | None = None) -> list[dict]:
    """Scrape jobs via Greenhouse API, preserving raw API data for detail extraction."""
    try:
        resolved = await resolve_greenhouse_slug(url, client=client)
        if not resolved.get("slug"):
            return []
        slug = resolved["slug"]

        # Fetch with raw data preservation
        jobs = await _fetch_greenhouse_with_raw_data(slug, client=client)
    except Exception as e:
        logger.error(f"Greenhouse scrape error: {e}")
        return []

    logger.info(f"Greenhouse scraper found {len(jobs)} jobs")
    return jobs


async def _fetch_greenhouse_with_raw_data(
    slug: str, client: httpx.AsyncClient | None = None
) -> list[dict]:
    """Fetch Greenhouse jobs preserving full API response for detail extraction."""
    api_url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"

    if client is None:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as local_client:
            return await _fetch_greenhouse_with_raw_data(slug, client=local_client)

    try:
        response = await client.get(api_url)
        response.raise_for_status()
        payload = response.json()
    except (ValueError, httpx.HTTPError):
        return []

    jobs: list[dict] = []
    for job in payload.get("jobs", []):
        if not isinstance(job, dict):
            continue
        title = str(job.get("title", "")).strip()
        job_url = str(job.get("absolute_url", "")).strip()
        if not title or not job_url:
            continue
        location = job.get("location", {})
        loc = location.get("name", "") if isinstance(location, dict) else str(location or "")

        job_entry = {
            "title": title,
            "location": loc,
            "url": job_url,
            "_raw_api": job,
        }
        jobs.append(job_entry)

    return jobs
