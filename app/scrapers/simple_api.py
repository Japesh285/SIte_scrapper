import httpx

from app.core.logger import logger
from app.detectors.simple_api import fetch_simple_api_jobs


async def scrape_simple_api(url: str, client: httpx.AsyncClient | None = None) -> list[dict]:
    """Scrape jobs from common JSON job endpoints."""
    try:
        jobs, _, _ = await fetch_simple_api_jobs(url, client=client)
    except Exception as e:
        logger.error(f"Simple API scrape error: {e}")
        return []

    logger.info(f"Simple API scraper found {len(jobs)} jobs")
    return jobs
