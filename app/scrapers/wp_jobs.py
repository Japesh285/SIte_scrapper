"""WordPress job_box page scraper."""

import httpx

from app.core.logger import logger
from app.detectors.wp_jobs import detect_wp_jobs, fetch_wp_jobs


async def scrape_wp_jobs(
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
                resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
                html = resp.text if resp.status_code == 200 else ""
            except Exception:
                html = ""
            result = await detect_wp_jobs(url, client, html)
            if not result.get("api_usable"):
                logger.warning("[WP_JOBS] No job listing detected for %s", url)
                return []
            api_url = result["api_url"]

        jobs = await fetch_wp_jobs(client, api_url, url)
        logger.info("[WP_JOBS] Scraped %d jobs from %s", len(jobs), url)
        return jobs
    finally:
        if close_client:
            await client.aclose()
