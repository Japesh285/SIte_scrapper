import asyncio
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, urlunparse
from uuid import uuid4

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel

from app.core.site_utils import normalize_site_url, get_domain
from app.db.database import async_session, get_session
from app.db.models import Site
from app.services.orchestrator import orchestrate_scrape
from app.services.raw_json_saver import RAW_JSON_DIR
from app.job_detail_engine.orchestrator import extract_job_details
from app.services.test_scrape import run_test_scrape
from app.core.logger import logger
from app.scrapers.accenture import (
    ACCENTURE_SITE_TYPE,
    AccentureRequestManager,
    AccentureThrottle,
)

# ── Job URL validation ─────────────────────────────────────────────

# URL suffixes/patterns that are NEVER job detail pages
_JOB_REJECT_SUFFIXES = [
    "/career-search",
    "/career_search",
    "/career-journeys",
    "/page/",
    "/job-alert",
    "/saved-jobs",
    "/apply-form",
]

# Content/marketing path prefixes — reject if URL starts with these
_CONTENT_PATH_PREFIXES = [
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
    "/key-projects",
    "/benefits",
    "/culture",
    "/perks",
    "/how-we-hire",
    "/eeo",
]

# Navigation pages that should be rejected even under /careers/
_CAREERS_NAVIGATION = [
    "/careers/",          # index page only (checked separately)
    "/careers/why-",
    "/careers/key-projects",
    "/careers/benefits",
    "/careers/culture",
    "/careers/how-we-hire",
    "/careers/learning",
]

# Patterns that indicate a REAL job detail page
_JOB_ACCEPT_SIGNALS = [
    "/job/",           # /jobs/python-dev-123/
    "/jobs/",          # /jobs/detail/123
    "/position/",
    "/opening/",
    "/requisition/",
    "/vacancy/",
    "jobid",
    "job_id",
    "jobId",
    "reqid",
    "req_id",
    "reqId",
]

# Regex for job IDs in URLs (e.g. IRC291384, REQ-123, JOB-001)
_JOB_ID_PATTERN = re.compile(r"(?:IRC|REQ|JOB|POS)[\d-]+|/[\w-]*\d{4,}[\w-]*/?$", re.IGNORECASE)


def is_valid_job_url(url: str) -> bool:
    """Strict validation: reject navigation/content pages, accept only job detail links.

    Rules:
    - Reject known non-job suffix patterns
    - Reject content/marketing path prefixes
    - Accept if URL has job ID pattern or job-related path segment
    - Reject index pages like /careers/ (single-segment paths)
    """
    if not url:
        return False

    lowered = url.lower()
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    path_parts = [p for p in path.strip("/").split("/") if p]

    # Hard reject: known non-job suffix patterns
    for pattern in _JOB_REJECT_SUFFIXES:
        if pattern in lowered:
            return False

    # Hard reject: content/marketing path prefixes (anywhere in path)
    for prefix in _CONTENT_PATH_PREFIXES:
        if prefix in lowered:
            return False

    # Reject /careers/ navigation sub-pages
    for nav_path in _CAREERS_NAVIGATION:
        if nav_path in lowered:
            # But allow /careers/<job-slug>/ — check if it has a job ID
            if nav_path == "/careers/" and len(path_parts) >= 2:
                # Could be /careers/python-genai-irc291384/ — check for job ID
                if _JOB_ID_PATTERN.search(url):
                    return True
                # If no job ID but deep path, still might be valid
                if len(path_parts) >= 3:
                    return True
            return False

    # Accept: URL has a job-related path segment
    for signal in _JOB_ACCEPT_SIGNALS:
        if signal in lowered:
            return True

    # Accept: URL contains a job ID pattern
    if _JOB_ID_PATTERN.search(url):
        return True

    # Reject: path is too short (likely an index page)
    if len(path_parts) <= 1:
        return False

    # Fallback: require at least "job" or "career" in the URL
    if "job" in lowered or "career" in lowered:
        # But only if path is deep enough to be a detail page
        if len(path_parts) >= 2:
            return True

    return False

router = APIRouter()
_BATCH_JOBS: dict[str, dict] = {}


class ScrapeRequest(BaseModel):
    url: str


class ScrapeResponse(BaseModel):
    domain: str
    type: str
    confidence: float
    jobs_found: int
    status: str


class BulkScrapeResponse(BaseModel):
    total_sites: int
    successful: int
    failed: int
    skipped: int
    results: list[ScrapeResponse]


class JobDetailResponse(BaseModel):
    id: str = ""
    title: str = ""
    company_name: str = ""
    job_link: str = ""
    experience: str = ""
    locations: list = []
    educational_qualifications: str = ""
    required_skill_set: list = []
    remote_type: str = ""
    posted_on: str = ""
    job_id: str = ""
    salary: str = ""
    is_active: bool = True
    first_seen: str = ""
    last_seen: str = ""
    job_summary: str = ""
    key_responsibilities: list = []
    additional_sections: list = []
    about_us: str = ""
    Scrap_json: dict = {}


@router.post("/scrape", response_model=ScrapeResponse)
async def scrape(request: ScrapeRequest, session: AsyncSession = Depends(get_session)):
    result = await orchestrate_scrape(request.url, session)
    return ScrapeResponse(
        domain=result["domain"],
        type=result["type"],
        confidence=result["confidence"],
        jobs_found=result["jobs_found"],
        status=result["status"],
    )


class JobDetailResult(BaseModel):
    id: str = ""
    title: str = ""
    company_name: str = ""
    job_link: str = ""
    experience: str = ""
    locations: list = []
    educational_qualifications: str = ""
    required_skill_set: list = []
    remote_type: str = ""
    posted_on: str = ""
    job_id: str = ""
    salary: str = ""
    is_active: bool = True
    first_seen: str = ""
    last_seen: str = ""
    job_summary: str = ""
    key_responsibilities: list = []
    additional_sections: list = []
    about_us: str = ""
    Scrap_json: dict = {}


def _normalize_locations_field(detail: dict) -> list:
    locations = detail.get("locations")
    if isinstance(locations, list):
        return locations

    location = detail.get("location")
    if isinstance(location, list):
        return location
    if isinstance(location, str) and location:
        return [location]
    if isinstance(locations, str) and locations:
        return [locations]
    return []


def _build_job_detail_result(detail: dict, job_url: str, scrap_json: dict) -> JobDetailResult:
    return JobDetailResult(
        id=str(detail.get("job_id") or detail.get("id") or job_url.split("/")[-1]),
        title=str(detail.get("title") or ""),
        company_name=str(detail.get("company_name") or ""),
        job_link=str(detail.get("job_link") or job_url),
        experience=str(detail.get("experience") or ""),
        locations=_normalize_locations_field(detail),
        educational_qualifications=str(
            detail.get("educational_qualifications")
            or detail.get("education")
            or detail.get("qualifications", [])
        ),
        required_skill_set=(
            detail.get("required_skill_set")
            or detail.get("required_skills")
            or detail.get("skills")
            or []
        ),
        remote_type=str(detail.get("remote_type") or detail.get("employment_type") or ""),
        posted_on=str(detail.get("posted_on") or ""),
        job_id=str(detail.get("job_id") or detail.get("id") or ""),
        salary=str(detail.get("salary") or ""),
        is_active=bool(detail.get("is_active", True)),
        first_seen=str(detail.get("first_seen") or ""),
        last_seen=str(detail.get("last_seen") or ""),
        job_summary=str(detail.get("job_summary") or detail.get("description") or ""),
        key_responsibilities=detail.get("key_responsibilities") or [],
        additional_sections=detail.get("additional_sections") or [],
        about_us=str(detail.get("about_us") or detail.get("about_company") or ""),
        Scrap_json=scrap_json or detail.get("Scrap_json") or {},
    )


