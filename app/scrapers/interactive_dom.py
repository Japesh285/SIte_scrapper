"""INTERACTIVE_DOM scraper — interaction-driven DOM job extraction.

This scraper is for sites that require user interaction to reveal job listings.
It uses Playwright to:
1. Load the page
2. Wait for initial content
3. Perform interaction simulation (clicks, fills, scrolls)
4. Extract job listings from the fully-loaded DOM

Unlike DOM_BROWSER, this scraper explicitly performs interactions before
extraction, making it suitable for sites like IBM, Dell, Oracle that hide
jobs behind search buttons, load-more controls, etc.
"""

from __future__ import annotations

import html
import re

from app.core.logger import logger
from app.core.site_utils import absolutize_url
from app.detectors.browser_probe import run_browser_probe

try:
    from playwright.async_api import async_playwright
except ImportError:
    async_playwright = None  # type: ignore

# Keywords that indicate a link is NOT a job listing
_EXCLUDE_TITLE_PARTS = (
    "privacy", "legal", "cookie", "terms", "accessibility",
    "benefits", "culture", "diversity", "students", "internship",
    "saved jobs", "talent community", "job alert", "sign in",
    "log in", "register", "profile", "settings",
    "your career", "recommended jobs", "job search", "help",
)

_EXCLUDE_URL_PARTS = (
    "/privacy", "/legal", "/cookie", "/terms", "/accessibility",
    "/benefits", "/culture", "/diversity", "/student", "/intern",
    "/saved-jobs", "/profile", "/login", "/register", "/settings",
    "/about", "/contact", "/help", "/faq",
    "/dashboard", "/recommendations", "/jobs/results/jobs/results",
    "support.google.com",
)

# Minimal job signal keywords in link text
_JOB_SIGNALS = ["job", "career", "position", "opening", "role", "apply"]

_GENERIC_TITLE_TEXTS = {
    "jobs", "job", "job search", "recommended jobs", "your career",
    "help", "view jobs", "search jobs", "careers", "career",
}

_STRUCTURED_JOB_DETAIL_PATTERNS = (
    re.compile(r"/careers/[^/?#]*-(?:irc|req|job|jr)\d+(?:/|$)", re.IGNORECASE),
    re.compile(r"/careers/[^/?#]*\d{4,}(?:/|$)", re.IGNORECASE),
)


async def scrape_interactive_dom(url: str, max_rounds: int = 8) -> list[dict]:
    """Scrape jobs from a page requiring user interaction.

    Flow:
    1. Run browser probe (interactions + network capture)
    2. Use the probe's final HTML (after interactions) as the source
    3. Extract job links from the fully-loaded DOM
    4. If still insufficient, run a dedicated Playwright session
       with more aggressive interaction simulation

    Parameters
    ----------
    url : str
        The career page URL.
    max_rounds : int
        Maximum interaction/extraction rounds.

    Returns
    -------
    list[dict] — job listings with title, location, url.
    """
    # Step 1: Run browser probe — it already performed interactions
    probe = await run_browser_probe(url)
    if not probe.available:
        logger.warning("[InteractiveDOM] Browser probe unavailable for %s", url)
        return []

    # Step 2: Extract jobs from the final HTML (after interactions)
    jobs = _extract_jobs_from_html(probe.final_html, url)

    if jobs:
        logger.info(
            "[InteractiveDOM] Extracted %d jobs from probe final HTML",
            len(jobs),
        )
        return jobs

    # Step 3: If probe didn't yield jobs, run a dedicated session
    # with more aggressive interaction
    logger.info(
        "[InteractiveDOM] No jobs from probe — running dedicated extraction for %s",
        url,
    )
    jobs = await _aggressive_extract_jobs(url)

    return jobs


async def _aggressive_extract_jobs(url: str, max_rounds: int = 8) -> list[dict]:
    """Dedicated Playwright session with aggressive interaction.

    Performs multiple rounds of:
    - Clicking buttons with job-related keywords
    - Filling search inputs
    - Scrolling
    - Waiting for content to load
    Then extracts job links from the final DOM.
    """
    if async_playwright is None:
        logger.warning("[InteractiveDOM] Playwright not available")
        return []

    all_jobs: list[dict] = []
    seen_keys: set[str] = set()

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

            await page.goto(url, wait_until="networkidle", timeout=60000)
            await _wait_for_interactive_settle(page, initial=True)

            for round_idx in range(max_rounds):
                # Extract jobs at this stage
                html = await page.content()
                round_jobs = _extract_jobs_from_html(html, url)

                new_count = 0
                for job in round_jobs:
                    key = (
                        job.get("url")
                        or f"{job.get('title', '')}|{job.get('location', '')}"
                    ).lower()
                    if key not in seen_keys:
                        seen_keys.add(key)
                        all_jobs.append(job)
                        new_count += 1

                logger.info(
                    "[InteractiveDOM] Round %d: %d new jobs (total=%d)",
                    round_idx + 1, new_count, len(all_jobs),
                )

                if new_count == 0 and round_idx >= 1:
                    recovered = await _recover_interactive_page(page, url, round_idx + 1)
                    if recovered:
                        html = await page.content()
                        round_jobs = _extract_jobs_from_html(html, url)
                        for job in round_jobs:
                            key = (
                                job.get("url")
                                or f"{job.get('title', '')}|{job.get('location', '')}"
                            ).lower()
                            if key not in seen_keys:
                                seen_keys.add(key)
                                all_jobs.append(job)
                                new_count += 1
                        logger.info(
                            "[InteractiveDOM] Recovery after round %d yielded %d extra jobs (total=%d)",
                            round_idx + 1, new_count, len(all_jobs),
                        )

                if new_count == 0 and round_idx >= 2:
                    # No new jobs for a couple rounds — stop early
                    break

                # Perform another round of interactions
                await _interact_page(page)
                await _wait_for_interactive_settle(page)

            await browser.close()

    except Exception as exc:
        logger.error("[InteractiveDOM] Aggressive extraction failed: %s", exc)

    return all_jobs


