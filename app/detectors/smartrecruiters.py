import re

import httpx

from app.core.logger import logger
from app.core.site_utils import normalize_site_url

_SR_HOST_RE = re.compile(
    r"(?:careers|jobs)\.smartrecruiters\.com/([a-zA-Z0-9_-]+)",
    re.IGNORECASE,
)
_SR_HTML_RE = re.compile(
    r"smartrecruiters\.com/([a-zA-Z0-9_-]+)",
    re.IGNORECASE,
)
_SR_API = "https://api.smartrecruiters.com/v1/companies/{slug}/postings"


async def detect_smartrecruiters(
    url: str,
    client: httpx.AsyncClient | None = None,
    html: str | None = None,
    discovered_urls: list[str] | None = None,
) -> dict:
    slug = _extract_slug(url, html or "", discovered_urls or [])
    if not slug:
        return _not_matched("no_company_slug")

    api_url = _SR_API.format(slug=slug)
    if client is None:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10, read=30, write=10, pool=10),
            follow_redirects=True,
        ) as c:
            return await _probe_api(c, api_url, slug)
    return await _probe_api(client, api_url, slug)


def _extract_slug(url: str, html: str, discovered_urls: list[str]) -> str:
    # 1. From URL path: careers.smartrecruiters.com/{slug}
    m = _SR_HOST_RE.search(url)
    if m and m.group(1).lower() not in ("", "home", "login", "search"):
        return m.group(1)

    # 2. From HTML
    m = _SR_HTML_RE.search(html)
    if m and m.group(1).lower() not in ("", "home", "login", "search"):
        return m.group(1)

    # 3. From discovered_urls (browser probe)
    for du in discovered_urls:
        m = _SR_HOST_RE.search(du)
        if m and m.group(1).lower() not in ("", "home", "login", "search"):
            return m.group(1)

    return ""


async def _probe_api(client: httpx.AsyncClient, api_url: str, slug: str) -> dict:
    try:
        resp = await client.get(api_url, params={"limit": 10, "offset": 0})
        if resp.status_code == 200 and "json" in resp.headers.get("content-type", ""):
            data = resp.json()
            count = len(data.get("content", data if isinstance(data, list) else []))
            total = data.get("totalFound", count) if isinstance(data, dict) else count
            if count > 0:
                logger.info("[SmartRecruiters] slug=%s total=%d", slug, total)
                return {
                    "matched": True,
                    "api_url": api_url,
                    "jobs_found": total,
                    "api_usable": True,
                    "slug": slug,
                    "confidence": 0.90,
                }
    except Exception as exc:
        logger.debug("[SmartRecruiters] API probe failed: %s", exc)

    return _not_matched("api_returned_no_jobs")


def _not_matched(reason: str = "") -> dict:
    return {
        "matched": False,
        "api_url": "",
        "jobs_found": 0,
        "api_usable": False,
        "slug": "",
        "confidence": 0.0,
    }
