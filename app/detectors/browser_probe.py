"""Unified Playwright probe — network interception + interaction simulation.

This module launches a SINGLE browser session that:
1. Loads the page
2. Waits for auto-loading content
3. Captures initial HTML
4. Attaches network listeners (requests + JSON responses)
5. Performs interaction simulation (clicks, scrolls, fills inputs)
6. Captures final HTML
7. Returns structured probe data for downstream detectors

DO NOT run multiple browser sessions — this is the ONE probe.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from app.core.logger import logger

# Interaction keywords — generic, non-brittle
_CLICK_KEYWORDS = ["search", "jobs", "find", "view", "explore", "load more", "show more"]
_INPUT_TEST_TEXT = "software"
_SCROLL_COUNT = 5
_WAIT_AFTER_INTERACTION_MS = 2000


@dataclass
class NetworkRequest:
    """Captured HTTP request metadata."""
    url: str = ""
    method: str = "GET"
    headers: dict[str, str] = field(default_factory=dict)
    post_data: str | None = None


@dataclass
class NetworkResponse:
    """Captured HTTP JSON response."""
    url: str = ""
    status: int = 0
    body: dict | list | None = None
    body_length: int = 0


@dataclass
class BrowserProbeResult:
    """Structured result from the browser probe."""
    available: bool = False
    final_url: str = ""
    initial_html: str = ""
    final_html: str = ""
    requests: list[NetworkRequest] = field(default_factory=list)
    responses: list[NetworkResponse] = field(default_factory=list)
    dom_changed: bool = False
    job_links_count: int = 0
    interactions_performed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    # Derived metrics (for detectors)
    @property
    def json_responses(self) -> list[NetworkResponse]:
        """All captured JSON responses."""
        return self.responses

    @property
    def json_urls(self) -> list[str]:
        """All URLs that returned JSON."""
        return [r.url for r in self.responses]

    @property
    def request_urls(self) -> list[str]:
        """All captured request URLs."""
        return [r.url for r in self.requests]


async def run_browser_probe(url: str, *, wait_ms: int = 3000) -> BrowserProbeResult:
    """Run a unified browser probe with network interception and interaction.

    Parameters
    ----------
    url : str
        The career page URL to probe.
    wait_ms : int
        Initial wait time after page load (milliseconds). Default 3000.

    Returns
    -------
    BrowserProbeResult
        Structured probe data for downstream detectors.
    """
    result = BrowserProbeResult(available=False)

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        result.errors.append("Playwright not installed")
        logger.warning("[BrowserProbe] Playwright not available")
        return result

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

            # ── Attach network listeners BEFORE navigation ──────────────
            captured_requests: list[NetworkRequest] = []
            captured_responses: list[NetworkResponse] = []

            async def _on_request(request_obj):
                try:
                    req = NetworkRequest(
                        url=request_obj.url,
                        method=request_obj.method,
                        headers=request_obj.headers,
                        post_data=request_obj.post_data,
                    )
                    captured_requests.append(req)
                except Exception as exc:
                    logger.debug("[BrowserProbe] Request capture error: %s", exc)

            async def _on_response(response_obj):
                try:
                    content_type = response_obj.headers.get("content-type", "")
                    # Capture ONLY JSON responses
                    if "json" not in content_type.lower():
                        return

                    status = response_obj.status
                    resp_url = response_obj.url

                    # Try to parse JSON body
                    try:
                        body = await response_obj.json()
                        body_text = json.dumps(body)
                        resp = NetworkResponse(
                            url=resp_url,
                            status=status,
                            body=body,
                            body_length=len(body_text),
                        )
                        captured_responses.append(resp)
                    except Exception:
                        # Not valid JSON — skip
                        pass
                except Exception as exc:
                    logger.debug("[BrowserProbe] Response capture error: %s", exc)


            page.on("request", _on_request)
            page.on("response", _on_response)

            logger.info("[BrowserProbe] Starting probe for %s", url)

            # ── Step 1: Load page ───────────────────────────────────────
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            except Exception as exc:
                result.errors.append(f"Initial navigation failed: {exc}")
                logger.warning("[BrowserProbe] Navigation failed: %s", exc)
                await browser.close()
                return result

            # ── Step 2: Wait for auto-loading content ───────────────────
            await page.wait_for_timeout(wait_ms)

            # ── Step 3: Capture initial HTML ────────────────────────────
            try:
                result.initial_html = await page.content()
            except Exception as exc:
                result.errors.append(f"Initial HTML capture failed: {exc}")
                logger.warning("[BrowserProbe] Initial HTML capture failed: %s", exc)

            logger.info(
                "[BrowserProbe] Initial HTML captured: %d chars, %d requests, %d JSON responses",
                len(result.initial_html),
                len(captured_requests),
                len(captured_responses),
            )

            # ── Step 4: Interaction simulation ──────────────────────────
            interactions = await _simulate_interactions(page)
            result.interactions_performed = interactions

            if interactions:
                logger.info("[BrowserProbe] Performed %d interactions: %s", len(interactions), interactions)
                # Wait for content to load after interactions
                await page.wait_for_timeout(_WAIT_AFTER_INTERACTION_MS)

            # ── Step 5: Capture final HTML ──────────────────────────────
            try:
                result.final_html = await page.content()
                result.final_url = page.url
            except Exception as exc:
                logger.warning("[BrowserProbe] Final HTML capture failed: %s", exc)
                result.final_html = result.initial_html

            # ── Step 6: Compute DOM change metrics ──────────────────────
            initial_len = len(result.initial_html)
            final_len = len(result.final_html)
            result.dom_changed = final_len > initial_len * 1.3 if initial_len > 0 else False

            # Count job-like links
            result.job_links_count = await _count_job_links(page)

            # ── Finalize result ─────────────────────────────────────────
            result.available = True
            result.requests = captured_requests
            result.responses = captured_responses

            logger.info(
                "[BrowserProbe] Done — dom_changed=%s, job_links=%d, "
                "requests=%d, json_responses=%d",
                result.dom_changed,
                result.job_links_count,
                len(captured_requests),
                len(captured_responses),
            )

            await browser.close()

    except Exception as exc:
        result.errors.append(f"Browser probe failed: {exc}")
        logger.error("[BrowserProbe] Fatal error: %s", exc)

    return result


async def _simulate_interactions(page) -> list[str]:
    """Simulate generic user interactions to trigger dynamic content loading.

    Actions (in order):
    1. Click buttons with job-related keywords
    2. Fill search inputs with test text and press Enter
    3. Scroll the page multiple times
    4. Click "load more" / "show more" buttons

    Returns list of action descriptions.
    """
    actions: list[str] = []

    # ── 1. Click keyword-matching buttons ─────────────────────
    for keyword in _CLICK_KEYWORDS:
        try:
            # Find buttons by text content (case-insensitive)
            buttons = await page.locator(
                f"button:has-text('{keyword}'), a:has-text('{keyword}'), "
                f"[role='button']:has-text('{keyword}'), input[type='submit']:has-text('{keyword}')"
            ).all()

            for btn in buttons[:3]:  # Max 3 per keyword to avoid over-clicking
                try:
                    if await btn.is_visible():
                        await btn.click(timeout=2000)
                        await page.wait_for_timeout(500)
                        actions.append(f"clicked_{keyword}")
                except Exception:
                    pass  # Button may have become stale
        except Exception:
            pass

    # ── 2. Fill search inputs ────────────────────────────────
    try:
        inputs = await page.locator(
            "input[type='text'], input[type='search'], input[name*='search'], "
            "input[placeholder*='search'], input[placeholder*='job']"
        ).all()

        for inp in inputs[:2]:  # Max 2 inputs
            try:
                if await inp.is_visible():
                    await inp.fill(_INPUT_TEST_TEXT, timeout=2000)
                    await inp.press("Enter")
                    await page.wait_for_timeout(1000)
                    actions.append("filled_search")
            except Exception:
                pass
    except Exception:
        pass

    # ── 3. Scroll multiple times ─────────────────────────────
    for i in range(_SCROLL_COUNT):
        try:
            await page.evaluate("window.scrollBy(0, window.innerHeight)")
            await page.wait_for_timeout(400)
        except Exception:
            break
    actions.append("scrolled")

    # ── 4. Click load-more / show-more buttons explicitly ────
    load_more_keywords = ["load more", "show more", "load jobs", "show jobs", "see more"]
    for keyword in load_more_keywords:
        try:
            buttons = await page.locator(
                f"button:has-text('{keyword}'), a:has-text('{keyword}'), "
                f"[role='button']:has-text('{keyword}')"
            ).all()

            for btn in buttons[:2]:
                try:
                    if await btn.is_visible():
                        await btn.click(timeout=2000)
                        await page.wait_for_timeout(1000)
                        actions.append(f"clicked_{keyword.replace(' ', '_')}")
                except Exception:
                    pass
        except Exception:
            pass

    return actions


async def _count_job_links(page) -> int:
    """Count job-like anchor elements on the page.

    Looks for <a> tags with href containing job-related patterns.
    """
    try:
        # Count links with job-related href patterns
        job_link_count = await page.evaluate("""
            () => {
                const patterns = ['job', 'position', 'career', 'opening', 'apply'];
                const links = Array.from(document.querySelectorAll('a[href]'));
                return links.filter(a => {
                    const href = a.getAttribute('href').toLowerCase();
                    const text = (a.textContent || '').toLowerCase();
                    return patterns.some(p => href.includes(p) || text.includes(p));
                }).length;
            }
        """)
        return int(job_link_count)
    except Exception:
        return 0
