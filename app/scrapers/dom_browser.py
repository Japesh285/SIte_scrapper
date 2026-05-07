import re

from app.core.logger import logger
from app.core.site_utils import absolutize_url

try:
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError
    from playwright.async_api import async_playwright
except Exception:  # pragma: no cover
    PlaywrightTimeoutError = Exception
    async_playwright = None


MAX_NO_GROWTH_ROUNDS = 2
GENERIC_TITLES = {
    "english",
    "careers",
    "career",
    "saved jobs",
    "saved jobs 0",
    "my applications",
    "search jobs",
    "open positions",
    "subscribe for job alerts",
    "join our talent community",
    "why concentrix",
    "perks & benefits",
    "sustainability",
    "recruitment fraud alert",
    "legal",
    "privacy policy",
    "gdpr",
    "personal information",
    "google's eeo policy",
    "eeo (english)",
    "eeo (spanish)",
    "how we hire",
    "learn more about our culture",
    "learn more about our benefits",
    "explore remote jobs",
    "apply now",
    "view details",
    "step 1: application",
    "step 2: digital self assessment",
    "step 3: process specific assessments",
    "step 4: interview",
    "step 5: offer letter",
}
GENERIC_TITLE_PARTS = (
    "talent community",
    "job alerts",
    "saved jobs",
    "privacy",
    "legal",
    "cookie",
    "application",
    "interview",
    "offer letter",
    "fraud",
)
BAD_URL_PARTS = (
    "/saved-jobs",
    "/job-cart",
    "/profile/",
    "/login",
    "/privacy",
    "/legal",
    "/cookies",
    "/job-alert",
    "/join-community",
    "/apply-form/",
    "/accessibility",
    "/benefits",
    "/culture",
    "/how-we-hire",
    "/eeo",
    ".pdf",
)
# Strict reject patterns for detail URLs — these are NEVER job pages
DETAIL_REJECT_PARTS = (
    "/careers/",
    "/career-search",
    "/why-",
    "/about",
    "/learning",
    "/projects",
    "/contact",
    "/our-team",
    "/testimonials",
    "/events",
    "/blog",
    "/news",
    "/insights",
    "/resources",
    "/webinars",
)
# Accept signals — URL must contain at least one of these
JOB_ACCEPT_PARTS = (
    "/job/",
    "/jobs/",
    "/position/",
    "/opening/",
    "/requisition/",
    "/vacancy/",
    "/career/",
    "/careers/",
    "jobid",
    "reqid",
    "job-id",
    "req-id",
)
STRUCTURED_JOB_DETAIL_PATTERNS = (
    re.compile(r"/careers/[^/?#]*-(?:irc|req|job|jr)\d+(?:/|$)", re.IGNORECASE),
    re.compile(r"/careers/[^/?#]*\d{4,}(?:/|$)", re.IGNORECASE),
)


async def scrape_dom_browser(url: str, max_pages: int | None = None) -> list[dict]:
    return await _scrape_dom_mode(url, mode="paged", max_pages=max_pages)


async def scrape_dom_load_more(url: str, max_pages: int | None = None) -> list[dict]:
    return await _scrape_dom_mode(url, mode="load_more", max_pages=max_pages)


async def scrape_dom_infinite_scroll(url: str, max_pages: int | None = None) -> list[dict]:
    return await _scrape_dom_mode(url, mode="infinite_scroll", max_pages=max_pages)


