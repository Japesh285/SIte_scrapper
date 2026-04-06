"""Job detail extraction — strategy-locked (API vs DOM).

Rules:
- If strategy == "api" → extract details from API response, NO HTML scraping
- If strategy == "dom" → use Playwright to fetch job detail pages
- NEVER mix API listing with DOM detail scraping
"""

import httpx
from pathlib import Path

from app.core.logger import logger

WORKDAY_HTML_DIR = Path("raw_html") / "workday"


def _save_workday_html(html: str, job_id: str, domain: str) -> str | None:
    """Save raw Workday HTML to disk for debugging/analysis."""
    try:
        from datetime import datetime

        domain_dir = WORKDAY_HTML_DIR / domain
        domain_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_id = job_id.replace("/", "_").replace("\\", "_") if job_id else "unknown"
        filename = f"{safe_id}_{timestamp}.html"
        file_path = domain_dir / filename

        with open(file_path, "w", encoding="utf-8") as f:
            f.write(html)

        logger.info("[WorkdayHTML] Saved to %s (%d bytes)", file_path, len(html))
        return str(file_path)
    except Exception as exc:
        logger.warning("[WorkdayHTML] Failed to save HTML: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def extract_job_details(
    *,
    strategy: str,
    job: dict,
    site_type: str = "",
    api_url: str = "",
    base_url: str = "",
    client: httpx.AsyncClient | None = None,
) -> dict:
    """Extract full job details respecting the locked strategy.

    Parameters
    ----------
    strategy : str
        "api" or "dom" — must match the listing strategy.
    job : dict
        The listing-level job dict (must have at least title, url).
        For API sites, should also contain the raw API response in
        ``job["_raw_api"]`` if available.
    site_type : str
        e.g. "WORKDAY_API", "GREENHOUSE_API", "SIMPLE_API", "DOM_BROWSER"
    api_url : str
        The API base URL detected during listing (used for API detail calls).
    base_url : str
        The normalized site base URL.
    client : httpx.AsyncClient | None
        Reusable HTTP client.

    Returns
    -------
    dict with enriched job fields (title, location, description, skills, …)
    """
    if strategy == "api":
        return await _extract_api_details(
            job=job,
            site_type=site_type,
            api_url=api_url,
            base_url=base_url,
            client=client,
        )
    else:
        return await _extract_dom_details(job=job)


# ---------------------------------------------------------------------------
# API detail extraction
# ---------------------------------------------------------------------------

async def _extract_api_details(
    *,
    job: dict,
    site_type: str,
    api_url: str,
    base_url: str,
    client: httpx.AsyncClient | None,
) -> dict:
    """Extract job details using ONLY API — no HTML, no BeautifulSoup."""

    result = {
        "title": job.get("title", ""),
        "location": job.get("location", ""),
        "url": job.get("url", ""),
        "job_id": "",
        "description": "",
        "skills": [],
        "experience": "",
        "education": "",
        "posted_on": "",
        "employment_type": "",
        "salary": "",
        "company_name": "",
        "remote_type": "",
        "department": "",
        "qualifications": [],
        "additional_sections": [],
    }

    # ── Try to use raw API data already attached to job ────────────
    raw = job.get("_raw_api")
    if raw and isinstance(raw, dict):
        logger.info(
            "[DETAIL STRATEGY] Using API for job_id=%s (from raw listing data)",
            raw.get("externalPath") or raw.get("id") or raw.get("reqId") or "unknown",
        )
        # Workday: also fetch HTML page (raw API lacks full description)
        if site_type == "WORKDAY_API":
            result = _enrich_from_raw_api(result, raw, site_type)
            return await _workday_html_detail(job, base_url, client, result)
        return _enrich_from_raw_api(result, raw, site_type)

    # ── Workday: fetch public HTML page (API detail endpoint broken)
    if site_type == "WORKDAY_API":
        return await _workday_html_detail(job, base_url, client, result)

    # ── Greenhouse: call detail API ───────────────────────────────
    if site_type == "GREENHOUSE_API":
        return await _greenhouse_api_detail(job, base_url, client, result)

    # ── Simple API: try to fetch individual job endpoint ──────────
    if site_type == "SIMPLE_API":
        return await _simple_api_detail(job, api_url, base_url, client, result)

    logger.warning(
        "[DETAIL STRATEGY] API strategy but no detail method for %s — returning listing data only",
        site_type,
    )
    return result


