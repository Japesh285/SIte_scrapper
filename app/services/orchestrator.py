import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.detectors import (
    detect_dom_browser,
    detect_dom_infinite_scroll,
    detect_dom_load_more,
    detect_workday,
    detect_greenhouse,
    detect_simple_api,
    detect_dynamic_api,
    detect_interactive_dom,
    run_browser_probe,
    inspect_browser_network,
)
from app.core.site_utils import get_domain, normalize_site_url
from app.detectors.simple_api import fetch_simple_api_jobs
from app.detectors.workday import fetch_workday_jobs
from app.services.ai_classifier import classify_site
from app.scrapers.dom_browser import (
    scrape_dom_browser,
    scrape_dom_infinite_scroll,
    scrape_dom_load_more,
)
from app.scrapers.simple_api import scrape_simple_api
from app.scrapers.greenhouse import scrape_greenhouse
from app.scrapers.dynamic_api import scrape_dynamic_api, scrape_dynamic_api_direct
from app.scrapers.interactive_dom import scrape_interactive_dom
from app.services.raw_json_saver import save_scrape_result
from app.db.models import Site, Job
from app.core.logger import logger

# ── URL filters ─────────────────────────────────────────────────────

_NON_JOB_API_PATTERNS = [
    "bugherd", "googleapis", "analytics", "tracking",
    "datadog", "segment", "sentry", "hotjar",
    "telemetry", "metrics", "ping", "beacon",
    "cdn.", "static.", "fonts.", "assets.",
]

_DETAIL_URL_REJECT_PARTS = [
    "search", "jobs?", "job-search", "careers",
    "home", "404", "career-journeys",
    "about", "contact", "privacy", "terms",
    "login", "signin", "signup", "register",
]


def _is_valid_job_api_url(url: str) -> bool:
    """Reject non-job API URLs (analytics, tracking, etc.)."""
    lowered = url.lower()
    return not any(p in lowered for p in _NON_JOB_API_PATTERNS)


def _is_valid_detail_url(url: str) -> bool:
    """Reject search/404/marketing URLs from detail extraction."""
    if not url:
        return False
    lowered = url.lower()
    return not any(p in lowered for p in _DETAIL_URL_REJECT_PARTS)