async def _scrape_dom_mode(url: str, mode: str, max_pages: int | None = None) -> list[dict]:
    if async_playwright is None:
        logger.warning("[DOM] Playwright unavailable")
        return []

    collected: list[dict] = []
    seen: set[str] = set()
    no_growth_rounds = 0
    used_relaxed_selectors = False

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page()

            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await _wait_for_results_settle(page, initial=True)

            round_index = 0
            while True:
                if max_pages is not None and round_index >= max_pages:
                    break
                extracted = await _extract_jobs_from_page(page, url, relaxed=used_relaxed_selectors)
                new_count = 0
                for job in extracted:
                    key = (job.get("url") or f"{job.get('title', '')}|{job.get('location', '')}").lower()
                    if key in seen:
                        continue
                    seen.add(key)
                    collected.append(job)
                    new_count += 1

                logger.info("[DOM:%s] Round %s extracted=%s new=%s total=%s relaxed=%s", mode, round_index + 1, len(extracted), new_count, len(collected), used_relaxed_selectors)

                if new_count == 0:
                    no_growth_rounds += 1
                else:
                    no_growth_rounds = 0

                progressed = await _advance_dom_results(page, mode)
                logger.info("[DOM:%s] Progressed=%s no_growth_rounds=%s", mode, progressed, no_growth_rounds)

                if not progressed:
                    if await _reload_and_wait(page, reason=f"{mode}:no_progress"):
                        logger.info("[DOM:%s] Reloaded page after stalled pagination", mode)
                        progressed = True
                    else:
                        break

                await _wait_for_results_settle(page)

                if no_growth_rounds >= 1:
                    recovered = await _recover_if_results_look_empty(page, mode)
                    if recovered:
                        await _wait_for_results_settle(page)

                if no_growth_rounds >= MAX_NO_GROWTH_ROUNDS:
                    logger.info("[DOM:%s] Stopping after %s rounds without new jobs", mode, no_growth_rounds)
                    break
                round_index += 1

            await browser.close()
    except Exception as exc:
        logger.error(f"[DOM:{mode}] Browser scrape error: {exc}")
        return []

    # ── RETRY with relaxed selectors if detection found jobs but extraction returned 0 ──
    if not collected and len(seen) > 10:
        # This shouldn't happen, but if we previously extracted many jobs that all
        # got filtered out, retry with relaxed selectors
        logger.info("[DOM RETRY] Previously found jobs but all filtered — retrying with relaxed selectors")
        # Re-run extraction with relaxed mode
        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True)
                page = await browser.new_page()
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await _wait_for_results_settle(page, initial=True)
                logger.info("[DOM RETRY] Using relaxed selectors")
                round_index = 0
                while True:
                    if max_pages is not None and round_index >= max_pages:
                        break
                    extracted = await _extract_jobs_from_page(page, url, relaxed=True)
                    for job in extracted:
                        key = (job.get("url") or f"{job.get('title', '')}|{job.get('location', '')}").lower()
                        if key not in seen:
                            seen.add(key)
                            collected.append(job)
                    progressed = await _advance_dom_results(page, mode)
                    if not progressed:
                        break
                    await _wait_for_results_settle(page)
                    round_index += 1
                await browser.close()
        except Exception as exc:
            logger.error(f"[DOM RETRY] Failed: {exc}")

    return collected


