import re
from urllib.parse import urlparse

import httpx

from app.core.site_utils import normalize_site_url


GREENHOUSE_BOARD_PATTERN = re.compile(
    r"https?://(?:boards|job-boards)\.greenhouse\.io/([a-zA-Z0-9_-]+)",
    re.IGNORECASE,
)
GREENHOUSE_QUERY_PATTERN = re.compile(r"[?&]for=([a-zA-Z0-9_-]+)", re.IGNORECASE)


async def detect_greenhouse(
    url: str,
    client: httpx.AsyncClient | None = None,
    html: str | None = None,
    discovered_urls: list[str] | None = None,
) -> dict:
    resolved = await resolve_greenhouse_slug(
        url,
        client=client,
        html=html,
        discovered_urls=discovered_urls,
    )
    slug = resolved.get("slug", "")
    if not slug:
        return {
            "matched": False,
            "jobs_found": 0,
            "api_usable": False,
            "slug": "",
            "source": "not_found",
            "board_url": "",
            "api_url": "",
        }

    jobs = await fetch_greenhouse_jobs(slug, client=client)
    board_url = resolved.get("board_url") or f"https://boards.greenhouse.io/{slug}"
    api_url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
    return {
        "matched": bool(jobs),
        "jobs_found": len(jobs),
        "api_usable": bool(jobs),
        "slug": slug,
        "source": resolved.get("source", "unknown"),
        "board_url": board_url,
        "api_url": api_url,
    }


async def resolve_greenhouse_slug(
    url: str,
    client: httpx.AsyncClient | None = None,
    html: str | None = None,
    discovered_urls: list[str] | None = None,
) -> dict:
    normalized_url = normalize_site_url(url)
    parsed = urlparse(normalized_url)

    if parsed.netloc.endswith("greenhouse.io"):
        slug = parsed.path.strip("/").split("/", 1)[0]
        if slug:
            return {
                "slug": slug,
                "source": "direct_greenhouse_url",
                "board_url": normalized_url,
            }

    for discovered_url in discovered_urls or []:
        match = GREENHOUSE_BOARD_PATTERN.search(discovered_url)
        if match:
            return {
                "slug": match.group(1),
                "source": "browser_xhr_board_url",
                "board_url": match.group(0),
            }
        query_match = GREENHOUSE_QUERY_PATTERN.search(discovered_url)
        if query_match:
            return {
                "slug": query_match.group(1),
                "source": "browser_xhr_query_param",
                "board_url": discovered_url,
            }

    if html:
        match = GREENHOUSE_BOARD_PATTERN.search(html)
        if match:
            return {
                "slug": match.group(1),
                "source": "company_html_board_url",
                "board_url": match.group(0),
            }
        query_match = GREENHOUSE_QUERY_PATTERN.search(html)
        if query_match:
            return {
                "slug": query_match.group(1),
                "source": "company_html_query_param",
                "board_url": "",
            }

    if client is None:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as local_client:
            return await resolve_greenhouse_slug(
                url,
                client=local_client,
                html=html,
                discovered_urls=discovered_urls,
            )

    try:
        response = await client.get(normalized_url)
        response.raise_for_status()
    except httpx.HTTPError:
        return {
            "slug": "",
            "source": "fetch_failed",
            "board_url": "",
        }

    final_url = str(response.url)
    final_parsed = urlparse(final_url)
    if final_parsed.netloc.endswith("greenhouse.io"):
        slug = final_parsed.path.strip("/").split("/", 1)[0]
        if slug:
            return {
                "slug": slug,
                "source": "redirect_final_url",
                "board_url": final_url,
            }

    match = GREENHOUSE_BOARD_PATTERN.search(response.text)
    if match:
        return {
            "slug": match.group(1),
            "source": "fetched_html_board_url",
            "board_url": match.group(0),
        }
    query_match = GREENHOUSE_QUERY_PATTERN.search(response.text)
    if query_match:
        return {
            "slug": query_match.group(1),
            "source": "fetched_html_query_param",
            "board_url": final_url,
        }
    return {
        "slug": "",
        "source": "not_found",
        "board_url": "",
    }


async def fetch_greenhouse_jobs(
    slug: str, client: httpx.AsyncClient | None = None
) -> list[dict]:
    api_url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"

    if client is None:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as local_client:
            return await fetch_greenhouse_jobs(slug, client=local_client)

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
        jobs.append(
            {
                "title": title,
                "location": location.get("name", "") if isinstance(location, dict) else str(location or ""),
                "url": job_url,
            }
        )

    return jobs
