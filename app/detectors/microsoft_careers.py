import re

import httpx

from app.core.logger import logger
from app.core.site_utils import normalize_site_url

_MSFT_SIGNALS = ("careers.microsoft.com", "apply.careers.microsoft.com")
_MSFT_SEARCH_URL = "https://careers.microsoft.com/us/en/search"
_MSFT_APPLY_URL = "https://apply.careers.microsoft.com"

# Known search URL patterns from the Microsoft careers SPA
_MSFT_API_RE = re.compile(
    r"https?://[a-z0-9.-]*microsoft[a-z0-9.-]*/[a-z0-9/_.-]*(?:search|positions|jobs)[a-z0-9/_.-?=&]*",
    re.IGNORECASE,
)


async def detect_microsoft(
    url: str,
    client: httpx.AsyncClient | None = None,
    discovered_urls: list[str] | None = None,
) -> dict:
    # Only trigger on Microsoft careers URLs
    if not any(s in url.lower() for s in _MSFT_SIGNALS):
        html = await _fetch_html(normalize_site_url(url) or url, client)
        if not any(s in html.lower() for s in _MSFT_SIGNALS):
            return _not_matched()

    # Use the input URL if it's already a careers page, otherwise fall back to search
    normalized = normalize_site_url(url)
    if "apply.careers.microsoft.com" in normalized.lower():
        target = _MSFT_APPLY_URL
    else:
        target = _MSFT_SEARCH_URL
    logger.info("[Microsoft] launching browser interception for %s", target)
    return await _intercept_microsoft_api(target)


async def _fetch_html(url: str, client: httpx.AsyncClient | None) -> str:
    try:
        if client:
            resp = await client.get(url)
            return resp.text if resp.status_code < 400 else ""
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10, read=30, write=10, pool=10),
            follow_redirects=True,
        ) as c:
            resp = await c.get(url)
            return resp.text if resp.status_code < 400 else ""
    except Exception:
        return ""


async def _intercept_microsoft_api(url: str) -> dict:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.warning("[Microsoft] Playwright not installed")
        return _not_matched()

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
            )
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1920, "height": 1080},
                ignore_https_errors=True,
            )
            page = await context.new_page()

            captured: dict | None = None

            async def on_response(response):
                nonlocal captured
                if captured:
                    return
                resp_url = response.url.lower()
                # Only intercept Microsoft API responses
                if "microsoft" not in resp_url and "msft" not in resp_url:
                    return
                content_type = response.headers.get("content-type", "")
                if "json" not in content_type:
                    return
                try:
                    body = await response.json()
                    items = _find_job_items(body)
                    if len(items) >= 5:
                        captured = {"url": response.url, "count": len(items)}
                        logger.info("[Microsoft] captured API: %s (%d jobs)", response.url, len(items))
                except Exception:
                    pass

            page.on("response", on_response)

            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=25000)
                await page.wait_for_timeout(10000)
            except Exception as exc:
                logger.debug("[Microsoft] navigation error (non-fatal): %s", exc)

            await browser.close()

            if captured:
                return {
                    "matched": True,
                    "api_url": captured["url"],
                    "jobs_found": captured["count"],
                    "api_usable": True,
                    "confidence": 0.88,
                }
            logger.info("[Microsoft] no job-list API intercepted for %s", url)
            return _not_matched()

    except Exception as exc:
        logger.warning("[Microsoft] browser interception failed: %s", exc)
        return _not_matched()


def _find_job_items(body) -> list:
    if isinstance(body, list):
        items = [x for x in body if isinstance(x, dict) and ("title" in x or "Title" in x)]
        if len(items) >= 5:
            return items
    if isinstance(body, dict):
        for value in body.values():
            if isinstance(value, list):
                items = [x for x in value if isinstance(x, dict) and ("title" in x or "Title" in x)]
                if len(items) >= 5:
                    return items
    return []


def _not_matched() -> dict:
    return {
        "matched": False,
        "api_url": "",
        "jobs_found": 0,
        "api_usable": False,
        "confidence": 0.0,
    }