async def _extract_jobs_from_page(page, base_url: str, relaxed: bool = False) -> list[dict]:
    """Extract job links from the current page.

    Parameters
    ----------
    page : Playwright page
    base_url : str
    relaxed : bool
        If True, use broader selectors that match more links (fallback mode).
    """
    if relaxed:
        data = await page.evaluate(
            """
            () => {
              const visible = (el) => {
                if (!el) return false;
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0;
              };

              const results = [];
              const anchors = Array.from(document.querySelectorAll("a[href]"));
              for (const anchor of anchors) {
                if (!visible(anchor)) continue;
                const href = anchor.getAttribute("href") || "";
                const hrefLower = href.toLowerCase();
                // Relaxed: match any href containing job/career/position keywords
                if (!["job", "career", "position", "opening", "req", "vacancy"].some(t => hrefLower.includes(t))) continue;
                const text = (anchor.innerText || anchor.textContent || "").trim();
                if (!text) continue;

                let location = "";
                let title = text;
                const card = anchor.closest("article, li, div");
                if (card) {
                  const cardText = (card.innerText || card.textContent || "").trim().split("\\n").map(p => p.trim()).filter(Boolean);
                  for (const part of cardText) {
                    const lower = part.toLowerCase();
                    if (lower.length > 2 && !lower.includes("apply") && !lower.includes("save") && !lower.includes("share")) {
                      if (/[a-z]/i.test(part) && (part.includes(",") || lower.includes("remote") || lower.includes("india") || lower.includes("united") || lower.includes("hyderabad") || lower.includes("bangalore"))) {
                        location = part;
                        if (!title || title.length < 5) title = part;
                        break;
                      }
                    }
                  }
                }
                results.push({ title, url: href, location });
              }
              return results;
            }
            """
        )
    else:
        data = await page.evaluate(
            """
            () => {
              const visible = (el) => {
                if (!el) return false;
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0;
              };

                const results = [];
                const anchors = Array.from(document.querySelectorAll("a[href]"));
                for (const anchor of anchors) {
                    if (!visible(anchor)) continue;
                    const href = anchor.getAttribute("href") || "";
                    const text = (anchor.innerText || anchor.textContent || "").trim();
                if (!href || !text) continue;
                const haystack = `${href} ${text}`.toLowerCase();
                if (!["job", "career", "position", "opening", "requisition", "vacancy"].some(token => haystack.includes(token))) continue;

                const hrefLower = href.toLowerCase();
                const isLikelyJobUrl =
                  hrefLower.includes("/job/") ||
                  hrefLower.includes("/job-detail/") ||
                  hrefLower.includes("/jobs/detail") ||
                  hrefLower.includes("/position/") ||
                  hrefLower.includes("/opening/") ||
                  hrefLower.includes("/requisition/");

                const isBadUrl =
                  hrefLower.includes("/saved-jobs") ||
                  hrefLower.includes("/job-cart") ||
                  hrefLower.includes("/profile/") ||
                  hrefLower.includes("/login") ||
                  hrefLower.includes("/privacy") ||
                  hrefLower.includes("/legal") ||
                  hrefLower.includes("/cookies") ||
                  hrefLower.includes("/job-alert") ||
                  hrefLower.includes("/join-community") ||
                  hrefLower.includes("/apply-form/") ||
                  hrefLower.includes("/accessibility") ||
                  hrefLower.includes("/benefits") ||
                  hrefLower.includes("/culture") ||
                  hrefLower.includes("/how-we-hire") ||
                  hrefLower.includes("/eeo") ||
                  hrefLower.endsWith(".pdf");

                let location = "";
                let title = text;
                const card = anchor.closest("article, li, div");
                if (card) {
                  const cardText = (card.innerText || card.textContent || "").trim().split("\\n").map(part => part.trim()).filter(Boolean);
                  if (["apply now", "view details"].includes(text.toLowerCase())) {
                    const replacement = cardText.find(part => {
                      const lower = part.toLowerCase();
                      if (!part || part.trim().length < 6) return false;
                      if (lower === text.toLowerCase()) return false;
                      if (lower.includes("apply now") || lower.includes("view details")) return false;
                      if (lower.includes("saved jobs") || lower.includes("talent community")) return false;
                      return true;
                    });
                    if (replacement) title = replacement;
                  }
                  for (const part of cardText.slice(1, 8)) {
                    const lower = part.toLowerCase();
                    if (lower.length > 2 && !lower.includes("apply") && !lower.includes("save job") && !lower.includes("share")) {
                      if (/[a-z]/i.test(part) && (part.includes(",") || lower.includes("remote") || lower.includes("india") || lower.includes("united") || lower.includes("hyderabad") || lower.includes("bangalore"))) {
                        location = part;
                        break;
                      }
                    }
                  }
                }

                results.push({ title, url: href, location, isLikelyJobUrl, isBadUrl });
              }
              return results;
            }
            """
        )

    jobs: list[dict] = []
    for item in data:
        title = str(item.get("title", "")).strip()
        href = str(item.get("url", "")).strip()
        if not title or len(title) < 3 or not href:
            continue
        lower_title = title.lower()
        if lower_title in {"search", "apply", "menu", "next", "previous"}:
            continue
        if lower_title in GENERIC_TITLES:
            continue
        if any(part in lower_title for part in GENERIC_TITLE_PARTS):
            continue
        lower_href = href.lower()
        is_structured_job_detail = _is_structured_job_detail_url(lower_href)

        # ── Strict detail URL rejection ──
        if any(part in lower_href for part in DETAIL_REJECT_PARTS) and not is_structured_job_detail:
            logger.info("[FILTER] Rejected non-job URL: %s", href)
            continue

        if not relaxed:
            if any(part in lower_href for part in BAD_URL_PARTS):
                continue
            if item.get("isBadUrl"):
                continue
            if not item.get("isLikelyJobUrl") and not is_structured_job_detail and not str(item.get("location", "")).strip():
                continue
        else:
            # Relaxed mode: still reject obvious bad URLs
            if any(part in lower_href for part in BAD_URL_PARTS):
                continue
            if any(part in lower_href for part in DETAIL_REJECT_PARTS) and not is_structured_job_detail:
                logger.info("[FILTER] Rejected non-job URL (relaxed): %s", href)
                continue
            # In relaxed mode, require at least some job signal in the URL
            if not is_structured_job_detail and not any(p in lower_href for p in ("job", "career", "position", "opening", "req", "vacancy")):
                continue

        jobs.append(
            {
                "title": title,
                "location": str(item.get("location", "")).strip(),
                "url": absolutize_url(base_url, href),
            }
        )
    return _dedupe_jobs(jobs)