async def _extract_accenture_jobs_detail(
    jobs_raw: list[dict],
    domain: str,
    log_prefix: str,
) -> tuple[list[JobDetailResult], int]:
    from app.job_detail_engine.orchestrator import extract_job_details as extract_dom_details

    try:
        from playwright.async_api import async_playwright
    except Exception:
        logger.warning("%s Playwright unavailable for Accenture detail extraction", log_prefix)
        return [], 0

    jobs_detail: list[JobDetailResult] = []
    total_ai_tokens = 0

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
        manager = AccentureRequestManager(browser, throttle=AccentureThrottle())

        try:
            for i, job_data in enumerate(jobs_raw):
                job_url = str(job_data.get("url", "")).strip()
                if not job_url:
                    continue

                logger.info("%s %s ACCENTURE job %d/%d: %s", log_prefix, domain, i + 1, len(jobs_raw), job_url)
                meta = {}
                detail = {}
                ai_usage = {}

                try:
                    html = await manager.fetch_page(job_url, phase="job details", scroll_rounds=4)
                    if len(html) < 1500:
                        logger.info("%s [ACCENTURE] Weak page skipped: %s (length=%d)", log_prefix, job_url, len(html))
                        continue

                    detail = await extract_dom_details(html, force_ai=True, domain=domain)
                    meta = detail.pop("_meta", {})
                    ai_usage = detail.pop("ai_usage", {})
                    total_ai_tokens += ai_usage.get("total_tokens", 0)
                    logger.info(
                        "%s [Engine] %s job %d → title=%r, parser=%s, score=%s, ai_used=%s, ai_forced=%s, skills=%d, ai_tokens=%d",
                        log_prefix,
                        domain,
                        i + 1,
                        detail.get("title", ""),
                        meta.get("parser_used", "?"),
                        meta.get("confidence", 0),
                        meta.get("ai_used", False),
                        meta.get("ai_forced", False),
                        len(detail.get("skills", [])),
                        ai_usage.get("total_tokens", 0),
                    )
                except Exception as exc:
                    logger.error("%s Accenture detail extraction failed for %s: %s", log_prefix, job_url, exc)
                    continue

                jobs_detail.append(
                    _build_job_detail_result(
                        detail,
                        job_url,
                        {
                            "url": job_url,
                            "strategy": "dom",
                            "site_type": ACCENTURE_SITE_TYPE,
                            "parser_used": str(meta.get("parser_used", "")),
                            "confidence": meta.get("confidence", 0),
                            "ai_forced": meta.get("ai_forced", False),
                            "preferred_skills": detail.get("preferred_skills") or [],
                            "tools_and_technologies": detail.get("tools_and_technologies") or [],
                            "certifications": detail.get("certifications") or [],
                            "soft_skills": detail.get("soft_skills") or [],
                            "inferred_skills": detail.get("inferred_skills") or [],
                            "benefits": detail.get("benefits") or [],
                        },
                    )
                )
        finally:
            await manager.close()
            await browser.close()

    return jobs_detail, total_ai_tokens


class ScrapeDetailsResponse(BaseModel):
    domain: str
    site_type: str
    listing_jobs_found: int
    listing_status: str
    jobs_count: int
    jobs: list[JobDetailResult]


