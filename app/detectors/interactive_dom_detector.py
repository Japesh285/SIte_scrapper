"""INTERACTIVE_DOM detection — identifies pages requiring user interaction.

This detector evaluates pages that:
1. Load minimal content initially
2. Require user actions (clicks, scrolls, searches) to reveal job listings
3. Do NOT have a usable API (DYNAMIC_API score < 5)

Detection is deterministic via DOM change analysis and job signal detection.
"""

from __future__ import annotations

import re

from app.core.logger import logger
from app.detectors.browser_probe import BrowserProbeResult

# Job signal keywords (case-insensitive matching)
_JOB_SIGNAL_KEYWORDS = [
    "job", "position", "location", "apply", "responsibilities",
    "requirements", "qualifications", "experience", "department",
    "career", "opening", "openings", "role", "team",
]

# Job link patterns (href matching)
_JOB_LINK_PATTERNS = [
    r"/job[sS]?",
    r"/position[sS]?",
    r"/career[sS]?",
    r"/opening[sS]?",
    r"/apply",
    r"job_id",
    r"requisition",
    r"/careers/.*-\w+",
]


def dom_has_job_signals(html: str) -> int:
    """Count job-related keyword hits in HTML.

    Parameters
    ----------
    html : str
        Full HTML content.

    Returns
    -------
    int — number of distinct job keywords found.
    """
    text = html.lower()
    return sum(1 for kw in _JOB_SIGNAL_KEYWORDS if kw in text)


def count_job_links(html: str) -> int:
    """Count anchor elements with job-related href patterns.

    Parameters
    ----------
    html : str
        Full HTML content.

    Returns
    -------
    int — number of job-like links.
    """
    # Extract all href attributes
    hrefs = re.findall(r'href=["\']([^"\']+)["\']', html, re.IGNORECASE)

    # Count those matching job patterns
    job_link_count = 0
    for href in hrefs:
        if any(re.search(pattern, href, re.IGNORECASE) for pattern in _JOB_LINK_PATTERNS):
            job_link_count += 1

    return job_link_count


def detect_interactive_dom(probe: BrowserProbeResult) -> dict:
    """Detect INTERACTIVE_DOM from browser probe results.

    Evaluation steps:
    1. Check if DOM changed significantly after interactions (>30% growth)
    2. Check if final HTML contains job signals (≥3 keywords)
    3. Check if job links count >= 3
    4. Final rule: dom_changed AND (has_job_signals OR job_links >= 3)

    Parameters
    ----------
    probe : BrowserProbeResult
        Result from run_browser_probe().

    Returns
    -------
    dict with keys:
        - matched: bool — True if INTERACTIVE_DOM detected
        - jobs_found: int — estimated job count from links
        - api_usable: bool — True if DOM has sufficient job content
        - dom_changed: bool — True if final_html > initial_html * 1.3
        - initial_html_length: int
        - final_html_length: int
        - job_signal_count: int — keyword hits in final HTML
        - job_links_count: int — job-like anchor count
        - interactions_performed: list[str]
        - confidence: float — 0-1 confidence score
    """
    if not probe.available:
        logger.info("[InteractiveDOM] Browser probe not available")
        return _empty_result()

    initial_len = len(probe.initial_html)
    final_len = len(probe.final_html)

    # Step 1: Detect DOM change
    dom_changed = final_len > initial_len * 1.3 if initial_len > 0 else False

    # Step 2: Detect job signals in final HTML
    job_signal_count = dom_has_job_signals(probe.final_html)

    # Step 3: Detect job links
    job_links_count = count_job_links(probe.final_html)

    # Use probe's job_links_count as well (from live DOM query)
    probe_job_links = probe.job_links_count
    effective_job_links = max(job_links_count, probe_job_links)

    logger.info(
        "[InteractiveDOM] DOM change: %s (%d → %d chars, %.1f%% growth)",
        dom_changed,
        initial_len,
        final_len,
        ((final_len - initial_len) / initial_len * 100) if initial_len > 0 else 0,
    )
    logger.info(
        "[InteractiveDOM] Job signals: %d keywords, %d job links (probe: %d)",
        job_signal_count,
        effective_job_links,
        probe_job_links,
    )
    logger.info(
        "[InteractiveDOM] Interactions performed: %s",
        probe.interactions_performed,
    )

    # Final detection rule
    has_job_signals = job_signal_count >= 3
    has_sufficient_links = effective_job_links >= 3

    matched = dom_changed and (has_job_signals or has_sufficient_links)

    if not matched:
        logger.info(
            "[InteractiveDOM] NO MATCH — dom_changed=%s, has_job_signals=%s, "
            "job_links=%d",
            dom_changed,
            has_job_signals,
            effective_job_links,
        )
        return {
            **_empty_result(),
            "dom_changed": dom_changed,
            "initial_html_length": initial_len,
            "final_html_length": final_len,
            "job_signal_count": job_signal_count,
            "job_links_count": effective_job_links,
            "interactions_performed": probe.interactions_performed,
        }

    # Confidence formula based on DOM change magnitude and job signal strength
    growth_factor = min(0.4, (final_len - initial_len) / max(initial_len, 1) * 0.1)
    signal_factor = min(0.3, job_signal_count * 0.03)
    link_factor = min(0.3, effective_job_links * 0.02)
    confidence = min(0.95, 0.40 + growth_factor + signal_factor + link_factor)

    # Estimate job count from links
    jobs_found = effective_job_links

    logger.info(
        "[InteractiveDOM] MATCH — confidence=%.2f, jobs_found=%d, "
        "dom_changed=%s, growth=%.1f%%",
        confidence,
        jobs_found,
        dom_changed,
        ((final_len - initial_len) / initial_len * 100) if initial_len > 0 else 0,
    )

    return {
        "matched": True,
        "jobs_found": jobs_found,
        "api_usable": True,
        "dom_changed": dom_changed,
        "initial_html_length": initial_len,
        "final_html_length": final_len,
        "job_signal_count": job_signal_count,
        "job_links_count": effective_job_links,
        "interactions_performed": probe.interactions_performed,
        "confidence": confidence,
    }


def _empty_result() -> dict:
    return {
        "matched": False,
        "jobs_found": 0,
        "api_usable": False,
        "dom_changed": False,
        "initial_html_length": 0,
        "final_html_length": 0,
        "job_signal_count": 0,
        "job_links_count": 0,
        "interactions_performed": [],
        "confidence": 0.0,
    }
