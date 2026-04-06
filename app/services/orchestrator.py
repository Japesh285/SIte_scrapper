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
from app.scrapers.greenhouse import scrape_greenhouse
from app.services.raw_json_saver import save_scrape_result
from app.db.models import Site, Job
from app.core.logger import logger


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

        workday_result = await detect_workday(
            normalized_url,
            client=client,
            html=page_html,
        )
        logger.info(f"Workday -> {workday_result}")

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

        simple_api_result = await detect_simple_api(normalized_url, client=client)
        logger.info(f"Simple API -> {simple_api_result}")
        dom_browser_result = await detect_dom_browser(normalized_url, html=page_html)
        logger.info(f"DOM Browser -> {dom_browser_result}")
        dom_load_more_result = await detect_dom_load_more(normalized_url, html=page_html)
        logger.info(f"DOM Load More -> {dom_load_more_result}")
        dom_infinite_scroll_result = await detect_dom_infinite_scroll(normalized_url, html=page_html)
        logger.info(f"DOM Infinite Scroll -> {dom_infinite_scroll_result}")

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

    # Build payload for AI classifier
    payload = {
        "domain": domain,
        "url": normalized_url,
        "tests": {
            "workday": workday_result,
            "greenhouse": greenhouse_result,
            "simple_api": simple_api_result,
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

    # ── Scrape with strategy guard ────────────────────────────────
    # API sites: preserve full API response data in each job dict
    # DOM sites: normal scraping, HTML detail extraction later
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        if site_type == "WORKDAY_API":
            api_url, jobs = await fetch_workday_jobs(
                normalized_url,
                client=client,
                html=page_html,
                discovered_urls=discovered_urls,
            )
            # For API sites: attach raw API data to each job for detail extraction
            logger.info("[BLOCK HTML SCRAPING] strategy=api for %s", domain)
        elif site_type == "GREENHOUSE_API":
            jobs = await scrape_greenhouse(normalized_url, client=client)
            logger.info("[BLOCK HTML SCRAPING] strategy=api for %s", domain)
        elif site_type == "SIMPLE_API":
            jobs, selected_api_url, selected_api_score = await fetch_simple_api_jobs(
                normalized_url,
                client=client,
                discovered_urls=discovered_urls,
            )
            api_url = selected_api_url or ""
            logger.info(
                "Simple API selected for scraping -> url=%s score=%s",
                selected_api_url or "",
                selected_api_score,
            )
            logger.info("[BLOCK HTML SCRAPING] strategy=api for %s", domain)
        elif site_type == "DOM_BROWSER":
            jobs = await scrape_dom_browser(normalized_url)
            api_url = ""
        elif site_type == "DOM_LOAD_MORE":
            jobs = await scrape_dom_load_more(normalized_url)
            api_url = ""
        elif site_type == "DOM_INFINITE_SCROLL":
            jobs = await scrape_dom_infinite_scroll(normalized_url)
            api_url = ""
        else:
            jobs = []
            api_url = ""

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
            "strategy": strategy,
            "api_url": api_url if strategy == "api" else "",
            "detection_results": payload["tests"],
        }
        saved_path = save_scrape_result(jobs, domain, site_type, metadata)
        if saved_path:
            logger.info(f"Raw JSON saved to: {saved_path}")

    return {
        "domain": domain,
        "type": site_type,
        "confidence": confidence,
        "jobs_found": len(jobs),
        "status": "success" if jobs else "failed",
        "strategy": strategy,
        "api_url": api_url if strategy == "api" else "",
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