async def _interact_page(page) -> None:
    """Perform a set of user-like interactions on the page.

    Actions:
    1. Click buttons with job-related keywords
    2. Fill search inputs with "software" and press Enter
    3. Scroll down
    """
    # Click keyword-matching buttons
    click_keywords = ["search", "jobs", "find", "view", "explore", "load more", "show more"]
    for keyword in click_keywords:
        try:
            buttons = await page.locator(
                f"button:has-text('{keyword}'), a:has-text('{keyword}'), "
                f"[role='button']:has-text('{keyword}')"
            ).all()
            for btn in buttons[:2]:
                if await btn.is_visible():
                    await btn.click(timeout=2000)
                    await page.wait_for_timeout(500)
        except Exception:
            pass

    # Fill search inputs
    try:
        inputs = await page.locator(
            "input[type='text'], input[type='search'], "
            "input[placeholder*='search'], input[placeholder*='job']"
        ).all()
        for inp in inputs[:1]:
            if await inp.is_visible():
                await inp.fill("software", timeout=2000)
                await inp.press("Enter")
                await page.wait_for_timeout(1500)
    except Exception:
        pass

    # Scroll
    for _ in range(3):
        try:
            await page.evaluate("window.scrollBy(0, window.innerHeight)")
            await page.wait_for_timeout(400)
        except Exception:
            break


async def _wait_for_interactive_settle(page, initial: bool = False) -> None:
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=8000)
    except Exception:
        pass
    try:
        await page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass

    await page.wait_for_timeout(3200 if initial else 2200)

    last_len = -1
    stable_rounds = 0
    for _ in range(4):
        try:
            html_len = len(await page.content())
        except Exception:
            break
        if abs(html_len - last_len) < 80:
            stable_rounds += 1
        else:
            stable_rounds = 0
        last_len = html_len
        if stable_rounds >= 1:
            break
        await page.wait_for_timeout(900)


async def _recover_interactive_page(page, base_url: str, round_no: int) -> bool:
    """Reload current page if interaction rounds appear to have pushed the UI into a false empty state."""
    try:
        stats = await page.evaluate(
            """
            () => {
              const text = (document.body?.innerText || "").toLowerCase();
              const links = Array.from(document.querySelectorAll("a[href]")).length;
              const likelyEmpty =
                text.includes("no jobs found") ||
                text.includes("no results found") ||
                text.includes("0 jobs") ||
                text.includes("0 results");
              return { links, likelyEmpty, textLength: text.length, href: window.location.href };
            }
            """
        )
    except Exception:
        return False

    if not (stats.get("likelyEmpty") or (stats.get("links", 0) < 8 and stats.get("textLength", 0) < 1200)):
        return False

    try:
        logger.info("[InteractiveDOM] Reloading after suspected false empty state on round %d", round_no)
        await page.goto(stats.get("href") or base_url, wait_until="domcontentloaded", timeout=60000)
        await _wait_for_interactive_settle(page, initial=True)
        return True
    except Exception as exc:
        logger.warning("[InteractiveDOM] Reload recovery failed on round %d: %s", round_no, exc)
        return False


def _extract_jobs_from_html(html: str, base_url: str) -> list[dict]:
    """Extract job listings from HTML using regex-based link analysis.

    This is a lightweight extraction that doesn't require BeautifulSoup.
    It finds anchor elements with job-related text and hrefs.
    """
    jobs: list[dict] = []
    seen_urls: set[str] = set()

    # Find all <a> tags with href
    anchor_pattern = r'<a\s+[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>'
    matches = re.findall(anchor_pattern, html, re.IGNORECASE | re.DOTALL)

    for href, inner_html in matches:
        try:
            # Defensive casting for html.unescape
            clean_href = html.unescape(str(href)).strip()
            raw_text = re.sub(r'<[^>]+>', '', str(inner_html)).strip()
            text = html.unescape(raw_text)
            text = re.sub(r'\s+', ' ', text).strip()

            if not text or len(text) < 3 or not clean_href:
                continue

            abs_url = absolutize_url(base_url, clean_href)

            # Use positive-match filter to avoid discarding valid jobs
            if not _is_actual_job_url(abs_url):
                continue

            job = {"title": text.strip(), "location": "", "url": abs_url}
            dedup_key = f"{text.lower()}|{abs_url}".lower()
            if dedup_key not in seen_urls:
                seen_urls.add(dedup_key)
                jobs.append(job)
        except Exception:
            continue

    return jobs[:200]


def _is_actual_job_url(url: str) -> bool:
    """Only allow URLs that contain explicit job indicators."""
    url_lower = url.lower()

    if _is_structured_job_detail_url(url_lower):
        return True

    # If it contains these, it's a job.
    is_job = any(kw in url_lower for kw in ["/job/", "/job-details", "/position/"])
    # If it contains these, it's a navigation/search page, not a job.
    is_nav = any(nav in url_lower for nav in ["/search", "/results", "/benefits", "/life-at-"])

    return is_job and not is_nav


def _is_structured_job_detail_url(url: str) -> bool:
    """Allow known careers detail URLs without weakening generic navigation filters."""
    if any(nav in url for nav in ("/career-search", "/careers/why-", "/careers/page/")):
        return False
    return any(pattern.search(url) for pattern in _STRUCTURED_JOB_DETAIL_PATTERNS)
