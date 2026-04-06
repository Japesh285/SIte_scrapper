"""Parse JobPosting data from <script type="application/ld+json"> blocks."""

import json
from typing import Optional

from bs4 import BeautifulSoup

from app.core.logger import logger


def parse_json_ld(html: str) -> dict:
    """Extract structured job data from JSON-LD script tags.

    Returns dict with keys:
        title, company_name, location, description, salary,
        experience, employment_type, posted_on, skills
    All values are str | list | None.  Never raises.
    """
    empty = _empty_result()

    if not html or not html.strip():
        return empty

    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception as exc:
        logger.warning("[JSON-LD] Failed to parse HTML: %s", exc)
        return empty

    for script in soup.find_all("script", type="application/ld+json"):
        try:
            raw = script.string or ""
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError, AttributeError) as exc:
            logger.debug("[JSON-LD] Skipping invalid script: %s", exc)
            continue

        items = _normalise_to_list(data)
        for item in items:
            job = _find_job_posting(item)
            if job is not None:
                return _extract_fields(job)

    return empty


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _empty_result() -> dict:
    return {
        "title": None,
        "company_name": None,
        "location": None,
        "description": None,
        "salary": None,
        "experience": None,
        "employment_type": None,
        "posted_on": None,
        "skills": [],
    }


def _normalise_to_list(data: object) -> list:
    """Wrap dicts in a list so we can iterate uniformly."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return [data]
    return []


def _find_job_posting(item: object) -> Optional[dict]:
    """Recursively find a dict with @type == JobPosting."""
    if not isinstance(item, dict):
        return None

    tp = str(item.get("@type", ""))
    if "JobPosting" in tp:
        return item

    # Check @graph arrays (common in Google-style markup)
    graph = item.get("@graph")
    if isinstance(graph, list):
        for child in graph:
            result = _find_job_posting(child)
            if result:
                return result

    return None


def _extract_fields(job: dict) -> dict:
    """Map JSON-LD fields to our canonical schema."""

    # Title
    title = _safe_str(job.get("title"))

    # Company
    company = job.get("hiringOrganization")
    company_name: Optional[str] = None
    if isinstance(company, dict):
        company_name = _safe_str(company.get("name")) or _safe_str(company.get("@id"))
    elif company:
        company_name = _safe_str(company)

    # Location
    location = _extract_location(job)

    # Description
    description = _safe_str(job.get("description"))

    # Salary
    salary = _extract_salary(job)

    # Experience
    experience = _safe_str(job.get("experienceRequirements")) or _safe_str(job.get("baseSalary"))

    # Employment type
    emp_type = job.get("employmentType")
    employment_type = _safe_str(emp_type).lower() if emp_type else None

    # Posted date
    posted_on = _safe_str(job.get("datePosted")) or _safe_str(job.get("validThrough"))

    # Skills
    skills = _extract_skills(job)

    return {
        "title": title or None,
        "company_name": company_name or None,
        "location": location or None,
        "description": description or None,
        "salary": salary or None,
        "experience": experience or None,
        "employment_type": employment_type or None,
        "posted_on": posted_on or None,
        "skills": skills,
    }


def _extract_location(job: dict) -> Optional[str]:
    loc_raw = job.get("jobLocation")
    if isinstance(loc_raw, dict):
        addr = loc_raw.get("address", {})
        if isinstance(addr, dict):
            parts = [
                _safe_str(addr.get("addressLocality")),
                _safe_str(addr.get("addressRegion")),
                _safe_str(addr.get("addressCountry")),
            ]
            joined = ", ".join(p for p in parts if p)
            if joined:
                return joined
        name = _safe_str(loc_raw.get("name"))
        if name:
            return name
    elif isinstance(loc_raw, str) and loc_raw.strip():
        return loc_raw.strip()
    return None


def _extract_salary(job: dict) -> Optional[str]:
    salary_data = job.get("baseSalary")
    if not salary_data:
        return None
    if isinstance(salary_data, dict):
        value = salary_data.get("value", {})
        if isinstance(value, dict):
            min_v = value.get("minValue")
            max_v = value.get("maxValue")
            currency = salary_data.get("currency", "")
            parts = [str(p) for p in [min_v, max_v] if p]
            if parts:
                return f"{currency} {'-'.join(parts)}".strip()
        txt = _safe_str(salary_data.get("text"))
        if txt:
            return txt
    return None


def _extract_skills(job: dict) -> list[str]:
    skills: list[str] = []
    # Schema.org skills field
    skill_field = job.get("skills")
    if isinstance(skill_field, list):
        for s in skill_field:
            s_str = _safe_str(s)
            if s_str:
                skills.append(s_str)
    elif isinstance(skill_field, str):
        # Comma-separated string
        skills = [s.strip() for s in skill_field.split(",") if s.strip()]
    return skills


def _safe_str(val: object) -> str:
    if val is None:
        return ""
    return str(val).strip()
