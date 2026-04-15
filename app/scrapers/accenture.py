from __future__ import annotations

import asyncio
import random
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
_ACCENTURE_BLOCK_HTML_THRESHOLD = 50000
_ACCENTURE_BLOCK_TITLE_TEXT = "Thank you for your interest in Accenture"
_ACCENTURE_MAX_RETRIES = 3
_ACCENTURE_REQUEST_DELAY_RANGE = (3, 8)
_ACCENTURE_LONG_PAUSE_REQUEST_RANGE = (20, 30)
_ACCENTURE_LONG_PAUSE_SECONDS_RANGE = (60, 180)
_ACCENTURE_BLOCK_COOLDOWN_SECONDS_RANGE = (600, 1200)
_ACCENTURE_SAFETY_COOLDOWN_EVERY = 50
_ACCENTURE_USER_AGENTS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
)
_ACCENTURE_ACCEPT_LANGUAGES = (
    "en-US,en;q=0.9",
    "en-GB,en;q=0.9",
    "en-IN,en;q=0.9",
)


def is_accenture_job_url(url: str) -> bool:
    lowered = (url or "").lower()
    return "accenture.com" in lowered and _ACCENTURE_JOB_PATH in lowered


async def maybe_accenture_cooldown(counter: int, phase: str) -> None:
    if counter <= 0 or counter % _ACCENTURE_SAFETY_COOLDOWN_EVERY != 0:
        return
    cooldown_seconds = random.randint(*_ACCENTURE_BLOCK_COOLDOWN_SECONDS_RANGE)
    logger.info(
        "[Accenture] Safety cooldown after %d %s — sleeping for %d seconds",
        counter,
        phase,
        cooldown_seconds,
    )
    await asyncio.sleep(cooldown_seconds)


def detect_accenture_block(html: str, title: str) -> bool:
    html_length = len(html or "")
    title_text = (title or "").strip().lower()
    return (
        html_length < _ACCENTURE_BLOCK_HTML_THRESHOLD
        or _ACCENTURE_BLOCK_TITLE_TEXT.lower() in title_text
    )


class AccentureThrottle:
    def __init__(self) -> None:
        self.request_count = 0
        self.next_long_pause_at = random.randint(*_ACCENTURE_LONG_PAUSE_REQUEST_RANGE)

    async def before_request(self, phase: str) -> None:
        self.request_count += 1

        delay_seconds = random.randint(*_ACCENTURE_REQUEST_DELAY_RANGE)
        logger.info(
            "[Accenture] Delay before %s request #%d — sleeping for %d seconds",
            phase,
            self.request_count,
            delay_seconds,
        )
        await asyncio.sleep(delay_seconds)

        if self.request_count % _ACCENTURE_SAFETY_COOLDOWN_EVERY == 0:
            await maybe_accenture_cooldown(self.request_count, phase)
        elif self.request_count >= self.next_long_pause_at:
            pause_seconds = random.randint(*_ACCENTURE_LONG_PAUSE_SECONDS_RANGE)
            logger.info(
                "[Accenture] Longer pause after %d %s requests — sleeping for %d seconds",
                self.request_count,
                phase,
                pause_seconds,
            )
            await asyncio.sleep(pause_seconds)
            self.next_long_pause_at += random.randint(*_ACCENTURE_LONG_PAUSE_REQUEST_RANGE)


def _random_accept_language() -> str:
    return random.choice(_ACCENTURE_ACCEPT_LANGUAGES)


def _random_user_agent() -> str:
    return random.choice(_ACCENTURE_USER_AGENTS)


async def create_accenture_context(browser):
    return await browser.new_context(
        user_agent=_random_user_agent(),
        extra_http_headers={
            "Accept-Language": _random_accept_language(),
        },
        viewport={"width": 1600, "height": 1200},
        ignore_https_errors=True,
    )


class AccentureRequestManager:
    def __init__(self, browser, throttle: AccentureThrottle | None = None) -> None:
        self.browser = browser
        self.throttle = throttle or AccentureThrottle()
        self.context = None

    async def ensure_context(self):
        if self.context is None:
            self.context = await create_accenture_context(self.browser)
        return self.context

    async def rotate_context(self) -> None:
        if self.context is not None:
            await self.context.close()
        self.context = await create_accenture_context(self.browser)

    async def close(self) -> None:
        if self.context is not None:
            await self.context.close()
            self.context = None

    async def fetch_page(
        self,
        url: str,
        *,
        phase: str,
        scroll_rounds: int = 4,
        max_retries: int = _ACCENTURE_MAX_RETRIES,
    ) -> str:
        for attempt in range(1, max_retries + 1):
            await self.throttle.before_request(phase)
            context = await self.ensure_context()
            page = await context.new_page()
            try:
                title, html = await _goto_and_capture(page, url, scroll_rounds=scroll_rounds)
                if detect_accenture_block(html, title):
                    await _handle_block(url, title, len(html), attempt, max_retries)
                    await page.close()
                    if attempt >= max_retries:
                        return ""
                    await self.rotate_context()
                    continue
                return html
            except Exception as exc:
                logger.warning(
                    "[Accenture] Request failed for %s on attempt %d/%d: %s",
                    url,
                    attempt,
                    max_retries,
                    exc,
                )
                await page.close()
                if attempt >= max_retries:
                    return ""
                backoff = 2 ** attempt
                logger.info(
                    "[Accenture] Retry backoff for %s — sleeping for %d seconds",
                    url,
                    backoff,
                )
                await asyncio.sleep(backoff)
                await self.rotate_context()
            finally:
                if not page.is_closed():
                    await page.close()
        return ""


async def _handle_block(url: str, title: str, html_length: int, attempt: int, max_retries: int) -> None:
    cooldown_seconds = random.randint(*_ACCENTURE_BLOCK_COOLDOWN_SECONDS_RANGE)
    logger.warning(
        "[Accenture] Block detected for %s on attempt %d/%d | title=%r | html_length=%d",
        url,
        attempt,
        max_retries,
        title,
        html_length,
    )
    logger.info(
        "[Accenture] Cooldown started for %s — sleeping for %d seconds before retry",
        url,
        cooldown_seconds,
    )
    await asyncio.sleep(cooldown_seconds)
    logger.info("[Accenture] Cooldown ended for %s", url)


async def _goto_and_capture(page: Page, url: str, *, scroll_rounds: int) -> tuple[str, str]:
    await _load_page(page, url)
    for _ in range(scroll_rounds):
        await page.mouse.wheel(0, 3000)
        await page.wait_for_timeout(750)
    title = await page.title()
    html = await page.content()
    logger.info("[Accenture] Captured HTML for %s (length=%d, title=%r)", url, len(html), title)
    return title, html


async def scrape_accenture_jobs(url: str) -> list[dict]:
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
        context = await create_accenture_context(browser)
        throttle = AccentureThrottle()
        page = await context.new_page()

        try:
            await _load_page(page, url)

            page_num = 1
            while True:
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

                await throttle.before_request("listing pages")
                advanced = await _click_next_page(page)
                if not advanced:
                    logger.info("[Accenture] No next page found after page %d", page_num)
                    break
                page_num += 1

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
        manager = AccentureRequestManager(browser)
        try:
            return await manager.fetch_page(job_url, phase="job details", scroll_rounds=4)
        finally:
            await manager.close()
            await browser.close()


async def capture_accenture_page_html(page: Page, url: str) -> str:
    """Load an Accenture page, wait for it to settle, and return HTML."""
    _, html = await _goto_and_capture(page, url, scroll_rounds=4)
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
