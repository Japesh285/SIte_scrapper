from app.detectors.dom_common import summarize_dom_html


async def detect_dom_load_more(
    url: str,
    html: str | None = None,
    discovered_urls: list[str] | None = None,
    browser_probe: dict | None = None,
) -> dict:
    summary = summarize_dom_html(html, discovered_urls)
    dom_signals = (browser_probe or {}).get("dom_signals", {})

    browser_job_count = int(dom_signals.get("job_anchor_count", 0))
    load_more_controls = int(dom_signals.get("load_more_controls", 0))
    load_more_growth = int(dom_signals.get("load_more_growth", 0))
    load_more_clicked = bool(dom_signals.get("load_more_clicked", False))

    matched = (
        summary["job_link_count"] >= 3
        or summary["keyword_hits"] >= 4
        or browser_job_count >= 4
    )
    api_usable = matched and load_more_controls > 0 and (load_more_growth > 0 or load_more_clicked)
    confidence = 0
    if api_usable:
        confidence = 4 + min(load_more_controls, 2) + min(load_more_growth, 4)

    return {
        "matched": matched,
        "jobs_found": max(summary["job_link_count"], browser_job_count),
        "api_usable": api_usable,
        "browser_compatible": api_usable,
        "load_more_controls": load_more_controls,
        "load_more_growth": load_more_growth,
        "load_more_clicked": load_more_clicked,
        "confidence": confidence,
    }
