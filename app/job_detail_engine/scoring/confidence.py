"""Confidence scoring for extracted job data."""


def score(job: dict) -> int:
    """Return integer confidence score for a partially-filled job dict.

    Scoring rules:
        title present          → +2
        location present       → +1
        description > 300 ch   → +2
        salary present         → +1
        skills (non-empty)     → +1
        experience present     → +1

    Max score = 8.  A score < 4 triggers AI fallback.
    """
    s = 0

    if job.get("title"):
        s += 2
    if job.get("location"):
        s += 1
    desc = job.get("description") or ""
    if len(desc) > 300:
        s += 2
    if job.get("salary"):
        s += 1
    if job.get("skills"):
        s += 1
    if job.get("experience"):
        s += 1

    return s
