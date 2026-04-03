from app.detectors.dom_common import summarize_dom_html


async def detect_dom_infinite_scroll(
    url: str,
    html: str | None = None,
    discovered_urls: list[str] | None = None,
    browser_probe: dict | None = None,
) -> dict:
    summary = summarize_dom_html(html, discovered_urls)
    dom_signals = (browser_probe or {}).get("dom_signals", {})

    browser_job_count = int(dom_signals.get("job_anchor_count", 0))
    scrollable_container = bool(dom_signals.get("scrollable_container", False))
    scroll_growth = int(dom_signals.get("scroll_growth", 0))
    page_height_delta = int(dom_signals.get("page_height_delta", 0))
    load_more_controls = int(dom_signals.get("load_more_controls", 0))
    next_controls = int(dom_signals.get("next_controls", 0))
    numbered_pagination = int(dom_signals.get("numbered_pagination", 0))

    matched = (
        summary["job_link_count"] >= 3
        or summary["keyword_hits"] >= 4
        or browser_job_count >= 4
    )
    no_explicit_pagination = load_more_controls == 0 and next_controls == 0 and numbered_pagination == 0
    api_usable = matched and (
        scroll_growth > 0
        or (scrollable_container and no_explicit_pagination and page_height_delta > 0)
    )
    confidence = 0
    if api_usable:
        confidence = 4 + min(scroll_growth, 4) + (2 if scrollable_container else 0)

    return {
        "matched": matched,
        "jobs_found": max(summary["job_link_count"], browser_job_count),
        "api_usable": api_usable,
        "browser_compatible": api_usable,
        "scrollable_container": scrollable_container,
        "scroll_growth": scroll_growth,
        "page_height_delta": page_height_delta,
        "confidence": confidence,
    }
