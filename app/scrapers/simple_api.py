import httpx

from app.core.logger import logger
from app.detectors.simple_api import fetch_simple_api_jobs, paginate_simple_api_jobs
from app.core.site_utils import normalize_site_url


async def scrape_simple_api(
    url: str,
    client: httpx.AsyncClient | None = None,
    api_url: str = "",
) -> list[dict]:
    """Scrape jobs from a known or auto-detected JSON job endpoint with pagination."""
    close_client = client is None
    if close_client:
        client = httpx.AsyncClient(timeout=20, follow_redirects=True)

    try:
        if not api_url:
            jobs, api_url, _ = await fetch_simple_api_jobs(url, client=client)
            if not api_url:
                return []
        else:
            jobs = []

        base_url = normalize_site_url(url)
        all_jobs = await paginate_simple_api_jobs(client, api_url, base_url)

        # Use paginated results if richer, else fall back to initial probe results
        result = all_jobs if len(all_jobs) >= len(jobs) else jobs
        logger.info("[SimpleAPI] %s → %d jobs (paginated from %s)", url, len(result), api_url)
        return result
    except Exception as e:
        logger.error("[SimpleAPI] scrape error: %s", e)
        return []
    finally:
        if close_client:
            await client.aclose()
