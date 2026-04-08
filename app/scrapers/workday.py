"""Workday scraper — API only, no DOM fallback."""

import httpx

from app.core.logger import logger
from app.detectors.workday import fetch_workday_jobs


async def scrape_workday(url: str, client: httpx.AsyncClient | None = None) -> list[dict]:
    """Scrape jobs from Workday API only. No DOM fallback."""
    try:
        api_url, jobs = await fetch_workday_jobs(url, client=client)
    except Exception as e:
        logger.error("Workday scrape error: %s", e)
        return []

    if not jobs:
        logger.warning("Workday scraper found 0 jobs for %s", url)
        return []

    logger.info("Workday scraper found %d jobs (API: %s)", len(jobs), api_url)
    return jobs
