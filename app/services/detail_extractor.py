"""Job detail extraction — strategy-locked (API vs DOM).

Rules:
- If strategy == "api" → extract details from API response, NO HTML scraping
- If strategy == "dom" → use Playwright to fetch job detail pages
- NEVER mix API listing with DOM detail scraping
- WORKDAY_API: use ONLY Workday detail API endpoint, no HTML scraping
"""

import httpx
from pathlib import Path

from app.core.logger import logger
from app.detectors.workday import (
    fetch_workday_job_detail,
    normalize_external_path,
    parse_workday_config,
)

WORKDAY_HTML_DIR = Path("raw_html") / "workday"


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

    # ── Workday: use detail API endpoint ONLY ────────────────────
    if site_type == "WORKDAY_API":
        return await _workday_api_detail(job, api_url, base_url, client, result)

    # ── Try to use raw API data already attached to job ────────────
    raw = job.get("_raw_api")
    if raw and isinstance(raw, dict):
        logger.info(
            "[DETAIL STRATEGY] Using API for job_id=%s (from raw listing data)",
            raw.get("externalPath") or raw.get("id") or raw.get("reqId") or "unknown",
        )
        return _enrich_from_raw_api(result, raw, site_type)

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


# ── Workday API detail — clean, no HTML ──────────────────────────────

async def _workday_api_detail(
    job: dict,
    api_url: str,
    base_url: str,
    client: httpx.AsyncClient | None,
    result: dict,
) -> dict:
    """Fetch Workday job detail via API ONLY. No HTML, no DOM fallback."""
    external_path = job.get("external_path") or job.get("_raw_api", {}).get("externalPath", "")

    # If we already have raw detail data, use it
    raw_detail = job.get("_raw_detail")
    if raw_detail and isinstance(raw_detail, dict):
        logger.info("[WORKDAY API] Using cached detail for externalPath=%s", external_path)
        return _enrich_from_workday_detail(result, raw_detail)

    # Parse config from API URL
    config = parse_workday_config(api_url)
    if not config:
        # Try to parse from base_url as fallback
        if api_url:
            config = parse_workday_config(api_url)
        if not config:
            logger.warning("[WORKDAY API] Cannot parse config from api_url=%s", api_url)
            # Return what we have from listing
            raw_listing = job.get("_raw_api")
            if raw_listing and isinstance(raw_listing, dict):
                return _enrich_from_raw_api(result, raw_listing, "WORKDAY_API")
            return result

    close_client = client is None
    if close_client:
        client = httpx.AsyncClient(timeout=30, follow_redirects=True)

    try:
        detail = await fetch_workday_job_detail(client, config, external_path)
        if detail:
            return _enrich_from_workday_detail(result, detail)
        else:
            logger.warning("[WORKDAY API] Detail fetch returned None for %s", external_path)
    except Exception as exc:
        logger.error("[WORKDAY API] Detail fetch error: %s", exc)
    finally:
        if close_client:
            await client.aclose()

    # Fallback: return listing data + any raw API data we have
    raw_listing = job.get("_raw_api")
    if raw_listing and isinstance(raw_listing, dict):
        return _enrich_from_raw_api(result, raw_listing, "WORKDAY_API")
    return result


def _enrich_from_workday_detail(result: dict, detail: dict) -> dict:
    """Enrich result from Workday detail API JSON response."""
    job_info = detail.get("jobPostingInfo", {})

    result["title"] = job_info.get("title") or result.get("title", "")
    result["location"] = job_info.get("location") or result.get("location", "")
    result["description"] = job_info.get("jobDescription", "")
    result["job_id"] = job_info.get("jobReqId") or job_info.get("jobPostingId") or result.get("job_id", "")
    result["posted_on"] = job_info.get("postedOn") or job_info.get("startDate", "")
    result["employment_type"] = job_info.get("timeType", "")
    result["company_name"] = detail.get("hiringOrganization", {}).get("name", "")

    # Location enrichment from requisition
    req_location = job_info.get("jobRequisitionLocation", {})
    if req_location and not result.get("location"):
        desc = req_location.get("descriptor", "")
        if desc:
            result["location"] = desc

    # Country
    country = job_info.get("country", {})
    if country and not result.get("location"):
        result["location"] = country.get("descriptor", "")

    return result


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
# DOM detail extraction — INTERACTIVE_DOM max extraction (for DOM strategy)
# ---------------------------------------------------------------------------

async def _extract_dom_details(job: dict) -> dict:
    """Fetch job detail page via Playwright with MAX extraction.

    Forces INTERACTIVE_DOM for ALL non-API flows:
    - Full page load with networkidle
    - Multiple scroll rounds to trigger lazy loading
    - Expand hidden sections (more/expand/show/read buttons)
    - Wait for DOM growth before capturing HTML
    - Send FULL content to AI via prepare_ai_payload
    """
    from urllib.parse import urlparse
    from app.job_detail_engine.utils.cleaner import prepare_ai_payload
    from app.job_detail_engine.orchestrator import extract_job_details

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

    logger.info("[DETAIL STRATEGY] INTERACTIVE_DOM for job_url=%s", job_url)

    # Extract domain from job URL for payload saving
    try:
        parsed = urlparse(job_url)
        domain = parsed.netloc
    except Exception:
        domain = ""

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.error("[DETAIL STRATEGY] Playwright not available")
        return _dom_fallback(job)

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
            try:
                # ── Max extraction: scroll, expand, wait for growth ──
                await page.goto(job_url, timeout=60000)
                try:
                    await page.wait_for_load_state("networkidle")
                except Exception:
                    logger.warning("[DETAIL] networkidle timeout for %s", job_url)
                await page.wait_for_timeout(2000)

                # Scroll to trigger lazy loading
                for _ in range(5):
                    await page.mouse.wheel(0, 3000)
                    await page.wait_for_timeout(1000)

                # Expand hidden sections
                try:
                    buttons = await page.query_selector_all("button")
                    for btn in buttons:
                        try:
                            text = (await btn.inner_text() or "").lower()
                            if any(k in text for k in ["more", "expand", "show", "read"]):
                                await btn.click()
                                await page.wait_for_timeout(500)
                        except Exception:
                            continue
                except Exception as exc:
                    logger.warning("[DETAIL] Button expansion failed: %s", exc)

                # Wait for DOM growth
                prev_len = 0
                for _ in range(5):
                    html_snapshot = await page.content()
                    curr_len = len(html_snapshot)
                    if curr_len > prev_len * 1.2:
                        prev_len = curr_len
                        await page.wait_for_timeout(1000)
                    else:
                        break

                html = await page.content()
                logger.info("[DETAIL] url=%s html_length=%d", job_url, len(html))

                # ── Prepare AI payload — ONLY remove script/style/noscript ──
                payload = prepare_ai_payload(html, domain=domain)
                logger.info("[AI PAYLOAD] length=%d source=JOB_DETAIL", len(payload))

                if len(payload) < 2000:
                    logger.warning("[WEAK DETAIL PAGE] url=%s length=%d", job_url, len(payload))

                # ── Send to AI for extraction ──
                detail = await extract_job_details(payload, force_ai=True, domain=domain)
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