# ── Workday HTML detail (public URL, no API) ──────────────────────

def _normalize_workday_url(job_url: str) -> str:
    """Normalize Workday job URL to working public HTML format.

    Broken:  .../job/job/Location/Title_R123
    Working: .../job/Title_R123
    """
    import re
    from urllib.parse import urlparse, urlunparse

    parsed = urlparse(job_url)
    path = parsed.path

    # Pattern 1: /job/job/Location/Title_R123 → /job/Title_R123
    path = re.sub(r"(/job)/job/[^/]+/([^/]+)$", r"\1/\2", path)

    # Pattern 2: duplicate /job/ segments
    path = re.sub(r"(/job){2,}/", r"\1/", path)

    # Ensure /en-US/ locale is present (required for Workday HTML pages)
    if "/en-US/" not in path:
        # Insert after career site name
        # Pattern: /{site}/job/... → /{site}/en-US/job/...
        parts = path.strip("/").split("/")
        if len(parts) >= 3:
            # parts[0] = site, parts[1] = job, ...
            if parts[1] == "job":
                path = "/" + parts[0] + "/en-US/" + "/".join(parts[1:])

    normalized = urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))
    return normalized


def _extract_job_id_from_workday_url(url: str) -> str:
    """Extract job ID (slug) from Workday URL.

    e.g. AI-Data-Science-Engineer-II_R61966-1 → R61966-1
    """
    import re
    # Look for the final slug pattern: something like _R\d+ or _\d+
    match = re.search(r"_([A-Za-z]?[\d]+[-\w]*)$", url.rstrip("/"))
    if match:
        return match.group(0)  # Include the underscore
    # Fallback: last path segment
    return url.rstrip("/").split("/")[-1]


async def _workday_html_detail(
    job: dict,
    base_url: str,
    client: httpx.AsyncClient | None,
    result: dict,
) -> dict:
    """Fetch Workday job detail from public HTML page (no API endpoint)."""
    import re
    from bs4 import BeautifulSoup

    job_url = job.get("url", "")
    if not job_url:
        logger.warning("[WorkdayFix] No URL for job %s", job.get("title", "unknown"))
        return result

    # Normalize the URL
    normalized_url = _normalize_workday_url(job_url)
    job_id = _extract_job_id_from_workday_url(job_url)

    logger.info("[WorkdayFix] Using normalized URL: %s", normalized_url)
    logger.info("[DETAIL STRATEGY] Using DOM for job_id=%s", job_id)

    result["job_id"] = job_id
    result["title"] = result.get("title") or job.get("title", "")
    result["location"] = result.get("location") or job.get("location", "")

    # Extract domain for saving path
    domain = base_url
    if "://" in base_url:
        domain = base_url.split("://", 1)[1].split("/")[0]

    try:
        close_client = client is None
        if close_client:
            client = httpx.AsyncClient(timeout=30, follow_redirects=True)

        # Fetch with browser-like headers to avoid bot detection
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
        }
        response = await client.get(normalized_url, headers=headers)
        response.raise_for_status()
        html = response.text

        # Save raw HTML for debugging/analysis
        _save_workday_html(html, job_id, domain)

        # Check for "Sign In" page — if found, URL normalization failed
        if "Sign In" in html and "sign-in" in html.lower():
            logger.warning("[WorkdayFix] Got sign-in page for %s", normalized_url)
            if close_client:
                await client.aclose()
            return result

        if close_client:
            await client.aclose()

        # ── Pass HTML to AI detail engine (same as DOM mode) ──
        from app.job_detail_engine.orchestrator import extract_job_details
        # Pass site_type to enable Workday full context mode
        ai_detail = await extract_job_details(html, force_ai=True, site_type="WORKDAY_API")
        ai_detail.pop("_meta", None)
        ai_usage = ai_detail.pop("ai_usage", {})

        # Merge: AI result takes priority, listing data fills gaps
        for key in ("title", "description", "experience", "education", "salary",
                     "posted_on", "employment_type", "company_name", "remote_type",
                     "qualifications", "skills", "required_skills", "additional_sections"):
            if ai_detail.get(key):
                result[key] = ai_detail[key]

        result["job_id"] = ai_detail.get("job_id") or result.get("job_id")
        result["location"] = ai_detail.get("location") or result.get("location")

        # Attach AI usage so the route can log it
        result["ai_usage"] = ai_usage
        logger.info("[AI] Workday tokens — input=%d, output=%d, total=%d",
                    ai_usage.get("input_tokens", 0),
                    ai_usage.get("output_tokens", 0),
                    ai_usage.get("total_tokens", 0))

    except httpx.HTTPError as exc:
        logger.warning("[WorkdayFix] HTTP fetch failed for %s: %s", normalized_url, exc)
        result = await _workday_html_detail_fallback(job, base_url, result)
    except Exception as exc:
        logger.warning("[WorkdayFix] HTML/AI parse failed for %s: %s", normalized_url, exc)

    return result


