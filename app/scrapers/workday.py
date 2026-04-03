import httpx

from app.core.logger import logger
from app.detectors.workday import fetch_workday_jobs


async def scrape_workday(url: str, client: httpx.AsyncClient | None = None) -> list[dict]:
    """Scrape jobs from a Workday API when it can be resolved from the site."""
    try:
        _, jobs = await fetch_workday_jobs(url, client=client)
    except Exception as e:
        logger.error(f"Workday scrape error: {e}")
        return []

    logger.info(f"Workday scraper found {len(jobs)} jobs")
    return jobs
