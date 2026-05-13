import httpx
from app.core.logger import logger
from app.core.site_utils import normalize_site_url

PHENOM_SIGNALS = ("phenompeople", "phenom-platform", "talent.phenompeople.com")


async def detect_phenom(
    url: str,
    client: httpx.AsyncClient | None = None,
    discovered_urls: list[str] | None = None,
) -> dict:
    normalized = normalize_site_url(url)
    if not normalized:
        return _not_matched()

    html = await _fetch_html(normalized, client)
    if not _has_phenom_signals(html, discovered_urls or []):
        return _not_matched()

    logger.info("[Phenom] signals found for %s — launching browser interception", normalized)
    return await _intercept_phenom_api(normalized)


def _has_phenom_signals(html: str, discovered_urls: list[str]) -> bool:
    lowered = html.lower()
    if any(s in lowered for s in PHENOM_SIGNALS):
        return True
    return any(s in u.lower() for s in PHENOM_SIGNALS for u in discovered_urls)


async def _fetch_html(url: str, client: httpx.AsyncClient | None) -> str:
    try:
        if client:
            resp = await client.get(url)
            return resp.text if resp.status_code == 200 else ""
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10, read=30, write=10, pool=10),
            follow_redirects=True,
        ) as c:
            resp = await c.get(url)
            return resp.text if resp.status_code == 200 else ""
    except Exception:
        return ""


async def _intercept_phenom_api(url: str) -> dict:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.warning("[Phenom] Playwright not installed")
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
                content_type = response.headers.get("content-type", "")
                if "json" not in content_type:
                    return
                try:
                    body = await response.json()
                    items = _find_job_items(body)
                    if len(items) >= 5:
                        captured = {"url": response.url, "count": len(items)}
                        logger.info("[Phenom] captured API: %s (%d jobs)", response.url, len(items))
                except Exception:
                    pass

            page.on("response", on_response)

            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=20000)
                await page.wait_for_timeout(8000)
            except Exception as exc:
                logger.debug("[Phenom] navigation error (non-fatal): %s", exc)

            await browser.close()

            if captured:
                return {
                    "matched": True,
                    "api_url": captured["url"],
                    "jobs_found": captured["count"],
                    "api_usable": True,
                    "confidence": 0.85,
                }
            logger.info("[Phenom] no job-list API intercepted for %s", url)
            return _not_matched()

    except Exception as exc:
        logger.warning("[Phenom] browser interception failed: %s", exc)
        return _not_matched()


def _find_job_items(body) -> list:
    """Find the first list of 5+ dicts that all have a 'title' field."""
    if isinstance(body, list):
        items = [x for x in body if isinstance(x, dict) and "title" in x]
        if len(items) >= 5:
            return items
    if isinstance(body, dict):
        for value in body.values():
            if isinstance(value, list):
                items = [x for x in value if isinstance(x, dict) and "title" in x]
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