async def _workday_html_detail_fallback(
    job: dict,
    base_url: str,
    result: dict,
) -> dict:
    """Fallback URL format for Workday jobs."""
    from urllib.parse import urlparse

    job_url = job.get("url", "")
    parsed = urlparse(job_url)
    path_parts = [p for p in parsed.path.split("/") if p]

    if len(path_parts) >= 3:
        # Alternative: /{site}/job/{location}/{slug}
        site = path_parts[0]
        # Try with location in path
        alt_url = f"{parsed.scheme}://{parsed.netloc}/{site}/job/{'/'.join(path_parts[1:])}"
        logger.info("[WorkdayFix] Trying fallback URL: %s", alt_url)

    # Return listing data as-is if fallback also fails
    return result


def _extract_workday_description(soup) -> str:
    """Extract job description text from Workday page."""
    import re

    # Try data-automation tags first
    desc_el = soup.find({"data-automation": re.compile(r"job.*description", re.I)})
    if desc_el:
        text = desc_el.get_text(separator="\n", strip=True)
        if text and len(text) > 50:
            return text

    # Try finding the largest text block in the main content area
    main = soup.find("main") or soup.find("article") or soup.find("div", {"role": "main"})
    if main:
        paragraphs = main.find_all("p")
        if paragraphs:
            text = "\n".join(p.get_text(strip=True) for p in paragraphs if p.get_text(strip=True))
            if len(text) > 100:
                return text

    # Fallback: get all text (cleaned)
    text = soup.get_text(separator="\n", strip=True)
    # Remove very short lines (likely navigation/menu items)
    lines = [line for line in text.split("\n") if len(line.strip()) > 10]
    return "\n".join(lines[:100])  # Cap to avoid huge strings


# ── Greenhouse API detail ──────────────────────────────────────────

async def _greenhouse_api_detail(
    job: dict,
    base_url: str,
    client: httpx.AsyncClient | None,
    result: dict,
) -> dict:
    """Greenhouse listing API already returns full job data — use it."""
    raw = job.get("_raw_api")
    if raw and isinstance(raw, dict):
        result = _enrich_from_raw_api(result, raw, "GREENHOUSE_API")
    return result


# ── Simple API detail ──────────────────────────────────────────────

async def _simple_api_detail(
    job: dict,
    api_url: str,
    base_url: str,
    client: httpx.AsyncClient | None,
    result: dict,
) -> dict:
    """Try to get detail from the same API or a derived endpoint."""
    raw = job.get("_raw_api")
    if raw and isinstance(raw, dict):
        result = _enrich_from_raw_api(result, raw, "SIMPLE_API")
    return result