async def _trigger_pagination(page) -> bool:
    return await _trigger_next_page(page)


async def _advance_dom_results(page, mode: str) -> bool:
    if mode == "load_more":
        clicked = await _trigger_load_more(page)
        if clicked:
            return True
        await _scroll_results(page)
        return await _trigger_load_more(page)
    if mode == "infinite_scroll":
        scrolled = await _scroll_results(page)
        if scrolled:
            return True
        return await _trigger_load_more(page)
    clicked = await _trigger_next_page(page)
    if clicked:
        return True
    return await _scroll_results(page)


async def _trigger_load_more(page) -> bool:
    result = await page.evaluate(
        """
        () => {
          const visible = (el) => {
            if (!el) return false;
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0;
          };

          const candidates = Array.from(document.querySelectorAll("button, a")).filter(visible);
          let best = null;
          let bestScore = -1;

          for (const el of candidates) {
            const text = (el.innerText || el.textContent || "").trim().toLowerCase();
            const aria = (el.getAttribute("aria-label") || "").trim().toLowerCase();
            const cls = (el.className || "").toString().toLowerCase();
            const haystack = `${text} ${aria} ${cls}`;
            if (el.disabled || el.getAttribute("aria-disabled") === "true") continue;
            if (haystack.includes("previous")) continue;

            let score = 0;
            if (haystack.includes("load more")) score += 30;
            if (haystack.includes("show more")) score += 20;
            if (haystack.includes("more jobs")) score += 18;
            if (haystack.includes("view more")) score += 12;
            if (haystack.includes("careers home")) score -= 20;
            if (haystack.includes("search jobs")) score -= 20;
            if (score > bestScore) {
              best = el;
              bestScore = score;
            }
          }

          if (!best || bestScore <= 0) return { clicked: false };
          best.scrollIntoView({ block: "center", inline: "center" });
          best.click();
          return { clicked: true };
        }
        """
    )
    return bool(result.get("clicked"))


