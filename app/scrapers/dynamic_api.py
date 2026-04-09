"""DYNAMIC_API scraper — imports dynamic.py directly, wraps for async."""

from __future__ import annotations

import sys
import os
import asyncio

import httpx

from app.core.logger import logger

# ── Import dynamic.py directly from project root ──────────────────
_DYNAMIC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _DYNAMIC_DIR not in sys.path:
    sys.path.insert(0, _DYNAMIC_DIR)

import dynamic  # type: ignore


async def scrape_dynamic_api_direct(
    api_url: str,
    method: str,
    payload: dict | str | None,
    headers: dict,
    base_url: str,
    max_pages: int = 20,
) -> list[dict]:
    """Run full dynamic.py pipeline: capture → paginate → AI enrich."""
    from app.detectors.dynamic_api_detector import detect_dynamic_api

    logger.info("[DynamicAPI Scraper] Starting full pipeline for %s", base_url)

    detection = await detect_dynamic_api(base_url)

    raw_jobs = detection.get("raw_jobs", [])
    if not raw_jobs:
        logger.warning("[DynamicAPI Scraper] No raw jobs from detector")
        raw_jobs = await _fetch_fallback(api_url, method, payload, headers, max_pages)

    if not raw_jobs:
        return []

    logger.info("[DynamicAPI Scraper] Enriching %d jobs with AI", len(raw_jobs))

    enriched = []
    total_tokens = 0

    for i, job in enumerate(raw_jobs, 1):
        logger.info("[DynamicAI] [%d/%d] %s", i, len(raw_jobs), job.get("title", "Untitled")[:60])

        result = await _enrich_with_ai(job)
        enriched.append(result)

        usage = result.get("ai_usage", {})
        tokens = usage.get("total_tokens", 0)
        total_tokens += tokens
        logger.info("[DynamicAI] Done | tokens=%d | total=%d", tokens, total_tokens)

        if i < len(raw_jobs):
            await asyncio.sleep(dynamic.OPENAI_DELAY)

    logger.info("[DynamicAPI Scraper] Complete — %d jobs, %d tokens", len(enriched), total_tokens)
    return enriched


async def _enrich_with_ai(job: dict) -> dict:
    """Run dynamic.py's enrich_job_with_ai in a thread executor."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: dynamic.enrich_job_with_ai(job))


async def _fetch_fallback(
    api_url: str,
    method: str,
    payload: dict | str | None,
    headers: dict,
    max_pages: int,
) -> list[dict]:
    """Sync fallback wrapped in executor."""
    loop = asyncio.get_event_loop()

    def _do():
        api = {
            "url": api_url,
            "method": method,
            "headers": headers,
            "payload": payload,
            "cookies": [],
        }
        return dynamic.paginate_universal(api, max_pages=max_pages)

    return await loop.run_in_executor(None, _do)


async def scrape_dynamic_api(url: str) -> list[dict]:
    """Legacy entry point — runs full pipeline."""
    from app.detectors.dynamic_api_detector import detect_dynamic_api

    detection = await detect_dynamic_api(url)
    if not detection.get("matched") or not detection.get("api_usable"):
        logger.warning("[DynamicAPI] No usable API for %s", url)
        return []

    return await scrape_dynamic_api_direct(
        api_url=detection.get("api_url", ""),
        method=detection.get("method", "GET"),
        payload=detection.get("payload"),
        headers=detection.get("headers", {}),
        base_url=url,
    )