# ── Enrich from raw API response ───────────────────────────────────

def _enrich_from_raw_api(result: dict, raw: dict, site_type: str) -> dict:
    """Populate result dict from the raw API job object."""

    logger.info("[DETAIL STRATEGY] Using API for job_id=%s", raw.get("externalPath") or raw.get("id") or raw.get("reqId") or "unknown")

    # Core fields — try multiple key names
    result["title"] = result.get("title") or _s(raw, "title", "job_title", "name")
    result["location"] = result.get("location") or _s(raw, "location", "locationsText", "city", "country")
    result["job_id"] = result.get("job_id") or _s(raw, "externalPath", "id", "reqId", "requisition_id", "job_id")
    result["company_name"] = result.get("company_name") or _s(raw, "company", "company_name", "organization")
    result["department"] = _s(raw, "department", "job_family", "category", "team")
    result["employment_type"] = _s(raw, "employment_type", "employmentType", "job_type", "workType")
    result["posted_on"] = _s(raw, "posted_date", "posted_on", "created_at", "date_posted", "publish_date")
    result["salary"] = _s(raw, "salary", "compensation", "pay_rate", "salary_text")

    # Location enrichment
    if not result.get("location"):
        loc_parts = [
            _s(raw, "city"), _s(raw, "state"), _s(raw, "country"),
            _s(raw, "addressLocality"), _s(raw, "addressRegion"), _s(raw, "addressCountry"),
        ]
        loc_str = ", ".join(p for p in loc_parts if p)
        if loc_str:
            result["location"] = loc_str

    # Remote type
    remote = _s(raw, "remote_type", "remote", "workplaceType", "workplace_type", "work_model")
    if remote:
        result["remote_type"] = remote

    # Description — try multiple keys
    result["description"] = _s(
        raw, "description", "job_description", "jobDescription",
        "content", "summary", "detail", "overview",
    )

    # Experience
    result["experience"] = _s(
        raw, "experience", "years_of_experience", "min_experience",
        "required_experience", "experience_level",
    )

    # Education
    result["education"] = _s(
        raw, "education", "degree", "education_required",
        "educational_requirements", "minimum_education",
    )

    # Skills — try array fields
    skills = _extract_skills_from_raw(raw)
    if skills:
        result["skills"] = skills

    # Qualifications
    quals = _extract_list_field(raw, "qualifications", "requirements", "required_qualifications",
                               "minimum_qualifications", "preferred_qualifications")
    if quals:
        result["qualifications"] = quals

    # Additional metadata sections
    additional = _build_additional_sections(raw)
    if additional:
        result["additional_sections"] = additional

    return result


def _extract_skills_from_raw(raw: dict) -> list[str]:
    """Extract skills from various API field names."""
    skill_fields = [
        "skills", "required_skills", "skill_set", "technologies",
        "tools", "tech_stack", "competencies", "key_skills",
    ]
    for field in skill_fields:
        val = raw.get(field)
        if isinstance(val, list):
            items = [str(v).strip() for v in val if str(v).strip()]
            if items:
                return items[:15]
        elif isinstance(val, str) and val.strip():
            # Comma-separated skills
            return [s.strip() for s in val.split(",") if s.strip()][:15]

    # Try to extract from description bullet fields
    bullet = raw.get("bulletFields")
    if isinstance(bullet, dict):
        for key in ("skills", "qualifications", "requirements"):
            val = bullet.get(key)
            if isinstance(val, str) and val.strip():
                return [s.strip() for s in val.split(";") if s.strip()][:15]
            if isinstance(val, list):
                return [str(v).strip() for v in val if str(v).strip()][:15]

    return []


def _extract_list_field(raw: dict, *keys: str) -> list[str]:
    """Extract a list field from raw API data."""
    for key in keys:
        val = raw.get(key)
        if isinstance(val, list):
            return [str(v).strip() for v in val if str(v).strip()][:15]
        if isinstance(val, str) and val.strip():
            return [val.strip()]
    return []