@router.post("/scrape-details", response_model=ScrapeDetailsResponse)
async def scrape_details(request: ScrapeRequest, session: AsyncSession = Depends(get_session)):
    """Scrape a single site for job listings, then extract details using strategy-locked approach."""
    normalized_url = normalize_site_url(request.url)
    domain = get_domain(normalized_url)
    logger.info(f"[ScrapeDetails] Starting: {domain}")

    # Step 1: Get listing URLs via orchestrator (includes strategy lock)
    scrape_result = await orchestrate_scrape(normalized_url, session)
    site_type = scrape_result["type"]
    strategy = scrape_result.get("strategy", "dom")
    api_url = scrape_result.get("api_url", "")
    logger.info(f"[ScrapeDetails] {domain} -> {site_type}, strategy={strategy}, jobs={scrape_result['jobs_found']}")

    # Step 2: Read jobs from latest saved JSON (with raw API data if API strategy)
    domain_dir = RAW_JSON_DIR / domain
    if not domain_dir.exists():
        return ScrapeDetailsResponse(
            domain=domain,
            site_type=site_type,
            listing_jobs_found=scrape_result["jobs_found"],
            listing_status=scrape_result["status"],
            jobs_count=0,
            jobs=[],
        )

    files = sorted(domain_dir.glob("scrape_result_*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
    if not files:
        return ScrapeDetailsResponse(
            domain=domain,
            site_type=site_type,
            listing_jobs_found=0,
            listing_status=scrape_result["status"],
            jobs_count=0,
            jobs=[],
        )

    with open(files[0], "r", encoding="utf-8") as f:
        data = json.load(f)
        jobs_raw = data.get("jobs", [])

    logger.info(f"[ScrapeDetails] {domain} read {len(jobs_raw)} jobs from {files[0].name}")

    # Cap to 20 jobs for this endpoint
    jobs_raw = jobs_raw[:20]

    logger.info(f"[ScrapeDetails] {domain} processing {len(jobs_raw)} jobs for detail extraction")
    logger.info("[DETAIL STRATEGY] strategy=%s for %s", strategy, domain)

    # Step 3: Extract details using strategy-locked approach
    jobs_detail = []
    total_ai_tokens = 0

    if site_type == ACCENTURE_SITE_TYPE:
        jobs_detail, total_ai_tokens = await _extract_accenture_jobs_detail(
            jobs_raw,
            domain,
            "[ScrapeDetails]",
        )

    elif strategy == "api":
        # ── API STRATEGY: Extract details from raw API data, NO HTML ──
        from app.services.detail_extractor import extract_job_details as extract_api_details

        for i, job_data in enumerate(jobs_raw):
            job_url = job_data.get("url", "")
            logger.info(f"[ScrapeDetails] {domain} API detail extraction job {i+1}/{len(jobs_raw)}")
            logger.info("[DETAIL STRATEGY] Using API for job_id=%s", job_data.get("_raw_api", {}).get("externalPath", "unknown"))
            if site_type == "WORKDAY_API":
                logger.info("[WORKDAY API] Using base_url=%s", api_url)

            try:
                detail = await extract_api_details(
                    strategy="api",
                    job=job_data,
                    site_type=site_type,
                    api_url=api_url,
                    base_url=normalized_url,
                )
            except Exception as exc:
                logger.error(f"[ScrapeDetails] API detail failed for {job_url}: {exc}")
                # Failsafe: return partial data, DO NOT fallback to HTML
                detail = {
                    "title": job_data.get("title", ""),
                    "location": job_data.get("location", ""),
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
                    "qualifications": [],
                    "additional_sections": [],
                }

            job_entry = _build_job_detail_result(
                detail,
                job_url,
                {
                    "url": job_url,
                    "strategy": "api",
                    "site_type": site_type,
                    "department": detail.get("department", ""),
                    "qualifications": detail.get("qualifications", []),
                },
            )
            jobs_detail.append(job_entry)

    else:
        # ── DOM STRATEGY: Use standard detail extraction (NO browser) ──
        # Only interactive_dom if explicitly selected by pipeline
        from app.job_detail_engine.orchestrator import extract_job_details as extract_dom_details

        if site_type == "INTERACTIVE_DOM":
            # Interactive DOM was explicitly selected — use browser
            from playwright.async_api import async_playwright
            from app.job_detail_engine.utils.cleaner import prepare_ai_payload

            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True)

                for i, job_data in enumerate(jobs_raw):
                    job_url = job_data.get("url", "")
                    logger.info(f"[ScrapeDetails] {domain} INTERACTIVE_DOM job {i+1}/{len(jobs_raw)}: {job_url}")
                    page = await browser.new_page()
                    meta = {}
                    ai_usage = {}
                    detail = {}
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
                        detail = await extract_dom_details(payload, force_ai=True, domain=domain)
                        meta = detail.pop("_meta", {})
                        ai_usage = detail.pop("ai_usage", {})
                        total_ai_tokens += ai_usage.get("total_tokens", 0)
                        logger.info(
                            f"[Engine] {domain} job {i+1} → title={detail.get('title','')!r}, "
                            f"parser={meta.get('parser_used','?')}, "
                            f"score={meta.get('confidence',0)}, "
                            f"ai_used={meta.get('ai_used', False)}, "
                            f"ai_forced={meta.get('ai_forced', False)}, "
                            f"skills={len(detail.get('skills',[]))}, "
                            f"ai_tokens={ai_usage.get('total_tokens', 0)}"
                        )
                    except Exception as exc:
                        logger.error(f"[ScrapeDetails] Failed for {job_url}: {exc}")
                        meta = {}
                        ai_usage = {}
                        detail = {}
                    finally:
                        await page.close()

                    job_entry = _build_job_detail_result(
                        detail,
                        job_url,
                        {
                            "url": job_url,
                            "strategy": "INTERACTIVE_DOM",
                            "parser_used": str(meta.get("parser_used", "")),
                            "confidence": meta.get("confidence", 0),
                            "ai_forced": meta.get("ai_forced", False),
                            "preferred_skills": detail.get("preferred_skills") or [],
                            "tools_and_technologies": detail.get("tools_and_technologies") or [],
                            "certifications": detail.get("certifications") or [],
                            "soft_skills": detail.get("soft_skills") or [],
                            "inferred_skills": detail.get("inferred_skills") or [],
                            "benefits": detail.get("benefits") or [],
                        },
                    )
                    jobs_detail.append(job_entry)

                await browser.close()
        else:
            # ── Standard DOM strategy: no browser needed ──
            # Deduplicate and normalize URLs before processing
            from urllib.parse import urlparse, urlunparse, parse_qs

            seen_detail_urls: set[str] = set()
            normalized_jobs: list[dict] = []
            for job_data in jobs_raw:
                job_url = job_data.get("url", "")
                if not job_url:
                    continue
                # Normalize: strip query params, trailing slash
                parsed = urlparse(job_url)
                normalized_path = parsed.path.rstrip("/")
                clean_url = urlunparse((parsed.scheme, parsed.netloc, normalized_path, "", "", ""))
                if clean_url in seen_detail_urls:
                    logger.info("[DEDUP] Skipping duplicate: %s", job_url)
                    continue
                seen_detail_urls.add(clean_url)
                normalized_jobs.append({**job_data, "_clean_url": clean_url})

            for i, job_data in enumerate(normalized_jobs):
                job_url = job_data.get("url", "")
                clean_url = job_data.get("_clean_url", job_url)

                # ── Strict URL filtering ──
                lowered = job_url.lower()
                reject_parts = [
                    "/careers/", "/career-search", "/why-", "/about",
                    "/learning", "/projects", "/contact", "/our-team",
                    "/testimonials", "/events", "/blog", "/news",
                    "/insights", "/resources", "/webinars",
                ]
                if any(p in lowered for p in reject_parts):
                    logger.info("[FILTER] Rejected non-job URL: %s", job_url)
                    continue

                logger.info(f"[ScrapeDetails] {domain} DOM detail extraction job {i+1}/{len(normalized_jobs)}: {job_url}")

                # Fetch the detail page HTML via httpx
                try:
                    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                        resp = await client.get(job_url)
                        resp.raise_for_status()
                        html = resp.text
                except Exception as exc:
                    logger.error(f"[ScrapeDetails] Failed to fetch {job_url}: {exc}")
                    continue

                # ── Page quality filter ──
                html_len = len(html)
                if html_len < 1500:
                    logger.info("[SKIP AI] Weak page: %s (length=%d < 1500)", job_url, html_len)
                    continue

                # Check for title
                import re
                title_match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
                page_title = title_match.group(1).strip() if title_match else ""
                if not page_title:
                    logger.info("[SKIP AI] Weak page: %s (no title)", job_url)
                    continue

                try:
                    detail = await extract_dom_details(html, force_ai=True, domain=domain)
                    meta = detail.pop("_meta", {})
                    ai_usage = detail.pop("ai_usage", {})

                    # ── Score-based AI skip ──
                    conf_score = meta.get("confidence", 0)
                    if conf_score < 3:
                        logger.info("[SKIP AI] Low confidence score=%d for %s", conf_score, job_url)
                        # Still keep the job if parser extracted a title
                        if not detail.get("title"):
                            continue

                    total_ai_tokens += ai_usage.get("total_tokens", 0)
                    logger.info(
                        f"[Engine] {domain} job {i+1} → title={detail.get('title','')!r}, "
                        f"parser={meta.get('parser_used','?')}, "
                        f"score={meta.get('confidence',0)}, "
                        f"ai_used={meta.get('ai_used', False)}, "
                        f"ai_forced={meta.get('ai_forced', False)}, "
                        f"skills={len(detail.get('skills',[]))}, "
                        f"ai_tokens={ai_usage.get('total_tokens', 0)}"
                    )
                except Exception as exc:
                    logger.error(f"[ScrapeDetails] Detail extraction failed for {job_url}: {exc}")
                    continue

                job_entry = _build_job_detail_result(
                    detail,
                    job_url,
                    {
                        "url": job_url,
                        "strategy": "dom",
                        "parser_used": str(meta.get("parser_used", "")),
                        "confidence": meta.get("confidence", 0),
                        "ai_forced": meta.get("ai_forced", False),
                        "preferred_skills": detail.get("preferred_skills") or [],
                        "tools_and_technologies": detail.get("tools_and_technologies") or [],
                        "certifications": detail.get("certifications") or [],
                        "soft_skills": detail.get("soft_skills") or [],
                        "inferred_skills": detail.get("inferred_skills") or [],
                        "benefits": detail.get("benefits") or [],
                    },
                )
                jobs_detail.append(job_entry)

    logger.info(
        f"[ScrapeDetails] {domain} complete → {len(jobs_detail)} jobs detailed, "
        f"strategy={strategy}, total AI tokens={total_ai_tokens}"
    )

    # Step 4: Save full JSON output to job-details/{domain}/
    from app.job_detail_engine.utils.json_saver import save_job_details
    saved_path = save_job_details(
        jobs=[j.model_dump() for j in jobs_detail],
        domain=domain,
        site_type=site_type,
        listing_jobs_found=scrape_result["jobs_found"],
        listing_status=scrape_result["status"],
    )
    if saved_path:
        logger.info(f"[ScrapeDetails] Full JSON saved → {saved_path}")

    return ScrapeDetailsResponse(
        domain=domain,
        site_type=site_type,
        listing_jobs_found=scrape_result["jobs_found"],
        listing_status=scrape_result["status"],
        jobs_count=len(jobs_detail),
        jobs=jobs_detail,
    )


# ── Batch scrape multiple sites with POST notifications ──────────

class BatchScrapeRequest(BaseModel):
    urls: list[str]


class SiteResult(BaseModel):
    url: str
    domain: str
    site_type: str
    strategy: str
    jobs_found: int
    status: str
    notification_sent: bool
    notification_endpoint: str = ""
    notification_response: str = ""
    error: str = ""


class BatchJobCreateResponse(BaseModel):
    job_id: str
    status: str


class BatchJobStatusResponse(BaseModel):
    job_id: str
    status: str
    created_at: str
    updated_at: str
    download_ready: bool = False
    file_name: str = ""
    total_sites: int = 0
    successful: int = 0
    failed: int = 0
    skipped: int = 0
    error: str = ""


_BATCH_JOB_INTERNAL_URL = "http://localhost:8002/scrape-details-batch"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _run_batch_job(job_id: str, request: BatchScrapeRequest) -> None:
    job = _BATCH_JOBS[job_id]
    job["status"] = "running"
    job["updated_at"] = _utc_now_iso()

    output_dir = Path(__file__).resolve().parents[2] / "output" / "batch_jobs"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{job_id}.csv"

    try:
        async with httpx.AsyncClient(timeout=None) as client:
            response = await client.post(
                _BATCH_JOB_INTERNAL_URL,
                json=request.model_dump(),
            )
            response.raise_for_status()

        output_path.write_bytes(response.content)

        job["status"] = "completed"
        job["updated_at"] = _utc_now_iso()
        job["download_ready"] = True
        job["file_path"] = str(output_path)
        job["file_name"] = "master_jobs.csv"
        job["total_sites"] = int(response.headers.get("X-Total-Sites", len(request.urls)) or 0)
        job["successful"] = int(response.headers.get("X-Successful-Sites", 0) or 0)
        job["failed"] = int(response.headers.get("X-Failed-Sites", 0) or 0)
        job["skipped"] = int(response.headers.get("X-Skipped-Sites", 0) or 0)
    except Exception as exc:
        job["status"] = "failed"
        job["updated_at"] = _utc_now_iso()
        job["error"] = str(exc)


@router.post("/scrape-details-batch/jobs", response_model=BatchJobCreateResponse)
async def create_scrape_details_batch_job(request: BatchScrapeRequest):
    job_id = uuid4().hex
    now = _utc_now_iso()
    _BATCH_JOBS[job_id] = {
        "job_id": job_id,
        "status": "queued",
        "created_at": now,
        "updated_at": now,
        "download_ready": False,
        "file_path": "",
        "file_name": "",
        "total_sites": len(request.urls),
        "successful": 0,
        "failed": 0,
        "skipped": 0,
        "error": "",
    }
    asyncio.create_task(_run_batch_job(job_id, request))
    return BatchJobCreateResponse(job_id=job_id, status="queued")


@router.get("/scrape-details-batch/jobs/{job_id}", response_model=BatchJobStatusResponse)
async def get_scrape_details_batch_job_status(job_id: str):
    job = _BATCH_JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job_not_found")
    return BatchJobStatusResponse(**{k: job.get(k) for k in BatchJobStatusResponse.model_fields})


@router.get("/scrape-details-batch/jobs/{job_id}/download")
async def download_scrape_details_batch_job(job_id: str):
    job = _BATCH_JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job_not_found")
    if job.get("status") != "completed" or not job.get("download_ready"):
        raise HTTPException(status_code=409, detail="job_not_completed")

    file_path = Path(job.get("file_path", ""))
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="result_file_not_found")

    return FileResponse(
        path=str(file_path),
        filename=job.get("file_name") or "master_jobs.csv",
        media_type="text/csv",
    )


