"""Detector and scraper for WordPress sites using the job_box career listing pattern."""

import re
from urllib.parse import urlparse, urlencode, parse_qs, urljoin

import httpx
from bs4 import BeautifulSoup

from app.core.site_utils import normalize_site_url
from app.core.logger import logger

# WordPress career page markers
WP_JOBS_HTML_PATTERNS = [
    re.compile(r'class=["\'][^"\']*job_box[^"\']*["\']', re.IGNORECASE),
    re.compile(r'career_listing_block', re.IGNORECASE),
    re.compile(r'career_open_roles', re.IGNORECASE),
]

# Max pages to paginate through
MAX_PAGES = 100


def _is_wp_jobs_page(html: str) -> bool:
    return any(p.search(html) for p in WP_JOBS_HTML_PATTERNS)


def _extract_jobs_from_html(html: str, base_url: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    jobs = []
    seen: set[str] = set()

    for a in soup.find_all("a", class_="job_box"):
        href = a.get("href", "").strip()
        if not href:
            continue

        title_tag = a.find("h4") or a.find("h3") or a.find("h2")
        title = title_tag.get_text(strip=True) if title_tag else a.get_text(separator=" ", strip=True)
        if not title:
            continue

        locations = [s.get_text(strip=True) for s in a.find_all("span", class_="job_location")]
        location = ", ".join(loc for loc in locations if loc) if locations else ""

        key = href
        if key in seen:
            continue
        seen.add(key)
        jobs.append({"title": title, "location": location, "url": href})

    return jobs


def _total_count_from_html(html: str) -> int:
    soup = BeautifulSoup(html, "html.parser")
    tag = soup.find(class_="career_open_roles_tag")
    if tag:
        nums = re.findall(r"\d+", tag.get_text())
        if nums:
            return int(nums[0])
    return 0


def _base_listing_url(url: str) -> str:
    """Strip query params except location/keywords filter params that are part of the listing."""
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    # Keep filter params that affect which jobs show, drop pagination
    keep = {k: v for k, v in qs.items() if k not in ("page", "paged")}
    base = parsed._replace(query=urlencode(keep, doseq=True)).geturl()
    return base.rstrip("/")


async def detect_wp_jobs(
    url: str,
    client: httpx.AsyncClient | None = None,
    html: str = "",
) -> dict:
    if not html or not _is_wp_jobs_page(html):
        return {"matched": False, "jobs_found": 0, "api_usable": False, "api_url": ""}

    jobs = _extract_jobs_from_html(html, url)
    total = _total_count_from_html(html)
    if not jobs and total == 0:
        return {"matched": True, "jobs_found": 0, "api_usable": False, "api_url": ""}

    api_url = _base_listing_url(normalize_site_url(url))
    logger.info("[WP_JOBS] Detected: %d jobs on page, %d total at %s", len(jobs), total, api_url)
    return {
        "matched": True,
        "jobs_found": total or len(jobs),
        "api_usable": True,
        "api_url": api_url,
    }


async def fetch_wp_jobs(
    client: httpx.AsyncClient,
    base_url: str,
    original_url: str = "",
) -> list[dict]:
    """Paginate through all WordPress paged= pages and collect job_box entries."""
    all_jobs: list[dict] = []
    seen: set[str] = set()

    def _add(jobs: list[dict]) -> int:
        added = 0
        for j in jobs:
            key = j.get("url", "")
            if key not in seen:
                seen.add(key)
                all_jobs.append(j)
                added += 1
        return added

    for page_num in range(1, MAX_PAGES + 1):
        sep = "&" if "?" in base_url else "?"
        page_url = f"{base_url}{sep}paged={page_num}" if page_num > 1 else base_url
        try:
            resp = await client.get(page_url, headers={"User-Agent": "Mozilla/5.0"})
            if resp.status_code != 200:
                break
            html = resp.text
        except Exception as exc:
            logger.debug("[WP_JOBS] Fetch failed at page %d: %s", page_num, exc)
            break

        jobs = _extract_jobs_from_html(html, base_url)
        added = _add(jobs)
        logger.info("[WP_JOBS] Page %d: %d new jobs (total=%d)", page_num, added, len(all_jobs))

        if added == 0:
            break

        # Stop if we've collected at least as many as the total shown on page 1
        total = _total_count_from_html(html)
        if total and len(all_jobs) >= total:
            break

    return all_jobs
