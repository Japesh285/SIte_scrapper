from app.core.logger import logger
from app.core.site_utils import absolutize_url

try:
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError
    from playwright.async_api import async_playwright
except Exception:  # pragma: no cover
    PlaywrightTimeoutError = Exception
    async_playwright = None


MAX_BROWSER_PAGES = 8
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


async def scrape_dom_browser(url: str, max_pages: int = MAX_BROWSER_PAGES) -> list[dict]:
    return await _scrape_dom_mode(url, mode="paged", max_pages=max_pages)


async def scrape_dom_load_more(url: str, max_pages: int = MAX_BROWSER_PAGES) -> list[dict]:
    return await _scrape_dom_mode(url, mode="load_more", max_pages=max_pages)


async def scrape_dom_infinite_scroll(url: str, max_pages: int = MAX_BROWSER_PAGES) -> list[dict]:
    return await _scrape_dom_mode(url, mode="infinite_scroll", max_pages=max_pages)


async def _scrape_dom_mode(url: str, mode: str, max_pages: int = MAX_BROWSER_PAGES) -> list[dict]:
    if async_playwright is None:
        logger.warning("[DOM] Playwright unavailable")
        return []

    collected: list[dict] = []
    seen: set[str] = set()
    no_growth_rounds = 0

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page()

            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(2500)

            for round_index in range(max_pages):
                extracted = await _extract_jobs_from_page(page, url)
                new_count = 0
                for job in extracted:
                    key = (job.get("url") or f"{job.get('title', '')}|{job.get('location', '')}").lower()
                    if key in seen:
                        continue
                    seen.add(key)
                    collected.append(job)
                    new_count += 1

                logger.info("[DOM:%s] Round %s extracted=%s new=%s total=%s", mode, round_index + 1, len(extracted), new_count, len(collected))

                if new_count == 0:
                    no_growth_rounds += 1
                else:
                    no_growth_rounds = 0

                progressed = await _advance_dom_results(page, mode)
                logger.info("[DOM:%s] Progressed=%s no_growth_rounds=%s", mode, progressed, no_growth_rounds)

                if not progressed:
                    break

                try:
                    await page.wait_for_load_state("networkidle", timeout=5000)
                except Exception:
                    pass
                await page.wait_for_timeout(1500)

                if no_growth_rounds >= MAX_NO_GROWTH_ROUNDS:
                    logger.info("[DOM:%s] Stopping after %s rounds without new jobs", mode, no_growth_rounds)
                    break

            await browser.close()
    except Exception as exc:
        logger.error(f"[DOM:{mode}] Browser scrape error: {exc}")
        return []

    return collected


async def _extract_jobs_from_page(page, base_url: str) -> list[dict]:
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
        if any(part in lower_href for part in BAD_URL_PARTS):
            continue
        if item.get("isBadUrl"):
            continue
        if not item.get("isLikelyJobUrl") and not str(item.get("location", "")).strip():
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
