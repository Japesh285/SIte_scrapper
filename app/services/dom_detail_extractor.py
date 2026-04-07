"""Max-extraction DOM detail scraper — forces full content capture.

For EVERY job detail page:
1. Fully render the page
2. Trigger all lazy-loaded content
3. Expand hidden sections
4. Extract FULL HTML
5. Send FULL content to AI (minimal cleaning only)

This is the ONLY path for DOM/INTERACTIVE_DOM detail extraction.
"""

from __future__ import annotations

from app.core.logger import logger

try:
    from playwright.async_api import async_playwright
except ImportError:
    async_playwright = None  # type: ignore


async def extract_job_detail(page, url: str) -> str:
    """Max-extract a single job detail page.

    Performs:
    - Full page load with networkidle
    - Multiple scroll rounds to trigger lazy loading
    - Clicks all expandable sections (more/expand/show/read)
    - Waits for DOM growth before capturing final HTML

    Parameters
    ----------
    page : Playwright Page
        An existing Playwright page object.
    url : str
        Job detail page URL.

    Returns
    -------
    str
        Full HTML content after all interactions.
    """
    logger.info("[DETAIL] url=%s", url)

    await page.goto(url, timeout=60000)

    # Wait for base load
    try:
        await page.wait_for_load_state("networkidle")
    except Exception:
        logger.warning("[DETAIL] networkidle timeout for %s", url)
    await page.wait_for_timeout(2000)

    # Scroll to trigger lazy loading
    for i in range(5):
        await page.mouse.wheel(0, 3000)
        await page.wait_for_timeout(1000)

    # Expand hidden sections
    try:
        buttons = await page.query_selector_all("button")

        for btn in buttons:
            try:
                text = (await btn.inner_text() or "").lower()

                if any(k in text for k in ["more", "expand", "show", "read"]):
                    await btn.click()
                    await page.wait_for_timeout(500)
            except Exception:
                continue
    except Exception as exc:
        logger.warning("[DETAIL] Button expansion failed: %s", exc)

    # Wait for DOM growth
    prev_len = 0

    for _ in range(5):
        html = await page.content()
        curr_len = len(html)

        if curr_len > prev_len * 1.2:
            prev_len = curr_len
            await page.wait_for_timeout(1000)
        else:
            break

    html = await page.content()
    logger.info("[DETAIL] url=%s html_length=%d", url, len(html))

    return html


async def extract_job_detail_standalone(url: str) -> str | None:
    """Convenience wrapper — creates its own browser context.

    Use this when you don't have an existing Playwright page object.

    Parameters
    ----------
    url : str
        Job detail page URL.

    Returns
    -------
    str or None
        Full HTML content, or None if extraction failed.
    """
    if async_playwright is None:
        logger.error("[DETAIL] Playwright not available")
        return None

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                ],
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
            try:
                html = await extract_job_detail(page, url)
                return html
            finally:
                await page.close()
                await browser.close()
    except Exception as exc:
        logger.error("[DETAIL] Extraction failed for %s: %s", url, exc)
        return None