@router.post("/scrape-details-batch")
async def scrape_details_batch(
    request: BatchScrapeRequest,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
):
    """Scrape all jobs for all URLs, then return the fresh master CSV file."""
    results = []
    successful = 0
    failed = 0
    skipped = 0
    files_to_cleanup: set[str] = set()
    project_root = Path(__file__).resolve().parents[2]
    master_csv = project_root / "output" / "master_jobs.csv"
    master_json = project_root / "output" / "master_jobs.json"

    for path in (master_csv, master_json):
        try:
            if path.exists():
                path.unlink()
                logger.info("[BatchScrape] Deleted stale output: %s", path)
        except Exception as exc:
            logger.warning("[BatchScrape] Failed deleting stale output %s: %s", path, exc)

    for url in request.urls:
        logger.info(f"[BatchScrape] Processing: {url}")
        
        site_result = SiteResult(
            url=url,
            domain="",
            site_type="UNKNOWN",
            strategy="dom",
            jobs_found=0,
            status="pending",
            notification_sent=False,
        )

        try:
            # Step 1: Normalize URL and scrape (same as scrape-details)
            normalized_url = normalize_site_url(url)
            domain = get_domain(normalized_url)
            site_result.domain = domain
            logger.info(f"[BatchScrape] Starting: {domain}")

            # SmartRecruiters shortcut for batch flow only:
            # send directly to the master ingest service and continue to next URL.
            if "smartrecruiters" in normalized_url.lower():
                site_result.site_type = "SMARTRECRUITERS"
                site_result.strategy = "api"
                notification_endpoint = "http://localhost:8001/process-smartrecruiters"
                payload = {
                    "url": normalized_url,
                }
                logger.info(f"[BatchScrape] {domain} detected SmartRecruiters → sending directly to {notification_endpoint}")

                try:
                    async with httpx.AsyncClient(timeout=300) as notify_client:
                        response = await notify_client.post(notification_endpoint, json=payload)
                        response_text = response.text[:500]
                        notification_response = f"status={response.status_code} body={response_text}"
                        response.raise_for_status()
                        response_json = response.json()
                except Exception as exc:
                    logger.error(f"[BatchScrape] {domain} SmartRecruiters notification failed: {exc}")
                    site_result.status = "failed"
                    site_result.notification_sent = False
                    site_result.notification_endpoint = notification_endpoint
                    site_result.notification_response = f"error={str(exc)}"
                    site_result.error = str(exc)
                    failed += 1
                    results.append(site_result)
                    continue

                site_result.jobs_found = int(response_json.get("processed", 0) or 0)
                json_file = str(response_json.get("json_file") or "").strip()
                if json_file:
                    files_to_cleanup.add(json_file)
                site_result.status = "success"
                site_result.notification_sent = True
                site_result.notification_endpoint = notification_endpoint
                site_result.notification_response = notification_response
                successful += 1
                results.append(site_result)
                logger.info(f"[BatchScrape] {domain} SmartRecruiters complete → processed={site_result.jobs_found}")
                continue

            # Step 2: Get listing URLs via orchestrator (includes strategy lock)
            scrape_result = await orchestrate_scrape(normalized_url, session)
            site_type = scrape_result["type"]
            strategy = scrape_result.get("strategy", "dom")
            api_url = scrape_result.get("api_url", "")
            site_result.site_type = site_type
            site_result.strategy = strategy
            site_result.jobs_found = scrape_result["jobs_found"]
            
            logger.info(f"[BatchScrape] {domain} -> {site_type}, strategy={strategy}, jobs={scrape_result['jobs_found']}")

            # If no jobs found, skip
            if scrape_result["jobs_found"] == 0 or scrape_result["status"] == "skipped":
                site_result.status = "skipped"
                skipped += 1
                results.append(site_result)
                logger.info(f"[BatchScrape] {domain} skipped (no jobs)")
                continue

            # Step 3: Read jobs from latest saved JSON (with raw API data if API strategy)
            domain_dir = RAW_JSON_DIR / domain
            if not domain_dir.exists():
                site_result.status = "failed"
                site_result.error = "domain_dir_not_found"
                failed += 1
                results.append(site_result)
                logger.warning(f"[BatchScrape] {domain} directory not found")
                continue

            files = sorted(domain_dir.glob("scrape_result_*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
            if not files:
                site_result.status = "failed"
                site_result.error = "no_scrape_result_files"
                failed += 1
                results.append(site_result)
                logger.warning(f"[BatchScrape] {domain} no scrape result files")
                continue

            with open(files[0], "r", encoding="utf-8") as f:
                data = json.load(f)
                jobs_raw = data.get("jobs", [])

            logger.info(f"[BatchScrape] {domain} read {len(jobs_raw)} jobs from {files[0].name}")

            # Save raw JSON path for Workday notification (BEFORE detail extraction)
            raw_json_path = str(files[0].resolve())

            logger.info(f"[BatchScrape] {domain} processing {len(jobs_raw)} jobs for detail extraction")
            logger.info("[BatchScrape] [DETAIL STRATEGY] strategy=%s for %s", strategy, domain)

            # Step 4: Extract details using strategy-locked approach (same logic as scrape-details)
            jobs_detail = []
            total_ai_tokens = 0

            if site_type == ACCENTURE_SITE_TYPE:
                jobs_detail, total_ai_tokens = await _extract_accenture_jobs_detail(
                    jobs_raw,
                    domain,
                    "[BatchScrape]",
                )

            elif site_type == "DYNAMIC_API":
                # ── DYNAMIC_API: Jobs already AI-enriched by scraper, pass through ──
                logger.info("[BatchScrape] [DYNAMIC_API] Jobs already enriched — passing through")
                for i, job_data in enumerate(jobs_raw):
                    job_url = str(job_data.get("job_link") or job_data.get("url", "")).strip()
                    job_entry = _build_job_detail_result(
                        job_data,
                        job_url,
                        job_data.get("Scrap_json") or {},
                    )
                    jobs_detail.append(job_entry)
                    total_ai_tokens += (job_data.get("ai_usage") or {}).get("total_tokens", 0)

            elif strategy == "api":
                # ── API STRATEGY: Extract details from raw API data, NO HTML ──
                from app.services.detail_extractor import extract_job_details as extract_api_details

                for i, job_data in enumerate(jobs_raw):
                    job_url = job_data.get("url", "")
                    logger.info(f"[BatchScrape] {domain} API detail extraction job {i+1}/{len(jobs_raw)}")
                    logger.info("[BatchScrape] [DETAIL STRATEGY] Using API for job_id=%s", job_data.get("_raw_api", {}).get("externalPath", "unknown"))
                    if site_type == "WORKDAY_API":
                        logger.info("[BatchScrape] [WORKDAY API] Using base_url=%s", api_url)

                    try:
                        detail = await extract_api_details(
                            strategy="api",
                            job=job_data,
                            site_type=site_type,
                            api_url=api_url,
                            base_url=normalized_url,
                        )
                    except Exception as exc:
                        logger.error(f"[BatchScrape] API detail failed for {job_url}: {exc}")
                        # Failsafe: return partial data, DO NOT fallback to HTML
                        detail = {
                            "title": job_data.get("title", ""),
                            "location": job_data.get("location", ""),
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
                            "qualifications": [],
                            "additional_sections": [],
                        }

                    job_entry = _build_job_detail_result(
                        detail,
                        job_url,
                        {
                            "url": job_url,
                            "strategy": "api",
                            "site_type": site_type,
                            "department": detail.get("department", ""),
                            "qualifications": detail.get("qualifications", []),
                        },
                    )
                    jobs_detail.append(job_entry)

            else:
                # ── DOM STRATEGY: Use standard detail extraction (NO browser) ──
                from app.job_detail_engine.orchestrator import extract_job_details as extract_dom_details

                if site_type == "INTERACTIVE_DOM":
                    # Interactive DOM was explicitly selected — use browser
                    from playwright.async_api import async_playwright
                    from app.job_detail_engine.utils.cleaner import prepare_ai_payload

                    async with async_playwright() as playwright:
                        browser = await playwright.chromium.launch(headless=True)

                        for i, job_data in enumerate(jobs_raw):
                            job_url = job_data.get("url", "")
                            logger.info(f"[BatchScrape] {domain} INTERACTIVE_DOM job {i+1}/{len(jobs_raw)}: {job_url}")
                            page = await browser.new_page()
                            meta = {}
                            ai_usage = {}
                            detail = {}
                            try:
                                # ── Max extraction: scroll, expand, wait for growth ──
                                await page.goto(job_url, timeout=60000)
                                try:
                                    await page.wait_for_load_state("networkidle")
                                except Exception:
                                    logger.warning("[BatchScrape] [DETAIL] networkidle timeout for %s", job_url)
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
                                    logger.warning("[BatchScrape] [DETAIL] Button expansion failed: %s", exc)

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
                                logger.info("[BatchScrape] [DETAIL] url=%s html_length=%d", job_url, len(html))

                                # ── Prepare AI payload — ONLY remove script/style/noscript ──
                                payload = prepare_ai_payload(html, domain=domain)
                                logger.info("[BatchScrape] [AI PAYLOAD] length=%d source=JOB_DETAIL", len(payload))

                                if len(payload) < 2000:
                                    logger.warning("[BatchScrape] [WEAK DETAIL PAGE] url=%s length=%d", job_url, len(payload))

                                # ── Send to AI for extraction ──
                                detail = await extract_dom_details(payload, force_ai=True, domain=domain)
                                meta = detail.pop("_meta", {})
                                ai_usage = detail.pop("ai_usage", {})
                                total_ai_tokens += ai_usage.get("total_tokens", 0)
                                logger.info(
                                    f"[BatchScrape] [Engine] {domain} job {i+1} → title={detail.get('title','')!r}, "
                                    f"parser={meta.get('parser_used','?')}, "
                                    f"score={meta.get('confidence',0)}, "
                                    f"ai_used={meta.get('ai_used', False)}, "
                                    f"ai_forced={meta.get('ai_forced', False)}, "
                                    f"skills={len(detail.get('skills',[]))}, "
                                    f"ai_tokens={ai_usage.get('total_tokens', 0)}"
                                )
                            except Exception as exc:
                                logger.error(f"[BatchScrape] Failed for {job_url}: {exc}")
                                meta = {}
                                ai_usage = {}
                                detail = {}
                            finally:
                                await page.close()

                            job_entry = _build_job_detail_result(
                                detail,
                                job_url,
                                {
                                    "url": job_url,
                                    "strategy": "INTERACTIVE_DOM",
                                    "parser_used": str(meta.get("parser_used", "")),
                                    "confidence": meta.get("confidence", 0),
                                    "ai_forced": meta.get("ai_forced", False),
                                    "preferred_skills": detail.get("preferred_skills") or [],
                                    "tools_and_technologies": detail.get("tools_and_technologies") or [],
                                    "certifications": detail.get("certifications") or [],
                                    "soft_skills": detail.get("soft_skills") or [],
                                    "inferred_skills": detail.get("inferred_skills") or [],
                                    "benefits": detail.get("benefits") or [],
                                },
                            )
                            jobs_detail.append(job_entry)

                        await browser.close()
                else:
                    # ── Standard DOM strategy: no browser needed ──
                    # Deduplicate and normalize URLs before processing
                    from urllib.parse import urlparse, urlunparse, parse_qs

                    seen_detail_urls: set[str] = set()
                    normalized_jobs: list[dict] = []
                    for job_data in jobs_raw:
                        job_url = job_data.get("url", "")
                        if not job_url:
                            continue
                        # Normalize: strip query params, trailing slash
                        parsed = urlparse(job_url)
                        normalized_path = parsed.path.rstrip("/")
                        clean_url = urlunparse((parsed.scheme, parsed.netloc, normalized_path, "", "", ""))
                        if clean_url in seen_detail_urls:
                            logger.info("[BatchScrape] [DEDUP] Skipping duplicate: %s", job_url)
                            continue
                        seen_detail_urls.add(clean_url)
                        normalized_jobs.append({**job_data, "_clean_url": clean_url})

                    for i, job_data in enumerate(normalized_jobs):
                        job_url = job_data.get("url", "")
                        clean_url = job_data.get("_clean_url", job_url)

                        # ── Strict URL filtering ──
                        lowered = job_url.lower()
                        reject_parts = [
                            "/careers/", "/career-search", "/why-", "/about",
                            "/learning", "/projects", "/contact", "/our-team",
                            "/testimonials", "/events", "/blog", "/news",
                            "/insights", "/resources", "/webinars",
                        ]
                        if any(p in lowered for p in reject_parts):
                            logger.info("[BatchScrape] [FILTER] Rejected non-job URL: %s", job_url)
                            continue

                        logger.info(f"[BatchScrape] {domain} DOM detail extraction job {i+1}/{len(normalized_jobs)}: {job_url}")

                        # Fetch the detail page HTML via httpx
                        try:
                            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                                resp = await client.get(job_url)
                                resp.raise_for_status()
                                html = resp.text
                        except Exception as exc:
                            logger.error(f"[BatchScrape] Failed to fetch {job_url}: {exc}")
                            continue

                        # ── Page quality filter ──
                        html_len = len(html)
                        if html_len < 1500:
                            logger.info("[BatchScrape] [SKIP AI] Weak page: %s (length=%d < 1500)", job_url, html_len)
                            continue

                        # Check for title
                        import re
                        title_match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
                        page_title = title_match.group(1).strip() if title_match else ""
                        if not page_title:
                            logger.info("[BatchScrape] [SKIP AI] Weak page: %s (no title)", job_url)
                            continue

                        try:
                            detail = await extract_dom_details(html, force_ai=True, domain=domain)
                            meta = detail.pop("_meta", {})
                            ai_usage = detail.pop("ai_usage", {})

                            # ── Score-based AI skip ──
                            conf_score = meta.get("confidence", 0)
                            if conf_score < 3:
                                logger.info("[BatchScrape] [SKIP AI] Low confidence score=%d for %s", conf_score, job_url)
                                # Still keep the job if parser extracted a title
                                if not detail.get("title"):
                                    continue

                            total_ai_tokens += ai_usage.get("total_tokens", 0)
                            logger.info(
                                f"[BatchScrape] [Engine] {domain} job {i+1} → title={detail.get('title','')!r}, "
                                f"parser={meta.get('parser_used','?')}, "
                                f"score={meta.get('confidence',0)}, "
                                f"ai_used={meta.get('ai_used', False)}, "
                                f"ai_forced={meta.get('ai_forced', False)}, "
                                f"skills={len(detail.get('skills',[]))}, "
                                f"ai_tokens={ai_usage.get('total_tokens', 0)}"
                            )
                        except Exception as exc:
                            logger.error(f"[BatchScrape] Detail extraction failed for {job_url}: {exc}")
                            continue

                        job_entry = _build_job_detail_result(
                            detail,
                            job_url,
                            {
                                "url": job_url,
                                "strategy": "dom",
                                "parser_used": str(meta.get("parser_used", "")),
                                "confidence": meta.get("confidence", 0),
                                "ai_forced": meta.get("ai_forced", False),
                                "preferred_skills": detail.get("preferred_skills") or [],
                                "tools_and_technologies": detail.get("tools_and_technologies") or [],
                                "certifications": detail.get("certifications") or [],
                                "soft_skills": detail.get("soft_skills") or [],
                                "inferred_skills": detail.get("inferred_skills") or [],
                                "benefits": detail.get("benefits") or [],
                            },
                        )
                        jobs_detail.append(job_entry)

            logger.info(
                f"[BatchScrape] {domain} complete → {len(jobs_detail)} jobs detailed, "
                f"strategy={strategy}, total AI tokens={total_ai_tokens}"
            )

            # Step 5: Save full JSON output to job-details/{domain}/
            from app.job_detail_engine.utils.json_saver import save_job_details
            saved_path = save_job_details(
                jobs=[j.model_dump() for j in jobs_detail],
                domain=domain,
                site_type=site_type,
                listing_jobs_found=scrape_result["jobs_found"],
                listing_status=scrape_result["status"],
            )
            if saved_path:
                logger.info(f"[BatchScrape] Full JSON saved → {saved_path}")
                files_to_cleanup.add(str(saved_path))
            if raw_json_path:
                files_to_cleanup.add(str(raw_json_path))

            # Step 6: Send POST request to port 8001 based on site type
            notification_sent = False
            notification_endpoint = ""
            notification_response = ""

            try:
                if site_type == "WORKDAY_API":
                    # Workday: POST to /process-workday with RAW JSON path
                    notification_endpoint = "http://localhost:8001/process-workday"
                    payload = {
                        "file_path": raw_json_path,
                    }
                    logger.info(f"[BatchScrape] {domain} Sending POST to {notification_endpoint} with file_path={raw_json_path}")

                    async with httpx.AsyncClient(timeout=300) as notify_client:
                        response = await notify_client.post(notification_endpoint, json=payload)
                        notification_response = f"status={response.status_code} body={response.text[:500]}"
                        logger.info(f"[BatchScrape] {domain} Response: {notification_response}")

                    notification_sent = True

                else:
                    # Other sites: POST to /ingest-json with detail JSON path
                    notification_endpoint = "http://localhost:8001/ingest-json"
                    payload = {
                        "file_path": saved_path,
                    }
                    logger.info(f"[BatchScrape] {domain} Sending POST to {notification_endpoint}")

                    async with httpx.AsyncClient(timeout=300) as notify_client:
                        response = await notify_client.post(notification_endpoint, json=payload)
                        notification_response = f"status={response.status_code} body={response.text[:500]}"
                        logger.info(f"[BatchScrape] {domain} Response: {notification_response}")

                    notification_sent = True

            except Exception as exc:
                logger.error(f"[BatchScrape] {domain} Failed to send notification: {exc}")
                notification_response = f"error={str(exc)}"

            # Update site result
            site_result.status = "success"
            site_result.notification_sent = notification_sent
            site_result.notification_endpoint = notification_endpoint
            site_result.notification_response = notification_response
            successful += 1

        except Exception as exc:
            logger.error(f"[BatchScrape] Failed for {url}: {exc}")
            site_result.status = "failed"
            site_result.error = str(exc)
            failed += 1

        results.append(site_result)

    logger.info(f"[BatchScrape] Complete → total={len(request.urls)}, success={successful}, failed={failed}, skipped={skipped}")
    if not master_csv.exists():
        return {
            "total_sites": len(request.urls),
            "successful": successful,
            "failed": failed,
            "skipped": skipped,
            "results": [r.model_dump() for r in results],
            "error": "master_csv_not_created",
        }

    def _cleanup_batch_outputs(paths: list[str], master_json_path: str):
        for raw_path in paths:
            try:
                file_path = Path(raw_path)
                if file_path.exists():
                    file_path.unlink()
            except Exception as exc:
                logger.warning("[BatchScrape] Cleanup failed for %s: %s", raw_path, exc)

        try:
            file_path = Path(master_json_path)
            if file_path.exists():
                file_path.unlink()
        except Exception as exc:
            logger.warning("[BatchScrape] Cleanup failed for %s: %s", master_json_path, exc)

    background_tasks.add_task(
        _cleanup_batch_outputs,
        sorted(files_to_cleanup),
        str(master_json),
    )

    return FileResponse(
        path=str(master_csv),
        filename="master_jobs.csv",
        media_type="text/csv",
        background=background_tasks,
        headers={
            "X-Total-Sites": str(len(request.urls)),
            "X-Successful-Sites": str(successful),
            "X-Failed-Sites": str(failed),
            "X-Skipped-Sites": str(skipped),
        },
    )


HARD_CODED_URLS = [
    "https://medtronic.wd1.myworkdayjobs.com/MedtronicCareers?locationCountry=c4f78be1a8f14da0ab49ce1162348a5e&jobFamilyGroup=2fe8588f35e84eb98ef535f4d738f243",
    "https://medtronic.wd1.myworkdayjobs.com/MedtronicCareers?jobFamilyGroup=5d03e9707876432d93848a9e7146e1ad",
    "https://jobs.dell.com/en/search-jobs/India/375/2/1269750/22/79/50/2",
    "https://jobs.standardchartered.com/go/Experienced-Professional-jobs/9783657/?feedid=363857&markerViewed=&carouselIndex=&facetFilters=%7B%22cust_region%22%3A%5B%22Asia%22%5D%2C%22jobLocationCountry%22%3A%5B%22India%22%5D%2C%22cust_csb_employmentType%22%3A%5B%22+Permanent%22%5D%7D&pageNumber=0",
]


@router.post("/scrape-hardcoded", response_model=BulkScrapeResponse)
async def scrape_hardcoded_urls(session: AsyncSession = Depends(get_session)):
    """Scrape a fixed list of URLs."""

    results = []
    successful = 0
    failed = 0
    skipped = 0

    for url in HARD_CODED_URLS:
        try:
            scrape_result = await orchestrate_scrape(url, session)
            results.append(ScrapeResponse(
                domain=scrape_result["domain"],
                type=scrape_result["type"],
                confidence=scrape_result["confidence"],
                jobs_found=scrape_result["jobs_found"],
                status=scrape_result["status"],
            ))
            if scrape_result["status"] == "success" and scrape_result["jobs_found"] > 0:
                successful += 1
            elif scrape_result["status"] == "skipped":
                skipped += 1
            else:
                failed += 1
        except Exception as e:
            failed += 1
            results.append(ScrapeResponse(
                domain=url,
                type="ERROR",
                confidence=0.0,
                jobs_found=0,
                status="failed",
            ))

    return BulkScrapeResponse(
        total_sites=len(HARD_CODED_URLS),
        successful=successful,
        failed=failed,
        skipped=skipped,
        results=results,
    )


@router.post("/scrape-all", response_model=BulkScrapeResponse)
async def scrape_all_sites(session: AsyncSession = Depends(get_session)):
    """Scrape all sites from the database one at a time."""

    # Fetch all sites from DB
    result = await session.execute(select(Site))
    sites = result.scalars().all()

    results = []
    successful = 0
    failed = 0
    skipped = 0

    for site in sites:
        try:
            url = normalize_site_url(site.domain)
            scrape_result = await orchestrate_scrape(url, session)
            results.append(ScrapeResponse(
                domain=scrape_result["domain"],
                type=scrape_result["type"],
                confidence=scrape_result["confidence"],
                jobs_found=scrape_result["jobs_found"],
                status=scrape_result["status"],
            ))
            if scrape_result["status"] == "success" and scrape_result["jobs_found"] > 0:
                successful += 1
            elif scrape_result["status"] == "skipped":
                skipped += 1
            else:
                failed += 1
        except Exception as e:
            failed += 1
            results.append(ScrapeResponse(
                domain=site.domain,
                type="ERROR",
                confidence=0.0,
                jobs_found=0,
                status="failed",
            ))

    return BulkScrapeResponse(
        total_sites=len(sites),
        successful=successful,
        failed=failed,
        skipped=skipped,
        results=results,
    )


@router.post("/scrape-hardcoded-details")
async def scrape_hardcoded_job_details():
    """Scrape hardcoded URLs, then extract details using strategy-locked approach."""
    from app.db.database import async_session
    from app.services.orchestrator import orchestrate_scrape
    from app.services.raw_json_saver import RAW_JSON_DIR

    result = {"sites": []}

    for url in HARD_CODED_URLS:
        normalized_url = normalize_site_url(url)
        domain = get_domain(normalized_url)
        logger.info(f"[DetailScraper] Starting: {domain}")

        job_urls = []
        site_type = "UNKNOWN"
        strategy = "dom"
        api_url = ""
        jobs_raw_list = []

        # Step 1: Scrape the job listing page to get job URLs + strategy
        try:
            async with async_session() as session:
                scrape_result = await orchestrate_scrape(normalized_url, session)
                site_type = scrape_result["type"]
                strategy = scrape_result.get("strategy", "dom")
                api_url = scrape_result.get("api_url", "")
                logger.info(f"[DetailScraper] {domain} -> {site_type}, strategy={strategy}, jobs={scrape_result['jobs_found']}")
        except Exception as e:
            logger.warning(f"[DetailScraper] Orchestrate failed for {url}: {e}")

        # Step 2: Collect jobs from the latest saved JSON
        domain_dir = RAW_JSON_DIR / domain
        if domain_dir.exists():
            files = sorted(domain_dir.glob("scrape_result_*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
            if files:
                with open(files[0], "r", encoding="utf-8") as f:
                    data = json.load(f)
                    site_type = data.get("site_type", site_type)
                    jobs_raw_list = data.get("jobs", [])
                    job_urls = [job.get("url", "") for job in jobs_raw_list if job.get("url")]
                    logger.info(f"[DetailScraper] {domain} collected {len(jobs_raw_list)} jobs, strategy={strategy}")

        # Step 3: Extract details using strategy-locked approach
        site_entry = {
            "domain": domain,
            "site_type": site_type,
            "strategy": strategy,
            "jobs_count": 0,
            "jobs": [],
        }

        if strategy == "api" and jobs_raw_list:
            # ── API STRATEGY: Use raw API data, NO HTML scraping ──
            from app.services.detail_extractor import extract_job_details as extract_api_details

            logger.info("[DETAIL STRATEGY] Using API for %s", domain)
            if site_type == "WORKDAY_API":
                logger.info("[WORKDAY API] Using base_url=%s", api_url)

            for i, job_data in enumerate(jobs_raw_list):
                job_url = job_data.get("url", "")
                logger.info("[DETAIL STRATEGY] Using API for job_id=%s", job_data.get("_raw_api", {}).get("externalPath", "unknown"))
                try:
                    detail = await extract_api_details(
                        strategy="api",
                        job=job_data,
                        site_type=site_type,
                        api_url=api_url,
                        base_url=normalized_url,
                    )
                except Exception as exc:
                    logger.error(f"[DetailScraper] API detail failed for {job_url}: {exc}")
                    detail = {
                        "id": job_url.split("/")[-1] if job_url else "",
                        "title": job_data.get("title", ""),
                        "company_name": "",
                        "job_link": job_url,
                        "experience": "",
                        "locations": [job_data["location"]] if isinstance(job_data.get("location"), str) and job_data.get("location") else (job_data.get("location") if isinstance(job_data.get("location"), list) else []),
                        "educational_qualifications": "",
                        "required_skill_set": [],
                        "remote_type": "",
                        "posted_on": "",
                        "job_id": "",
                        "salary": "",
                        "is_active": True,
                        "additional_sections": [],
                        "Scrap_json": {"url": job_url, "error": "api_detail_failed"},
                    }
                # Normalize location field to always be a list
                loc = detail.get("location", "")
                if isinstance(loc, str) and loc:
                    detail["locations"] = [loc]
                elif isinstance(loc, list):
                    detail["locations"] = loc
                else:
                    detail["locations"] = []
                detail.pop("location", None)
                site_entry["jobs"].append(detail)

        elif job_urls:
            # ── DOM STRATEGY: Force INTERACTIVE_DOM — max extraction ──
            try:
                from playwright.async_api import async_playwright
                from app.job_detail_engine.utils.cleaner import prepare_ai_payload

                async with async_playwright() as playwright:
                    browser = await playwright.chromium.launch(headless=True)

                    for i, job_url in enumerate(job_urls):
                        logger.info(f"[DetailScraper] {domain} INTERACTIVE_DOM job {i+1}/{len(job_urls)}: {job_url}")
                        page = await browser.new_page()
                        try:
                            # ── Max extraction ──
                            await page.goto(job_url, timeout=60000)
                            try:
                                await page.wait_for_load_state("networkidle")
                            except Exception:
                                pass
                            await page.wait_for_timeout(2000)

                            for _ in range(5):
                                await page.mouse.wheel(0, 3000)
                                await page.wait_for_timeout(1000)

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
                            except Exception:
                                pass

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

                            payload = prepare_ai_payload(html, domain=domain)
                            logger.info("[AI PAYLOAD] length=%d source=JOB_DETAIL", len(payload))

                            if len(payload) < 2000:
                                logger.warning("[WEAK DETAIL PAGE] url=%s length=%d", job_url, len(payload))

                            detail = await extract_job_details(payload, force_ai=True, domain=domain)
                            meta = detail.pop("_meta", {})
                            logger.info(
                                f"[Engine] {domain} job {i+1} → title={detail.get('title','')!r}, "
                                f"parser={meta.get('parser_used','?')}, "
                                f"score={meta.get('confidence',0)}, "
                                f"ai={meta.get('ai_used',False)}, "
                                f"skills={len(detail.get('skills',[]))}"
                            )
                            detail["id"] = detail.get("job_id", "") or job_url.split("/")[-1]
                            detail["job_link"] = job_url
                        except Exception as exc:
                            logger.error(f"[DetailScraper] Failed for {job_url}: {exc}")
                            detail = {
                                "id": job_url.split("/")[-1],
                                "title": "",
                                "company_name": "",
                                "job_link": job_url,
                                "experience": "",
                                "locations": [],
                                "educational_qualifications": "",
                                "required_skill_set": [],
                                "remote_type": "unknown",
                                "posted_on": "",
                                "job_id": "",
                                "salary": "",
                                "is_active": True,
                                "job_summary": "",
                                "key_responsibilities": [],
                                "additional_sections": [],
                                "about_us": "",
                                "Scrap_json": {"url": job_url, "error": "scrape_failed"},
                            }
                        finally:
                            await page.close()
                        site_entry["jobs"].append(detail)

                    await browser.close()
            except Exception as e:
                logger.error(f"[DetailScraper] Browser error for {domain}: {e}")
                for job_url in job_urls:
                    site_entry["jobs"].append({
                        "id": job_url.split("/")[-1],
                        "title": "",
                        "company_name": "",
                        "job_link": job_url,
                        "experience": "",
                        "locations": [],
                        "educational_qualifications": "",
                        "required_skill_set": [],
                        "remote_type": "unknown",
                        "posted_on": "",
                        "job_id": "",
                        "salary": "",
                        "is_active": True,
                        "job_summary": "",
                        "key_responsibilities": [],
                        "additional_sections": [],
                        "about_us": "",
                        "Scrap_json": {"url": job_url, "error": "scrape_failed"},
                    })

        site_entry["jobs_count"] = len(site_entry["jobs"])
        result["sites"].append(site_entry)
        logger.info(f"[DetailScraper] {domain} done -> {site_entry['jobs_count']} jobs detailed, strategy={strategy}")

    return result

class ProcessRequest(BaseModel):
    file_path: str
    limit: int = 50


class ProcessResponse(BaseModel):
    status: str
    jobs_processed: int
    file_path: str


@router.post("/process", response_model=ProcessResponse)
async def process_workday_jobs(request: ProcessRequest, session: AsyncSession = Depends(get_session)):
    """Process Workday jobs from a saved raw JSON file.

    This endpoint is called by the main scraper after Workday raw data is saved.
    It reads the JSON file, extracts job details, and saves the results.
    """
    import json
    from app.job_detail_engine.utils.json_saver import save_job_details

    file_path = request.file_path
    limit = request.limit

    logger.info(f"[Process] Received request to process: {file_path} (limit={limit})")

    # Read the raw JSON file
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.error(f"[Process] Failed to read file {file_path}: {e}")
        return ProcessResponse(
            status="error",
            jobs_processed=0,
            file_path=file_path,
        )

    jobs_raw = data.get("jobs", [])
    domain = data.get("domain", "unknown")
    site_type = data.get("site_type", "WORKDAY_API")

    # Cap to limit
    jobs_raw = jobs_raw[:limit]

    logger.info(f"[Process] Processing {len(jobs_raw)} jobs for {domain}")

    # Extract details for each job
    jobs_detail = []
    total_ai_tokens = 0

    from app.services.detail_extractor import extract_job_details as extract_api_details

    for i, job_data in enumerate(jobs_raw):
        job_url = job_data.get("url", "")
        api_url = data.get("metadata", {}).get("api_url", "")

        try:
            detail = await extract_api_details(
                strategy="api",
                job=job_data,
                site_type=site_type,
                api_url=api_url,
                base_url=data.get("metadata", {}).get("url", ""),
            )
        except Exception as exc:
            logger.error(f"[Process] Detail extraction failed for job {i}: {exc}")
            detail = {
                "title": job_data.get("title", ""),
                "location": job_data.get("location", ""),
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
            }

        job_entry = {
            "id": str(detail.get("job_id") or job_url.split("/")[-1]),
            "title": str(detail.get("title") or ""),
            "company_name": str(detail.get("company_name") or ""),
            "job_link": job_url,
            "experience": str(detail.get("experience") or ""),
            "locations": [detail["location"]] if isinstance(detail.get("location"), str) and detail.get("location") else (detail.get("location") or []),
            "educational_qualifications": str(detail.get("education") or detail.get("qualifications", [])),
            "required_skill_set": detail.get("skills", detail.get("required_skills", [])),
            "remote_type": str(detail.get("remote_type") or ""),
            "posted_on": str(detail.get("posted_on") or ""),
            "job_id": str(detail.get("job_id") or ""),
            "salary": str(detail.get("salary") or ""),
            "is_active": True,
            "first_seen": "",
            "last_seen": "",
            "job_summary": str(detail.get("job_summary") or detail.get("description") or ""),
            "key_responsibilities": detail.get("key_responsibilities") or [],
            "additional_sections": detail.get("additional_sections") or [],
            "about_us": str(detail.get("about_us") or detail.get("about_company") or ""),
            "Scrap_json": {
                "url": job_url,
                "strategy": "api",
                "site_type": site_type,
            },
        }
        jobs_detail.append(job_entry)
        ai_usage = detail.get("ai_usage") or {}
        total_ai_tokens += ai_usage.get("total_tokens", 0)

    # Save to job-details/{domain}/full_json/
    saved_path = save_job_details(
        jobs=jobs_detail,
        domain=domain,
        site_type=site_type,
        listing_jobs_found=len(jobs_raw),
        listing_status="success",
    )

    logger.info(
        f"[Process] Complete → {len(jobs_detail)} jobs processed, "
        f"total AI tokens={total_ai_tokens}, saved to={saved_path}"
    )

    return ProcessResponse(
        status="success",
        jobs_processed=len(jobs_detail),
        file_path=file_path,
    )


@router.get("/test-scrape")
async def test_scrape():
    """Run lightweight test scrape on hardcoded URLs.

    Limits to 20 jobs per domain, saves JSON files to /data/,
    and returns the total count of jobs scraped.
    """
    result = await run_test_scrape()
    return result
