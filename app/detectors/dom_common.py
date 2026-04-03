import re


JOB_TEXT_KEYWORDS = (
    "job",
    "jobs",
    "career",
    "careers",
    "opening",
    "openings",
    "position",
    "positions",
    "apply",
)
PAGINATION_KEYWORDS = (
    "load more",
    "show more",
    "next page",
    "next jobs",
    "pagination",
    "page 2",
)
ANCHOR_PATTERN = re.compile(r"<a\b[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", re.IGNORECASE | re.DOTALL)
TAG_PATTERN = re.compile(r"<[^>]+>")


def summarize_dom_html(html: str | None, discovered_urls: list[str] | None = None) -> dict:
    page_html = html or ""
    lowered_html = page_html.lower()
    text = TAG_PATTERN.sub(" ", lowered_html)

    keyword_hits = sum(1 for keyword in JOB_TEXT_KEYWORDS if keyword in text)
    pagination_hits = sum(1 for keyword in PAGINATION_KEYWORDS if keyword in text)
    job_link_count = 0

    for href, label in ANCHOR_PATTERN.findall(page_html):
        href_lower = href.lower()
        label_text = TAG_PATTERN.sub(" ", label).strip().lower()
        if any(token in href_lower for token in ("job", "career", "position", "opening", "requisition")):
            job_link_count += 1
            continue
        if any(token in label_text for token in ("apply", "job", "position", "opening", "career")):
            job_link_count += 1

    browser_signal_count = 0
    for discovered_url in discovered_urls or []:
        lowered = discovered_url.lower()
        if any(token in lowered for token in ("job", "career", "position", "opening", "search")):
            browser_signal_count += 1

    return {
        "keyword_hits": keyword_hits,
        "pagination_hits": pagination_hits,
        "job_link_count": job_link_count,
        "browser_signal_count": browser_signal_count,
    }
