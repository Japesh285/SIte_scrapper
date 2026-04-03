from app.core.logger import logger

try:
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError
    from playwright.async_api import async_playwright
except Exception:  # pragma: no cover
    PlaywrightTimeoutError = Exception
    async_playwright = None


async def inspect_browser_network(url: str, wait_ms: int = 5000) -> dict:
    if async_playwright is None:
        return _empty_probe("playwright_unavailable")

    json_urls: set[str] = set()
    request_urls: set[str] = set()
    errors: list[str] = []
    final_url = url
    dom_signals = _empty_dom_signals()

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page()

            async def on_response(response) -> None:
                try:
                    content_type = response.headers.get("content-type", "").lower()
                    response_url = response.url
                    if "json" in content_type or _looks_like_api_url(response_url):
                        json_urls.add(response_url)
                except Exception as exc:
                    errors.append(f"response:{exc}")

            async def on_request(request) -> None:
                try:
                    request_url = request.url
                    if _looks_like_api_url(request_url):
                        request_urls.add(request_url)
                except Exception as exc:
                    errors.append(f"request:{exc}")

            page.on("response", on_response)
            page.on("request", on_request)

            try:
                response = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                if response and response.url:
                    final_url = response.url
            except PlaywrightTimeoutError:
                errors.append("goto_timeout")
            except Exception as exc:
                errors.append(f"goto:{exc}")

            await page.wait_for_timeout(wait_ms)
            dom_signals = await _collect_dom_signals(page, errors)

            if page.url:
                final_url = page.url

            await browser.close()
    except Exception as exc:
        logger.warning(f"Browser inspection failed for {url}: {exc}")
        return _empty_probe(str(exc))

    return {
        "available": True,
        "final_url": final_url,
        "json_urls": sorted(json_urls),
        "request_urls": sorted(request_urls),
        "dom_signals": dom_signals,
        "errors": errors,
    }


def _looks_like_api_url(url: str) -> bool:
    lowered = (url or "").lower()
    return any(
        token in lowered
        for token in (
            "api",
            "graphql",
            "job",
            "career",
            "position",
            "opening",
            "greenhouse",
            "workday",
        )
    )


def _empty_probe(reason: str) -> dict:
    return {
        "available": False,
        "final_url": "",
        "json_urls": [],
        "request_urls": [],
        "dom_signals": _empty_dom_signals(),
        "errors": [reason] if reason else [],
    }


def _empty_dom_signals() -> dict:
    return {
        "job_anchor_count": 0,
        "load_more_controls": 0,
        "next_controls": 0,
        "numbered_pagination": 0,
        "scrollable_container": False,
        "page_height": 0,
        "page_height_delta": 0,
        "load_more_clicked": False,
        "load_more_growth": 0,
        "scroll_growth": 0,
    }


async def _collect_dom_signals(page, errors: list[str]) -> dict:
    initial = await _snapshot_dom_state(page)
    load_more_result = await _probe_load_more(page, initial, errors)
    after_load_more = await _snapshot_dom_state(page)
    scroll_result = await _probe_scroll(page, after_load_more, errors)
    final_state = await _snapshot_dom_state(page)

    return {
        "job_anchor_count": int(final_state.get("job_anchor_count", 0)),
        "load_more_controls": int(initial.get("load_more_controls", 0)),
        "next_controls": int(initial.get("next_controls", 0)),
        "numbered_pagination": int(initial.get("numbered_pagination", 0)),
        "scrollable_container": bool(initial.get("scrollable_container", False)),
        "page_height": int(final_state.get("page_height", 0)),
        "page_height_delta": max(0, int(final_state.get("page_height", 0)) - int(initial.get("page_height", 0))),
        "load_more_clicked": bool(load_more_result.get("clicked", False)),
        "load_more_growth": max(0, int(after_load_more.get("job_anchor_count", 0)) - int(initial.get("job_anchor_count", 0))),
        "scroll_growth": max(0, int(final_state.get("job_anchor_count", 0)) - int(after_load_more.get("job_anchor_count", 0))),
    }


