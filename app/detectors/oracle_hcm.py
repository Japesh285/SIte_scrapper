import re

import httpx

from app.core.logger import logger
from app.core.site_utils import normalize_site_url

_ORACLE_HOST_RE = re.compile(
    r"https?://([a-zA-Z0-9_-]+\.fa\.[a-z0-9]+\.oraclecloud\.com)",
    re.IGNORECASE,
)
_HCM_API_PATH = "/hcmRestApi/resources/latest/recruitingCEJobRequisitions"


async def detect_oracle_hcm(
    url: str,
    client: httpx.AsyncClient | None = None,
    html: str | None = None,
    discovered_urls: list[str] | None = None,
) -> dict:
    oracle_host = _extract_oracle_host(url, html or "", discovered_urls or [])
    if not oracle_host:
        return _not_matched()

    api_url = f"https://{oracle_host}{_HCM_API_PATH}"
    if client is None:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10, read=30, write=10, pool=10),
            follow_redirects=True,
        ) as c:
            return await _probe_api(c, api_url, oracle_host)
    return await _probe_api(client, api_url, oracle_host)


def _extract_oracle_host(url: str, html: str, discovered_urls: list[str]) -> str:
    # 1. Direct Oracle Cloud URL
    m = _ORACLE_HOST_RE.search(url)
    if m:
        return m.group(1)

    # 2. Oracle Cloud URL embedded in HTML
    m = _ORACLE_HOST_RE.search(html)
    if m:
        return m.group(1)

    # 3. Oracle Cloud URL in browser-discovered URLs
    for du in discovered_urls:
        m = _ORACLE_HOST_RE.search(du)
        if m:
            return m.group(1)

    return ""


async def _probe_api(client: httpx.AsyncClient, api_url: str, oracle_host: str) -> dict:
    try:
        resp = await client.get(api_url, params={"limit": 25, "onlyData": "true"})
        if resp.status_code == 200 and "json" in resp.headers.get("content-type", ""):
            data = resp.json()
            items = data.get("items", [])
            total = data.get("totalResults", len(items))
            if len(items) > 0:
                logger.info("[Oracle_HCM] host=%s total=%d", oracle_host, total)
                return {
                    "matched": True,
                    "api_url": api_url,
                    "jobs_found": total,
                    "api_usable": True,
                    "oracle_host": oracle_host,
                    "confidence": 0.92,
                }
    except Exception as exc:
        logger.debug("[Oracle_HCM] API probe failed: %s", exc)

    return _not_matched()


def _not_matched() -> dict:
    return {
        "matched": False,
        "api_url": "",
        "jobs_found": 0,
        "api_usable": False,
        "oracle_host": "",
        "confidence": 0.0,
    }
