"""Save AI input payloads for debugging and audit."""

import re
from pathlib import Path
from datetime import datetime

from app.core.logger import logger

AI_PAYLOAD_DIR = Path("ai-data")


def save_ai_payload(payload: str, domain: str, job_url: str = "") -> str | None:
    """Save the exact text sent to AI for a job detail extraction.

    Directory structure: ai-data/{domain}/data/
    Filename: ai_payload_{timestamp}.txt

    Parameters
    ----------
    payload : str
        The text content sent to AI (output of prepare_ai_payload).
    domain : str
        The site domain (used for directory structure).
    job_url : str
        Optional job URL (used for logging).

    Returns
    -------
    str or None
        Path to the saved file, or None on failure.
    """
    if not payload or not domain:
        return None

    try:
        # Sanitize domain for filesystem use
        safe_domain = re.sub(r"[^a-z0-9._-]", "_", domain.lower())

        # Create directory: ai-data/{domain}/data/
        data_dir = AI_PAYLOAD_DIR / safe_domain / "data"
        data_dir.mkdir(parents=True, exist_ok=True)

        # Generate filename with timestamp
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"ai_payload_{timestamp}.txt"
        file_path = data_dir / filename

        # Write payload as plain text
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(payload)

        logger.info(
            "[AI PAYLOAD SAVED] path=%s length=%d url=%s",
            file_path,
            len(payload),
            job_url or "unknown",
        )
        return str(file_path)

    except Exception as exc:
        logger.warning("[AI PAYLOAD SAVED] Failed to save: %s", exc)
        return None