async def orchestrate_scrape(url: str, session: AsyncSession) -> dict:
    """Main orchestration flow: detect, classify, lock strategy, scrape, save."""
    normalized_url = normalize_site_url(url)
    domain = get_domain(normalized_url)
    logger.info(f"Testing: {domain}")
    page_html = ""
    browser_probe: dict | None = None
    discovered_urls: list[str] = []
    dom_browser_result = {
        "matched": False,
        "jobs_found": 0,
        "api_usable": False,
        "browser_compatible": False,
    }
    dom_load_more_result = {
        "matched": False,
        "jobs_found": 0,
        "api_usable": False,
        "browser_compatible": False,
    }
    dom_infinite_scroll_result = {
        "matched": False,
        "jobs_found": 0,
        "api_usable": False,
        "browser_compatible": False,
    }
    dynamic_api_result = {
        "matched": False,
        "api_usable": False,
        "api_url": "",
        "method": "",
        "payload": None,
        "headers": {},
        "confidence": 0.0,
    }

    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        try:
            landing_response = await client.get(normalized_url)
            landing_response.raise_for_status()
            page_html = landing_response.text
            if landing_response.url:
                normalized_url = normalize_site_url(str(landing_response.url))
                domain = get_domain(normalized_url)
        except httpx.HTTPError as exc:
            logger.warning(f"Initial fetch failed for {normalized_url}: {exc}")

        # ═══════════════════════════════════════════════════════════
        # FIXED DETECTION ORDER — same for EVERY site, no early exits
        # ═══════════════════════════════════════════════════════════

        # 1. Workday (HTTP-only, fastest)
        workday_result = await detect_workday(
            normalized_url,
            client=client,
            html=page_html,
        )
        logger.info(f"Workday -> {workday_result}")

        # 2. Greenhouse (HTTP-only, fastest)
        greenhouse_result = await detect_greenhouse(
            normalized_url,
            client=client,
            html=page_html,
        )
        logger.info(
            "Greenhouse -> matched=%s jobs=%s usable=%s source=%s slug=%s board=%s",
            greenhouse_result.get("matched"),
            greenhouse_result.get("jobs_found"),
            greenhouse_result.get("api_usable"),
            greenhouse_result.get("source", ""),
            greenhouse_result.get("slug", ""),
            greenhouse_result.get("board_url", ""),
        )

        # 3. Simple API (HTTP-only, fast)
        simple_api_result = await detect_simple_api(normalized_url, client=client)
        logger.info(f"Simple API -> {simple_api_result}")

        # 4. DOM detectors (static analysis, no browser)
        dom_browser_result = await detect_dom_browser(normalized_url, html=page_html)
        logger.info(f"DOM Browser -> {dom_browser_result}")
        dom_load_more_result = await detect_dom_load_more(normalized_url, html=page_html)
        logger.info(f"DOM Load More -> {dom_load_more_result}")
        dom_infinite_scroll_result = await detect_dom_infinite_scroll(normalized_url, html=page_html)
        logger.info(f"DOM Infinite Scroll -> {dom_infinite_scroll_result}")

        # 5. Browser probe — ONLY if all fast detectors failed
        if not any(
            result.get("api_usable")
            for result in (
                workday_result,
                greenhouse_result,
                simple_api_result,
                dom_browser_result,
                dom_load_more_result,
                dom_infinite_scroll_result,
            )
        ):
            # ── UNIFIED BROWSER PROBE ────────────────────────────────
            probe_result = await run_browser_probe(normalized_url)

            # Also run legacy probe for backward compatibility
            browser_probe = await inspect_browser_network(normalized_url)
            discovered_urls = sorted(
                {
                    *browser_probe.get("json_urls", []),
                    *browser_probe.get("request_urls", []),
                }
            )
            browser_final_url = browser_probe.get("final_url", "")
            if browser_final_url:
                normalized_url = normalize_site_url(browser_final_url)
                domain = get_domain(normalized_url)

            logger.info(
                f"Browser probe -> available={browser_probe.get('available')} "
                f"urls={len(discovered_urls)} errors={browser_probe.get('errors', [])}"
            )

            # ── DYNAMIC_API from probe ───────────────────────────────
            from app.detectors.dynamic_api_detector import detect_dynamic_api_from_probe
            dynamic_api_result = detect_dynamic_api_from_probe(probe_result)
            logger.info(f"Dynamic API (probe) -> {dynamic_api_result}")

            # ── INTERACTIVE_DOM from probe ───────────────────────────
            interactive_dom_result = detect_interactive_dom(probe_result)
            logger.info(f"Interactive DOM -> {interactive_dom_result}")

            # Re-run detectors with discovered URLs
            workday_result = await detect_workday(
                normalized_url,
                client=client,
                html=page_html,
                discovered_urls=discovered_urls,
            )
            logger.info(f"Workday (browser-assisted) -> {workday_result}")

            greenhouse_result = await detect_greenhouse(
                normalized_url,
                client=client,
                html=page_html,
                discovered_urls=discovered_urls,
            )
            logger.info(
                "Greenhouse (browser-assisted) -> matched=%s jobs=%s usable=%s source=%s slug=%s board=%s",
                greenhouse_result.get("matched"),
                greenhouse_result.get("jobs_found"),
                greenhouse_result.get("api_usable"),
                greenhouse_result.get("source", ""),
                greenhouse_result.get("slug", ""),
                greenhouse_result.get("board_url", ""),
            )

            simple_api_result = await detect_simple_api(
                normalized_url,
                client=client,
                discovered_urls=discovered_urls,
            )
            logger.info(f"Simple API (browser-assisted) -> {simple_api_result}")
            dom_browser_result = await detect_dom_browser(
                normalized_url,
                html=page_html,
                discovered_urls=discovered_urls,
                browser_probe=browser_probe,
            )
            logger.info(f"DOM Browser (browser-assisted) -> {dom_browser_result}")
            dom_load_more_result = await detect_dom_load_more(
                normalized_url,
                html=page_html,
                discovered_urls=discovered_urls,
                browser_probe=browser_probe,
            )
            logger.info(f"DOM Load More (browser-assisted) -> {dom_load_more_result}")
            dom_infinite_scroll_result = await detect_dom_infinite_scroll(
                normalized_url,
                html=page_html,
                discovered_urls=discovered_urls,
                browser_probe=browser_probe,
            )
            logger.info(f"DOM Infinite Scroll (browser-assisted) -> {dom_infinite_scroll_result}")
        else:
            # No browser probe needed — stub results for consistency
            interactive_dom_result = {"matched": False, "api_usable": False, "jobs_found": 0, "confidence": 0.0}
            browser_probe = None

    # Build payload for AI classifier
    payload = {
        "domain": domain,
        "url": normalized_url,
        "tests": {
            "workday": workday_result,
            "greenhouse": greenhouse_result,
            "simple_api": simple_api_result,
            "dynamic_api": dynamic_api_result if "dynamic_api_result" in dir() else {},
            "interactive_dom": interactive_dom_result if "interactive_dom_result" in dir() else {},
            "dom_browser": dom_browser_result,
            "dom_load_more": dom_load_more_result,
            "dom_infinite_scroll": dom_infinite_scroll_result,
        },
    }
    if browser_probe:
        payload["browser_probe"] = {
            "available": browser_probe.get("available", False),
            "final_url": browser_probe.get("final_url", ""),
            "json_url_count": len(browser_probe.get("json_urls", [])),
            "request_url_count": len(browser_probe.get("request_urls", [])),
            "dom_signals": browser_probe.get("dom_signals", {}),
            "errors": browser_probe.get("errors", []),
        }

    logger.info(f"Detection results: {payload['tests']}")

    # Classify
    classification = await classify_site(payload)
    site_type = classification["type"]
    confidence = classification["confidence"]

    # ── STRATEGY LOCKING ──────────────────────────────────────────
    # If selected source contains "API" → strategy = "api", else "dom"
    selected_source = site_type  # e.g. WORKDAY_API, SIMPLE_API, DOM_BROWSER
    strategy = "api" if "API" in selected_source else "dom"
    logger.info("[STRATEGY LOCK] site_type=%s → strategy=%s", site_type, strategy)

    logger.info(f"Selected -> {site_type} ({confidence})")

    result = await session.execute(select(Site).where(Site.domain == url))
    site = result.scalar_one_or_none()
    if site is None:
        result = await session.execute(select(Site).where(Site.domain == normalized_url))
        site = result.scalar_one_or_none()

    if site is None:
        site = Site(domain=normalized_url, type=site_type, confidence=confidence)
        session.add(site)
        await session.flush()
    else:
        site.domain = normalized_url
        site.type = site_type
        site.confidence = confidence

    if site_type == "UNKNOWN":
        await session.commit()
        logger.info(f"Jobs -> 0 (skipped)")
        return {
            "domain": domain,
            "type": site_type,
            "confidence": confidence,
            "jobs_found": 0,
            "status": "skipped",
            "strategy": strategy,
        }

    # ── STRATEGY PIPELINE ──────────────────────────────────────────
    # ORDER: simple_api → dom_scraper → dynamic_api → interactive_dom
    # dom_scraper is the MAIN strategy. dynamic_api only runs if DOM fails.
    # interactive_dom is STRICT LAST RESORT.
    # Early exit thresholds:
    #   - dom_scraper >= 5 jobs → STOP (no dynamic_api, no interactive_dom)
    #   - simple_api >= 3 jobs → STOP
    #   - dynamic_api >= 5 jobs → STOP
    #   - interactive_dom runs only if dom < 3 AND dynamic_api failed/< 3

    jobs: list[dict] = []
    api_url = ""
    final_site_type = site_type
    final_strategy = strategy
    dom_jobs_count = 0

    async def _try_simple_api() -> bool:
        nonlocal jobs, api_url, final_site_type, final_strategy
        logger.info("[PIPELINE] Trying simple_api")
        jobs_result, selected_api_url, selected_api_score = await fetch_simple_api_jobs(
            normalized_url,
            client=client,
            discovered_urls=discovered_urls,
        )
        if len(jobs_result) >= 3:
            jobs = jobs_result
            api_url = selected_api_url or ""
            final_site_type = "SIMPLE_API"
            final_strategy = "api"
            logger.info("[PIPELINE] simple_api success → exiting (jobs=%d)", len(jobs))
            return True
        elif len(jobs_result) > 0:
            jobs = jobs_result
            api_url = selected_api_url or ""
            final_site_type = "SIMPLE_API"
            final_strategy = "api"
            logger.info("[PIPELINE] simple_api found %d jobs (continuing pipeline)", len(jobs))
        return False

    async def _try_dom_scraper() -> bool:
        """DOM is the MAIN strategy. Run EARLY. Exit at >= 5 jobs."""
        nonlocal jobs, api_url, final_site_type, final_strategy, dom_jobs_count
        logger.info("[PIPELINE] Trying dom_scraper")
        # Try the DOM variant that was classified
        if site_type == "DOM_BROWSER":
            jobs_result = await scrape_dom_browser(normalized_url)
            dom_type = "DOM_BROWSER"
        elif site_type == "DOM_LOAD_MORE":
            jobs_result = await scrape_dom_load_more(normalized_url)
            dom_type = "DOM_LOAD_MORE"
        elif site_type == "DOM_INFINITE_SCROLL":
            jobs_result = await scrape_dom_infinite_scroll(normalized_url)
            dom_type = "DOM_INFINITE_SCROLL"
        else:
            # Default to browser mode
            jobs_result = await scrape_dom_browser(normalized_url)
            dom_type = "DOM_BROWSER"

        dom_jobs_count = len(jobs_result)

        if len(jobs_result) >= 5:
            # DOM found plenty → STOP. No dynamic_api, no interactive_dom.
            jobs = jobs_result
            api_url = ""
            final_site_type = dom_type
            final_strategy = "dom"
            logger.info("[PIPELINE] dom_scraper SUCCESS (jobs=%d ≥ 5) → stopping pipeline", len(jobs))
            return True
        elif len(jobs_result) >= 3:
            # DOM found enough → exit but allow dynamic_api as backup if needed
            jobs = jobs_result
            api_url = ""
            final_site_type = dom_type
            final_strategy = "dom"
            logger.info("[PIPELINE] dom_scraper found %d jobs (continuing to verify)", len(jobs))
            return True
        elif len(jobs_result) > 0:
            jobs = jobs_result
            api_url = ""
            final_site_type = dom_type
            final_strategy = "dom"
            logger.info("[PIPELINE] dom_scraper found %d jobs (< 3, will try dynamic_api)", len(jobs))
        else:
            logger.info("[PIPELINE] dom_scraper found 0 jobs")
        return False

    async def _try_dynamic_api() -> bool:
        """Only runs if DOM failed or returned very few jobs."""
        nonlocal jobs, api_url, final_site_type, final_strategy
        logger.info("[PIPELINE] Trying dynamic_api (DOM was insufficient)")

        # NEVER launch browser for dynamic_api detection — only use pre-detected API
        if dynamic_api_result.get("matched") and dynamic_api_result.get("api_usable"):
            # Validate the detected API URL is actually a job API
            api_url_detected = dynamic_api_result.get("api_url", "")
            if not _is_valid_job_api_url(api_url_detected):
                logger.info("[PIPELINE] dynamic_api REJECTED — non-job API URL: %s", api_url_detected)
                return False

            logger.info("[PIPELINE] dynamic_api direct (no browser): %s", api_url_detected)
            jobs_result = await scrape_dynamic_api_direct(
                api_url=api_url_detected,
                method=dynamic_api_result.get("method", "GET"),
                payload=dynamic_api_result.get("payload"),
                headers=dynamic_api_result.get("headers", {}),
                base_url=normalized_url,
            )
        else:
            # No valid pre-detected API → skip dynamic_api entirely (no browser)
            logger.info("[PIPELINE] dynamic_api SKIPPED — no valid API detected (no browser fallback)")
            return False

        if len(jobs_result) >= 5:
            jobs = jobs_result
            api_url = ""
            final_site_type = "DYNAMIC_API"
            final_strategy = "api"
            logger.info("[PIPELINE] dynamic_api SUCCESS (jobs=%d ≥ 5) → exiting", len(jobs))
            return True
        elif len(jobs_result) >= 3:
            jobs = jobs_result
            api_url = ""
            final_site_type = "DYNAMIC_API"
            final_strategy = "api"
            logger.info("[PIPELINE] dynamic_api found %d jobs (acceptable)", len(jobs))
            return True
        elif len(jobs_result) > 0:
            jobs = jobs_result
            api_url = ""
            final_site_type = "DYNAMIC_API"
            final_strategy = "api"
            logger.info("[PIPELINE] dynamic_api found %d jobs (< 3, continuing)", len(jobs))
        else:
            logger.info("[PIPELINE] dynamic_api found 0 jobs")
        return False

    async def _try_interactive_dom() -> bool:
        """STRICT LAST RESORT. Only if dom < 3 AND dynamic_api failed."""
        nonlocal jobs, api_url, final_site_type, final_strategy
        logger.info("[PIPELINE] Falling back to interactive_dom (LAST RESORT)")
        jobs_result = await scrape_interactive_dom(normalized_url)
        jobs = jobs_result
        api_url = ""
        final_site_type = "INTERACTIVE_DOM"
        final_strategy = "dom"
        if len(jobs_result) >= 3:
            logger.info("[PIPELINE] interactive_dom success (jobs=%d)", len(jobs))
        else:
            logger.info("[PIPELINE] interactive_dom result: %d jobs", len(jobs))
        return len(jobs_result) >= 3

    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        # Handle special API types first (Workday, Greenhouse)
        if site_type == "WORKDAY_API":
            api_url, jobs = await fetch_workday_jobs(
                normalized_url,
                client=client,
                html=page_html,
                discovered_urls=discovered_urls,
            )
            final_site_type = "WORKDAY_API"
            final_strategy = "api"
            logger.info("[PIPELINE] WORKDAY_API → %d jobs", len(jobs))
        elif site_type == "GREENHOUSE_API":
            jobs = await scrape_greenhouse(normalized_url, client=client)
            api_url = ""
            final_site_type = "GREENHOUSE_API"
            final_strategy = "api"
            logger.info("[PIPELINE] GREENHOUSE_API → %d jobs", len(jobs))
        else:
            # NEW ORDER: simple_api → dom_scraper → dynamic_api → interactive_dom
            if not await _try_simple_api():
                # DOM is main strategy — always run it
                if not await _try_dom_scraper():
                    # DOM found 0 jobs → try dynamic_api
                    if not await _try_dynamic_api():
                        # Both failed → LAST RESORT
                        await _try_interactive_dom()
                elif dom_jobs_count < 3:
                    # DOM found 1-2 jobs → try dynamic_api for more
                    if not await _try_dynamic_api():
                        # Still < 3 → last resort
                        await _try_interactive_dom()
            # If simple_api succeeded, pipeline already exited

    # ── For API sites: enrich jobs with full raw API data ─────────
    # This ensures detail extraction can use the complete API response
    # without needing to fetch HTML pages
    if strategy == "api" and site_type == "WORKDAY_API":
        # Re-fetch with raw data preservation
        jobs = await _fetch_workday_with_raw_data(
            normalized_url, client=None, html=page_html,
            discovered_urls=discovered_urls, api_url=api_url,
        )
        logger.info("[WORKDAY API] Enriched %d jobs with raw API data", len(jobs))

    saved_count = 0
    for job_data in jobs:
        title = str(job_data.get("title", "")).strip()
        job_url = str(job_data.get("url", "")).strip()
        if not title or not job_url:
            continue

        existing_job = await session.execute(
            select(Job).where(Job.site_id == site.id, Job.url == job_url)
        )
        job = existing_job.scalar_one_or_none()
        if job:
            job.title = title
            job.location = str(job_data.get("location", "")).strip()
            job.raw_json = job_data
            saved_count += 1
            continue

        job = Job(
            site_id=site.id,
            title=title,
            location=str(job_data.get("location", "")).strip(),
            url=job_url,
            raw_json=job_data,
        )
        session.add(job)
        saved_count += 1

    await session.commit()

    logger.info(f"Jobs -> {saved_count}")

    # Save raw JSON to folder
    if jobs:  # Only save if we found jobs (successful run)
        metadata = {
            "url": normalized_url,
            "confidence": confidence,
            "strategy": final_strategy,
            "api_url": api_url if final_strategy == "api" else "",
            "detection_results": payload["tests"],
        }
        saved_path = save_scrape_result(jobs, domain, final_site_type, metadata)
        if saved_path:
            logger.info(f"Raw JSON saved to: {saved_path}")

    return {
        "domain": domain,
        "type": final_site_type,
        "confidence": confidence,
        "jobs_found": len(jobs),
        "status": "success" if jobs else "failed",
        "strategy": final_strategy,
        "api_url": api_url if final_strategy == "api" else "",
    }


