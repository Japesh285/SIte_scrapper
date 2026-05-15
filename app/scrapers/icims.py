"""iCIMS scraper."""

import httpx

from app.core.logger import logger
from app.detectors.icims import detect_icims, fetch_icims_jobs


async def scrape_icims(
    url: str,
    client: httpx.AsyncClient | None = None,
    api_url: str = "",
) -> list[dict]:
    close_client = client is None
    if close_client:
        client = httpx.AsyncClient(timeout=20, follow_redirects=True)

    try:
        if not api_url:
            try:
                resp = await client.get(url)
                html = resp.text if resp.status_code == 200 else ""
            except Exception:
                html = ""
            result = await detect_icims(url, client, html)
            if not result.get("api_usable"):
                logger.warning("[iCIMS] No usable API for %s", url)
                return []
            api_url = result["api_url"]

        jobs = await fetch_icims_jobs(client, api_url, url)
        logger.info("[iCIMS] Scraped %d jobs from %s", len(jobs), url)
        return jobs
    finally:
        if close_client:
            await client.aclose()
