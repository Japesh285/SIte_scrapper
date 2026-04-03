from app.detectors.dom_common import summarize_dom_html


async def detect_dom_browser(
    url: str,
    html: str | None = None,
    discovered_urls: list[str] | None = None,
    browser_probe: dict | None = None,
) -> dict:
    summary = summarize_dom_html(html, discovered_urls)
    dom_signals = (browser_probe or {}).get("dom_signals", {})

    keyword_hits = summary["keyword_hits"]
    pagination_hits = summary["pagination_hits"]
    job_link_count = summary["job_link_count"]
    browser_signal_count = summary["browser_signal_count"]
    next_controls = int(dom_signals.get("next_controls", 0))
    numbered_pagination = int(dom_signals.get("numbered_pagination", 0))
    browser_job_count = int(dom_signals.get("job_anchor_count", 0))

    matched = job_link_count >= 3 or keyword_hits >= 4 or browser_job_count >= 4
    browser_compatible = matched and (
        pagination_hits > 0
        or job_link_count >= 5
        or browser_signal_count >= 3
        or next_controls > 0
        or numbered_pagination > 0
    )
    confidence = 0
    if browser_compatible:
        confidence = 2 + min(next_controls, 2) + min(numbered_pagination, 2)

    return {
        "matched": matched,
        "jobs_found": max(job_link_count, browser_job_count),
        "api_usable": browser_compatible,
        "browser_compatible": browser_compatible,
        "keyword_hits": keyword_hits,
        "pagination_hits": pagination_hits,
        "browser_signals": browser_signal_count,
        "next_controls": next_controls,
        "numbered_pagination": numbered_pagination,
        "confidence": confidence,
    }
