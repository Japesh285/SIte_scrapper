import httpx

from app.core.logger import logger
from app.detectors.greenhouse import fetch_greenhouse_jobs, resolve_greenhouse_slug


async def scrape_greenhouse(url: str, client: httpx.AsyncClient | None = None) -> list[dict]:
    """Scrape jobs via Greenhouse API."""
    try:
        slug = await resolve_greenhouse_slug(url, client=client)
        if not slug:
            return []
        jobs = await fetch_greenhouse_jobs(slug, client=client)
    except Exception as e:
        logger.error(f"Greenhouse scrape error: {e}")
        return []

    logger.info(f"Greenhouse scraper found {len(jobs)} jobs")
    return jobs