async def _snapshot_dom_state(page) -> dict:
    return await page.evaluate(
        """
        () => {
          const visible = (el) => {
            if (!el) return false;
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0;
          };

          const anchors = Array.from(document.querySelectorAll("a[href]")).filter(visible);
          const jobAnchors = anchors.filter((anchor) => {
            const href = (anchor.getAttribute("href") || "").toLowerCase();
            const text = (anchor.innerText || anchor.textContent || "").trim().toLowerCase();
            const haystack = `${href} ${text}`;
            return ["job", "career", "position", "opening", "requisition", "vacancy"].some((token) => haystack.includes(token));
          });

          const controls = Array.from(document.querySelectorAll("button, a")).filter(visible);
          const loadMoreControls = controls.filter((el) => {
            const text = `${el.innerText || el.textContent || ""} ${el.getAttribute("aria-label") || ""}`.trim().toLowerCase();
            return text.includes("load more") || text.includes("show more") || text.includes("more jobs") || text.includes("view more");
          }).length;
          const nextControls = controls.filter((el) => {
            const text = `${el.innerText || el.textContent || ""} ${el.getAttribute("aria-label") || ""} ${el.className || ""}`.trim().toLowerCase();
            return text.includes("next jobs") || text.includes("next page") || (text === "next") || text.includes("pagination-next");
          }).length;

          const numberedPagination = Array.from(document.querySelectorAll("[aria-current='page'], [class*='pagination'], [role='navigation'] button, [role='navigation'] a"))
            .filter(visible)
            .filter((el) => /page\\s*\\d+/i.test(`${el.innerText || el.textContent || ""} ${el.getAttribute("aria-label") || ""}`))
            .length;

          const scrollableContainer = Array.from(document.querySelectorAll("div, section, main, ul"))
            .filter(visible)
            .some((el) => {
              const style = window.getComputedStyle(el);
              return ["auto", "scroll"].includes(style.overflowY) && el.scrollHeight > el.clientHeight + 100;
            });

          return {
            job_anchor_count: jobAnchors.length,
            load_more_controls: loadMoreControls,
            next_controls: nextControls,
            numbered_pagination: numberedPagination,
            scrollable_container: scrollableContainer,
            page_height: Math.max(document.body.scrollHeight, document.documentElement.scrollHeight),
          };
        }
        """
    )


async def _probe_load_more(page, previous_state: dict, errors: list[str]) -> dict:
    try:
        result = await page.evaluate(
            """
            () => {
              const visible = (el) => {
                if (!el) return false;
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0;
              };

              const controls = Array.from(document.querySelectorAll("button, a")).filter(visible);
              const target = controls.find((el) => {
                const text = `${el.innerText || el.textContent || ""} ${el.getAttribute("aria-label") || ""}`.trim().toLowerCase();
                return text.includes("load more") || text.includes("show more") || text.includes("more jobs") || text.includes("view more");
              });

              if (!target) return { clicked: false };
              target.scrollIntoView({ block: "center", inline: "center" });
              target.click();
              return { clicked: true };
            }
            """
        )
        if result.get("clicked"):
            await page.wait_for_timeout(1800)
        return {"clicked": bool(result.get("clicked"))}
    except Exception as exc:
        errors.append(f"load_more_probe:{exc}")
        return {"clicked": False}


async def _probe_scroll(page, previous_state: dict, errors: list[str]) -> dict:
    try:
        result = await page.evaluate(
            """
            () => {
              const visible = (el) => {
                if (!el) return false;
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0;
              };

              const candidates = Array.from(document.querySelectorAll("div, section, main, ul")).filter(visible);
              const scrollable = candidates
                .filter((el) => {
                  const style = window.getComputedStyle(el);
                  return ["auto", "scroll"].includes(style.overflowY) && el.scrollHeight > el.clientHeight + 100;
                })
                .sort((a, b) => b.scrollHeight - a.scrollHeight)[0];

              if (scrollable) {
                const before = scrollable.scrollTop;
                scrollable.scrollTop = Math.min(scrollable.scrollTop + Math.max(700, scrollable.clientHeight), scrollable.scrollHeight);
                return { changed: scrollable.scrollTop !== before };
              }

              const before = window.scrollY;
              window.scrollTo(0, Math.min(window.scrollY + window.innerHeight * 1.5, document.body.scrollHeight));
              return { changed: window.scrollY !== before };
            }
            """
        )
        if result.get("changed"):
            await page.wait_for_timeout(1800)
        return {"changed": bool(result.get("changed"))}
    except Exception as exc:
        errors.append(f"scroll_probe:{exc}")
        return {"changed": False}
