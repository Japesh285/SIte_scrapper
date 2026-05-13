"""
Batch detection test — reads URLs from Bank Job Data.xlsx and runs the detector
pipeline against each one, printing a summary table without needing the DB or
the full FastAPI server.

Usage:
    python run_batch_detection_test.py
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import httpx
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

from app.detectors.workday import detect_workday
from app.detectors.greenhouse import detect_greenhouse
from app.detectors.taleo import detect_taleo
from app.detectors.icims import detect_icims
from app.detectors.wp_jobs import detect_wp_jobs
from app.detectors.simple_api import detect_simple_api
from app.detectors.smartrecruiters import detect_smartrecruiters
from app.detectors.oracle_hcm import detect_oracle_hcm
from app.core.site_utils import get_domain

XLSX_PATH = "/home/japesh/Dev/scrapper/SIte_scrapper/Bank Job Data.xlsx"
URL_COLUMN = "Exact India Jobs Link"

# HTTP-only detectors only — Phenom and SAP_SF use Playwright and are excluded
# from this fast batch test.
DETECTORS = [
    ("WORKDAY_API",       detect_workday),
    ("GREENHOUSE_API",    detect_greenhouse),
    ("TALEO_API",         detect_taleo),
    ("ICIMS_API",         detect_icims),
    ("SMARTRECRUITERS",   detect_smartrecruiters),
    ("ORACLE_HCM",        detect_oracle_hcm),
    ("WP_JOBS",           detect_wp_jobs),
    ("SIMPLE_API",        detect_simple_api),
]

PRIORITY = {
    "WORKDAY_API":     8,
    "GREENHOUSE_API":  7,
    "TALEO_API":       6,
    "ICIMS_API":       5,
    "SMARTRECRUITERS": 4,
    "ORACLE_HCM":      5,
    "WP_JOBS":         3,
    "SIMPLE_API":      2,
    "UNKNOWN":         0,
}


async def detect_url(url: str, client: httpx.AsyncClient) -> tuple[str, int, str]:
    """Returns (site_type, jobs_found, note)."""
    best_type = "UNKNOWN"
    best_jobs = 0
    best_priority = 0
    note = ""

    for label, detector in DETECTORS:
        try:
            result = await detector(url, client=client)
        except Exception as exc:
            continue

        if result.get("matched") and result.get("api_usable"):
            jobs = result.get("jobs_found", 0)
            priority = PRIORITY.get(label, 0)
            if priority > best_priority or (priority == best_priority and jobs > best_jobs):
                best_type = label
                best_jobs = jobs
                best_priority = priority
                # Capture any extra notes
                if result.get("reason"):
                    note = result["reason"]

        # Also capture informative non-matched results
        elif result.get("matched") and not result.get("api_usable") and best_type == "UNKNOWN":
            reason = result.get("reason", "")
            if reason and note == "":
                # e.g. workday_url_pattern_only, oracle_hcm_not_supported
                best_type = label
                best_jobs = 0
                note = reason

    return best_type, best_jobs, note


async def run_all(urls: list[str]) -> list[dict]:
    results = []
    timeout = httpx.Timeout(connect=10, read=60, write=10, pool=10)

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        for i, url in enumerate(urls, 1):
            domain = get_domain(url) or url
            t0 = time.time()
            try:
                site_type, jobs, note = await asyncio.wait_for(
                    detect_url(url, client), timeout=90
                )
            except asyncio.TimeoutError:
                site_type, jobs, note = "TIMEOUT", 0, "hard_cap_90s"
            except Exception as exc:
                site_type, jobs, note = "ERROR", 0, str(exc)[:60]
            elapsed = time.time() - t0

            note_str = f" {note}" if note else ""
            print(
                f"{i:3d}  [{site_type:<18}] {domain:<40} jobs={jobs:>4} t={elapsed:.1f}s{note_str}"
            )
            results.append({
                "index": i,
                "url": url,
                "domain": domain,
                "type": site_type,
                "jobs_found": jobs,
                "note": note,
                "elapsed_s": round(elapsed, 1),
            })

    return results


def load_urls() -> list[str]:
    df = pd.read_excel(XLSX_PATH)
    if URL_COLUMN not in df.columns:
        raise ValueError(f"Column '{URL_COLUMN}' not found. Available: {list(df.columns)}")
    urls = []
    seen: set[str] = set()
    for val in df[URL_COLUMN].dropna():
        url = str(val).strip()
        if url and url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(line_buffering=True)

    urls = load_urls()
    print(f"Loaded {len(urls)} URLs from {XLSX_PATH}")
    print()

    results = asyncio.run(run_all(urls))

    print()
    print("Done. Results saved.")
    print()

    # Summary counts
    from collections import Counter
    counts = Counter(r["type"] for r in results)
    working = sum(r["jobs_found"] for r in results)
    print("=== SUMMARY ===")
    for t, c in sorted(counts.items(), key=lambda x: -x[1]):
        total_jobs = sum(r["jobs_found"] for r in results if r["type"] == t)
        print(f"  {t:<20} {c:>3} sites   {total_jobs:>6} jobs")
    print(f"\n  Total jobs found across all sites: {working}")