# ---------------------------------------------------------------------------
# Workday: fetch with raw API data preserved for detail extraction
# ---------------------------------------------------------------------------

async def _fetch_workday_with_raw_data(
    url: str,
    client: httpx.AsyncClient | None = None,
    html: str | None = None,
    discovered_urls: list[str] | None = None,
    api_url: str = "",
) -> list[dict]:
    """Fetch Workday jobs preserving full API response for each job."""
    from urllib.parse import urlparse
    from app.detectors.workday import (
        _build_workday_applied_facets,
        _normalize_workday_job,
    )

    if not api_url:
        # Fall back to standard fetch
        _, jobs = await fetch_workday_jobs(url, client=client, html=html, discovered_urls=discovered_urls)
        return jobs

    close_client = client is None
    if close_client:
        client = httpx.AsyncClient(timeout=20, follow_redirects=True)

    try:
        parsed = urlparse(url)
        tenant = parsed.netloc.split(".")[0]
        site = next((part for part in parsed.path.split("/") if part), "")
        if not tenant or not site:
            return []

        applied_facets = _build_workday_applied_facets(url)
        jobs: list[dict] = []
        seen_urls: set[str] = set()
        offset = 0
        limit = 20

        while True:
            response = await client.post(
                api_url,
                json={
                    "limit": limit,
                    "offset": offset,
                    "searchText": "",
                    "appliedFacets": applied_facets,
                },
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
            response.raise_for_status()
            payload = response.json()
            postings = payload.get("jobPostings") or []
            if not postings:
                break

            added = 0
            for posting in postings:
                normalized = _normalize_workday_job(posting, tenant, site)
                if not normalized:
                    continue
                job_url = normalized["url"].lower()
                if job_url in seen_urls:
                    continue
                seen_urls.add(job_url)

                # Attach full raw API response for detail extraction
                normalized["_raw_api"] = posting
                jobs.append(normalized)
                added += 1

            if added == 0:
                break
            offset += limit

        return jobs
    finally:
        if close_client:
            await client.aclose()
