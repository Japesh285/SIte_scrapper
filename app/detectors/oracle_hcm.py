import re
from urllib.parse import parse_qs, urlparse

import httpx

from app.core.logger import logger
from app.core.site_utils import normalize_site_url

_ORACLE_HOST_RE = re.compile(
    r"https?://([a-zA-Z0-9_-]+\.fa\.[a-z0-9]+\.oraclecloud\.com)",
    re.IGNORECASE,
)
# Extracts site name from candidate experience URLs:
# /hcmUI/CandidateExperience/en/sites/{SiteName}/jobs
_SITE_NAME_RE = re.compile(
    r"/sites/([^/?#]+)/jobs",
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

    site_name, location_facet = _extract_site_params(url)
    api_url = f"https://{oracle_host}{_HCM_API_PATH}"

    if client is None:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10, read=30, write=10, pool=10),
            follow_redirects=True,
        ) as c:
            return await _probe_api(c, api_url, oracle_host, site_name, location_facet)
    return await _probe_api(client, api_url, oracle_host, site_name, location_facet)


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


def _extract_site_params(url: str) -> tuple[str, str]:
    """Extract site name and location facet from Oracle HCM candidate experience URL."""
    site_name = ""
    location_facet = ""
    m = _SITE_NAME_RE.search(url)
    if m:
        site_name = m.group(1)
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    loc = qs.get("selectedLocationsFacet", [])
    if loc:
        location_facet = loc[0]
    return site_name, location_facet


def _build_finder(site_name: str, location_facet: str) -> str:
    finder = "findReqs"
    if site_name:
        finder += f";siteNumber={site_name}"
    if location_facet:
        finder += f",lastSelectedFacet=LOCATIONS,selectedLocationsFacet={location_facet}"
    return finder


async def _probe_api(
    client: httpx.AsyncClient,
    api_url: str,
    oracle_host: str,
    site_name: str = "",
    location_facet: str = "",
) -> dict:
    finder = _build_finder(site_name, location_facet)
    params: dict = {"limit": 1, "onlyData": "true", "expand": "requisitionList"}
    if finder != "findReqs":
        params["finder"] = finder

    try:
        resp = await client.get(api_url, params=params)
        if resp.status_code == 200 and "json" in resp.headers.get("content-type", ""):
            data = resp.json()
            items = data.get("items", [])
            if items:
                ctx = items[0]
                total = ctx.get("TotalJobsCount", 0)
                req_list = ctx.get("requisitionList", [])
                jobs_found = total if total else len(req_list)
                if jobs_found > 0 or req_list:
                    logger.info(
                        "[Oracle_HCM] host=%s site=%s location=%s total=%d",
                        oracle_host, site_name, location_facet, jobs_found,
                    )
                    return {
                        "matched": True,
                        "api_url": api_url,
                        "jobs_found": jobs_found,
                        "api_usable": True,
                        "oracle_host": oracle_host,
                        "oracle_site": site_name,
                        "oracle_location": location_facet,
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
        "oracle_site": "",
        "oracle_location": "",
        "confidence": 0.0,
    }