async def _trigger_next_page(page) -> bool:
    result = await page.evaluate(
        """
        () => {
          const visible = (el) => {
            if (!el) return false;
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0;
          };

          const candidates = Array.from(document.querySelectorAll("button, a")).filter(visible);
          let best = null;
          let bestScore = -1;

          for (const el of candidates) {
            const text = (el.innerText || el.textContent || "").trim().toLowerCase();
            const aria = (el.getAttribute("aria-label") || "").trim().toLowerCase();
            const cls = (el.className || "").toString().toLowerCase();
            const haystack = `${text} ${aria} ${cls}`;
            if (el.disabled || el.getAttribute("aria-disabled") === "true") continue;
            if (haystack.includes("previous")) continue;

            let score = 0;
            if (haystack.includes("next jobs")) score += 25;
            if (haystack.includes("next page")) score += 20;
            if (haystack.includes("next")) score += 12;
            if (haystack.includes("pagination-next")) score += 15;
            if (/\\bpage\\s*\\d+\\b/.test(haystack)) score += 10;
            if (text === "next") score += 8;
            if (haystack.includes("search jobs")) score -= 20;
            if (score > bestScore) {
              best = el;
              bestScore = score;
            }
          }

          if (!best || bestScore <= 0) return { clicked: false };
          best.scrollIntoView({ block: "center", inline: "center" });
          best.click();
          return { clicked: true };
        }
        """
    )
    return bool(result.get("clicked"))


async def _scroll_results(page) -> bool:
    result = await page.evaluate(
        """
        () => {
          const visible = (el) => {
            if (!el) return false;
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0;
          };

          const containers = Array.from(document.querySelectorAll("div, section, main, ul")).filter(visible);
          const scrollable = containers
            .filter((el) => {
              const style = window.getComputedStyle(el);
              return ["auto", "scroll"].includes(style.overflowY) && el.scrollHeight > el.clientHeight + 100;
            })
            .sort((a, b) => b.scrollHeight - a.scrollHeight)[0];

          if (scrollable) {
            const before = scrollable.scrollTop;
            scrollable.scrollTop = Math.min(scrollable.scrollTop + Math.max(scrollable.clientHeight, 800), scrollable.scrollHeight);
            return { changed: scrollable.scrollTop !== before };
          }

          const before = window.scrollY;
          window.scrollTo(0, Math.min(window.scrollY + window.innerHeight * 1.5, document.body.scrollHeight));
          return { changed: window.scrollY !== before };
        }
        """
    )
    if result.get("changed"):
        await page.wait_for_timeout(1500)
    return bool(result.get("changed"))


async def _wait_for_results_settle(page, initial: bool = False) -> None:
    """Give JS-heavy result pages time to re-render after pagination actions."""
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=8000)
    except Exception:
        pass
    try:
        await page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass

    wait_ms = 2800 if initial else 2200
    await page.wait_for_timeout(wait_ms)

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


async def _recover_if_results_look_empty(page, mode: str) -> bool:
    """Reload current page if a pagination action seems to have produced an empty JS state."""
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
              return { links, likelyEmpty, textLength: text.length };
            }
            """
        )
    except Exception:
        return False

    if stats.get("likelyEmpty") or (stats.get("links", 0) < 8 and stats.get("textLength", 0) < 1200):
        logger.info("[DOM:%s] Suspected false empty state after pagination, reloading current page", mode)
        return await _reload_and_wait(page, reason=f"{mode}:false_empty")
    return False


async def _reload_and_wait(page, reason: str) -> bool:
    try:
        logger.info("[DOM] Reloading page (%s)", reason)
        await page.reload(wait_until="domcontentloaded", timeout=30000)
        await _wait_for_results_settle(page, initial=True)
        return True
    except Exception as exc:
        logger.warning("[DOM] Reload failed (%s): %s", reason, exc)
        return False


def _dedupe_jobs(jobs: list[dict]) -> list[dict]:
    deduped: list[dict] = []
    seen: set[str] = set()
    for job in jobs:
        key = (job.get("url") or f"{job.get('title', '')}|{job.get('location', '')}").lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(job)
    return deduped


def _is_structured_job_detail_url(url: str) -> bool:
    """Allow known careers detail URLs without admitting careers index/search pages."""
    if any(nav in url for nav in ("/career-search", "/careers/why-", "/careers/page/")):
        return False
    return any(pattern.search(url) for pattern in STRUCTURED_JOB_DETAIL_PATTERNS)
