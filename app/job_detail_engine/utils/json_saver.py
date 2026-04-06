"""Save full job detail JSON responses to disk."""

import json
from datetime import datetime
from pathlib import Path

from app.core.logger import logger

JOB_DETAILS_DIR = Path("job-details")


def save_job_details(
    jobs: list[dict],
    domain: str,
    site_type: str = "",
    listing_jobs_found: int = 0,
    listing_status: str = "",
) -> str | None:
    """Save the complete scrape-details response as a timestamped JSON file.

    Path: job-details/{domain}/full_json/scrape_result_{YYYYMMDD_HHMMSS}.json

    Returns the saved file path or None on failure.
    """
    try:
        domain_dir = JOB_DETAILS_DIR / domain / "full_json"
        domain_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"scrape_result_{timestamp}.json"
        file_path = domain_dir / filename

        output = {
            "domain": domain,
            "site_type": site_type,
            "listing_jobs_found": listing_jobs_found,
            "listing_status": listing_status,
            "saved_at": datetime.now().isoformat(),
            "jobs_count": len(jobs),
            "jobs": jobs,
        }

        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(output, f, indent=2, ensure_ascii=False, default=str)

        logger.info("[JobDetails] Saved %d jobs to %s", len(jobs), file_path)
        return str(file_path)

    except Exception as exc:
        logger.error("[JobDetails] Failed to save job details for %s: %s", domain, exc)
        return None