def _build_additional_sections(raw: dict) -> list[dict]:
    """Build short additional_sections from remaining API fields."""
    sections = []
    skip_keys = {
        "title", "location", "url", "description", "skills", "experience",
        "education", "qualifications", "employment_type", "salary",
        "posted_on", "company", "company_name", "department",
        "remote_type", "externalPath", "id", "reqId", "requisition_id",
        "job_id", "bulletFields", "subNavLinks",
    }
    for key, val in raw.items():
        if key in skip_keys:
            continue
        if isinstance(val, str) and 5 < len(val) < 500:
            sections.append({"section_title": key, "content": val})
        elif isinstance(val, (int, float, bool)):
            sections.append({"section_title": key, "content": str(val)})
    return sections[:5]


# ---------------------------------------------------------------------------
# DOM detail extraction (uses Playwright — for DOM strategy only)
# ---------------------------------------------------------------------------

async def _extract_dom_details(job: dict) -> dict:
    """Fetch job detail page via Playwright and extract content.

    This is ONLY used when strategy == "dom".
    """
    job_url = job.get("url", "")
    if not job_url:
        logger.warning("[DETAIL STRATEGY] No URL for DOM extraction")
        return {
            "title": job.get("title", ""),
            "location": job.get("location", ""),
            "url": job_url,
            "job_id": "",
            "description": "",
            "skills": [],
            "experience": "",
            "education": "",
            "posted_on": "",
            "employment_type": "",
            "salary": "",
            "company_name": "",
            "remote_type": "",
            "department": "",
            "qualifications": [],
            "additional_sections": [],
        }

    logger.info("[DETAIL STRATEGY] Using DOM for job_url=%s", job_url)

    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page()
            try:
                await page.goto(job_url, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(3000)
                html = await page.content()

                # Pass to the existing detail engine
                from app.job_detail_engine.orchestrator import extract_job_details
                detail = await extract_job_details(html, force_ai=True)
                detail.pop("_meta", None)
                detail.pop("ai_usage", None)

                # Merge with listing data
                result = {
                    "title": detail.get("title") or job.get("title", ""),
                    "location": detail.get("location") or job.get("location", ""),
                    "url": job_url,
                    "job_id": detail.get("job_id", ""),
                    "description": detail.get("description", ""),
                    "skills": detail.get("skills", detail.get("required_skills", [])),
                    "experience": detail.get("experience", ""),
                    "education": detail.get("education", ""),
                    "posted_on": detail.get("posted_on", ""),
                    "employment_type": detail.get("employment_type", ""),
                    "salary": detail.get("salary", ""),
                    "company_name": detail.get("company_name", ""),
                    "remote_type": detail.get("remote_type", ""),
                    "department": "",
                    "qualifications": detail.get("qualifications", []),
                    "additional_sections": detail.get("additional_sections", []),
                }
                return result
            finally:
                await page.close()
                await browser.close()
    except ImportError:
        logger.error("[DETAIL STRATEGY] Playwright not available for DOM extraction")
        return _dom_fallback(job)
    except Exception as exc:
        logger.error("[DETAIL STRATEGY] DOM extraction failed for %s: %s", job_url, exc)
        return _dom_fallback(job)


def _dom_fallback(job: dict) -> dict:
    """Return partial data when DOM extraction fails — DO NOT fallback to API."""
    return {
        "title": job.get("title", ""),
        "location": job.get("location", ""),
        "url": job.get("url", ""),
        "job_id": "",
        "description": "",
        "skills": [],
        "experience": "",
        "education": "",
        "posted_on": "",
        "employment_type": "",
        "salary": "",
        "company_name": "",
        "remote_type": "",
        "department": "",
        "qualifications": [],
        "additional_sections": [],
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _s(d: dict, *keys: str) -> str:
    """Get first non-empty string value from dict."""
    for key in keys:
        val = d.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
        if isinstance(val, dict):
            # e.g. {"value": "foo"}
            nested = val.get("value") or val.get("text")
            if isinstance(nested, str) and nested.strip():
                return nested.strip()
    return ""
