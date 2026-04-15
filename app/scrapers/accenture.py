from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from app.core.logger import logger

try:
    from playwright.async_api import Page, async_playwright
except ImportError:
    Page = None  # type: ignore[assignment]
    async_playwright = None  # type: ignore[assignment]


ACCENTURE_SITE_TYPE = "ACCENTURE"
_ACCENTURE_JOB_PATH = "/careers/jobdetails"
_COOKIE_BUTTON_KEYWORDS = ("accept all", "accept cookies", "allow all")
_DEFAULT_MAX_PAGES = 10


def is_accenture_job_url(url: str) -> bool:
    lowered = (url or "").lower()
    return "accenture.com" in lowered and _ACCENTURE_JOB_PATH in lowered


async def scrape_accenture_jobs(url: str, max_pages: int = _DEFAULT_MAX_PAGES) -> list[dict]:
    """Collect Accenture job detail URLs using button-driven pagination."""
    if async_playwright is None:
        logger.warning("[Accenture] Playwright not available")
        return []

    jobs: list[dict] = []
    seen_urls: set[str] = set()

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
            viewport={"width": 1600, "height": 1200},
            ignore_https_errors=True,
        )
        page = await context.new_page()

        try:
            await _load_page(page, url)

            for page_num in range(1, max_pages + 1):
                await _wait_for_listing_links(page)
                round_jobs = await _extract_listing_jobs(page)
                new_count = 0
                for job in round_jobs:
                    job_url = job.get("url", "")
                    if not job_url or job_url in seen_urls:
                        continue
                    seen_urls.add(job_url)
                    jobs.append(job)
                    new_count += 1

                logger.info(
                    "[Accenture] Page %d collected %d new jobs (total=%d)",
                    page_num,
                    new_count,
                    len(jobs),
                )

                if page_num >= max_pages:
                    break

                advanced = await _click_next_page(page)
                if not advanced:
                    logger.info("[Accenture] No next page found after page %d", page_num)
                    break

        except Exception as exc:
            logger.error("[Accenture] Listing scrape failed for %s: %s", url, exc)
        finally:
            await browser.close()

    return jobs


async def fetch_accenture_job_html(job_url: str) -> str:
    """Capture full Accenture job detail HTML using Playwright."""
    if async_playwright is None:
        logger.warning("[Accenture] Playwright not available for detail capture")
        return ""

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
            viewport={"width": 1600, "height": 1200},
            ignore_https_errors=True,
        )
        page = await context.new_page()

        try:
            html = await capture_accenture_page_html(page, job_url)
            return html
        except Exception as exc:
            logger.error("[Accenture] Detail capture failed for %s: %s", job_url, exc)
            return ""
        finally:
            await browser.close()


async def capture_accenture_page_html(page: Page, url: str) -> str:
    """Load an Accenture page, wait for it to settle, and return HTML."""
    await _load_page(page, url)

    for _ in range(4):
        await page.mouse.wheel(0, 3000)
        await page.wait_for_timeout(750)

    html = await page.content()
    logger.info("[Accenture] Captured HTML for %s (length=%d)", url, len(html))
    return html


async def _load_page(page: Page, url: str) -> None:
    await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    try:
        await page.wait_for_load_state("networkidle", timeout=20000)
    except Exception:
        logger.info("[Accenture] networkidle timeout for %s", url)
    await page.wait_for_timeout(2500)
    await _dismiss_cookie_banner(page)


async def _dismiss_cookie_banner(page: Page) -> None:
    for keyword in _COOKIE_BUTTON_KEYWORDS:
        try:
            button = page.locator(
                f"button:has-text('{keyword}'), [role='button']:has-text('{keyword}')"
            ).first
            if await button.count() and await button.is_visible():
                await button.click(timeout=2000)
                await page.wait_for_timeout(1000)
                return
        except Exception:
            continue


async def _wait_for_listing_links(page: Page) -> None:
    try:
        await page.wait_for_selector(
            f"a[href*='{_ACCENTURE_JOB_PATH}']",
            state="attached",
            timeout=30000,
        )
    except Exception:
        logger.info("[Accenture] jobdetails links did not appear before timeout on %s", page.url)


async def _click_next_page(page: Page) -> bool:
    try:
        next_btn = page.locator("[aria-label='Next']").first
        if not await next_btn.count():
            return False
        if not await next_btn.is_visible() or not await next_btn.is_enabled():
            return False
        await next_btn.click(timeout=3000)
        await _wait_for_page_refresh(page)
        logger.info("[Accenture] Advanced pagination via [aria-label='Next'] → %s", page.url)
        return True
    except Exception:
        return False


async def _wait_for_page_refresh(page: Page) -> None:
    try:
        await page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        logger.info("[Accenture] networkidle timeout after Next click on %s", page.url)
    await page.wait_for_timeout(7000)


async def _extract_listing_jobs(page: Page) -> list[dict]:
    rows = await page.locator(f"a[href*='{_ACCENTURE_JOB_PATH}']").evaluate_all(
        """(elements) => elements.map((el) => ({
            href: el.href || el.getAttribute('href') || '',
            text: (el.textContent || '').trim()
        }))"""
    )

    jobs: list[dict] = []
    seen_urls: set[str] = set()
    for row in rows:
        href = str(row.get("href") or "").strip()
        if not is_accenture_job_url(href) or href in seen_urls:
            continue
        seen_urls.add(href)

        parsed = urlparse(href)
        query = parse_qs(parsed.query)
        title = str(row.get("text") or "").strip()
        if not title:
            title = str((query.get("title") or [""])[0]).replace("+", " ").strip()
        job_id = str((query.get("id") or [""])[0]).strip()

        jobs.append(
            {
                "title": title or job_id or "Accenture Job",
                "location": "",
                "url": href,
                "job_id": job_id,
            }
        )

    return jobs
