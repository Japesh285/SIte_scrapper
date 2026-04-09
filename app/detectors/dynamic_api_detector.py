"""DYNAMIC_API detection — imports dynamic.py directly, wraps for async."""

from __future__ import annotations

import sys
import os
import asyncio

from app.core.logger import logger
from app.core.site_utils import normalize_site_url

# ── Import dynamic.py directly from project root ──────────────────
_DYNAMIC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _DYNAMIC_DIR not in sys.path:
    sys.path.insert(0, _DYNAMIC_DIR)

import dynamic  # type: ignore


async def detect_dynamic_api(url: str) -> dict:
    """Detect hidden job APIs using dynamic.py's capture + pagination pipeline."""
    normalized_url = normalize_site_url(url)

    # Step 1: capture_apis_universal (sync)
    loop = asyncio.get_event_loop()
    apis = await loop.run_in_executor(None, lambda: dynamic.capture_apis_universal(normalized_url))
    if not apis:
        logger.info("[DynamicAPI] No APIs captured for %s", normalized_url)
        return _empty_result()

    # Step 2: score and select
    ranked = sorted(apis, key=dynamic.score_api_universal, reverse=True)
    best = ranked[0]
    best_score = dynamic.score_api_universal(best)

    if best_score < 5:
        logger.info("[DynamicAPI] Best API score %d < 5 — not valid", best_score)
        return _empty_result()

    valid_count = len([j for j in best["jobs"] if dynamic.is_valid_job(j)])
    logger.info(
        "[DynamicAPI] Best API: url=%s method=%s score=%d valid=%d",
        best["url"][:80], best["method"], best_score, valid_count,
    )

    # Step 3: paginate_universal (sync)
    all_jobs = await loop.run_in_executor(None, lambda: dynamic.paginate_universal(best))
    jobs_found = len(all_jobs)

    if jobs_found == 0:
        logger.info("[DynamicAPI] Pagination returned 0 jobs")
        return _empty_result()

    logger.info("[DynamicAPI] Total jobs after pagination: %d", jobs_found)

    confidence = min(0.95, 0.50 + (best_score * 0.05) + min(jobs_found, 25) * 0.01)

    return {
        "matched": True,
        "jobs_found": jobs_found,
        "api_usable": True,
        "api_url": best["url"],
        "method": best["method"],
        "payload": best.get("payload"),
        "headers": best.get("headers", {}),
        "cookies": best.get("cookies", []),
        "confidence": confidence,
        "score": best_score,
        "raw_jobs": all_jobs,
    }


def _empty_result() -> dict:
    return {
        "matched": False,
        "jobs_found": 0,
        "api_usable": False,
        "api_url": "",
        "method": "",
        "payload": None,
        "headers": {},
        "cookies": [],
        "confidence": 0.0,
        "score": 0,
        "raw_jobs": [],
    }


def detect_dynamic_api_from_probe(probe) -> dict:
    """Legacy stub — use detect_dynamic_api instead."""
    return _empty_result()
