"""Accenture job detail parser for the RAD jobdetails page structure."""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

from app.core.logger import logger


def parse_accenture_html(html: str) -> dict:
    """Parse Accenture-specific job detail pages."""
    empty = _empty_result()
    if not html or "rad-job-details__wrapper" not in html:
        return empty

    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception as exc:
        logger.warning("[AccentureParser] Failed to parse HTML: %s", exc)
        return empty

    wrapper = soup.select_one(".rad-job-details__wrapper")
    hero = soup.select_one(".rad-job-details-hero")
    if not wrapper or not hero:
        return empty

    title = _text(hero.select_one(".rad-job-details-hero__title"))
    company_name = "Accenture"
    location = _first_non_empty(
        _clean_attr(wrapper.get("data-joblocation")),
        _extract_hero_location(hero),
        _accordion_text(soup, "locations"),
    )
    employment_type = _first_non_empty(
        _clean_attr(wrapper.get("data-employeetype")),
        _extract_employment_type(hero),
    )
    experience = _first_non_empty(
        _extract_years_of_experience(wrapper),
        _extract_years_from_description(_accordion_text(soup, "job-description")),
    )
    job_id = _clean_attr(wrapper.get("data-jobid"))
    education = _first_non_empty(
        _accordion_text(soup, "qualification"),
        _extract_labeled_value(_accordion_text(soup, "job-description"), "Qualifications"),
    )

    job_description = _accordion_text(soup, "job-description")
    about_company = _accordion_text(soup, "about-accenture")
    skills = _extract_skills(wrapper, job_description)
    qualifications = _extract_bullets_after_label(job_description, "What are we looking for?")
    responsibilities = _extract_bullets_after_label(job_description, "Roles and Responsibilities:")

    return {
        "title": title or None,
        "company_name": company_name,
        "location": location or None,
        "description": job_description or None,
        "salary": None,
        "experience": experience or None,
        "employment_type": employment_type or None,
        "posted_on": None,
        "skills": skills,
        "job_id": job_id or None,
        "education": education or None,
        "qualifications": qualifications,
        "key_responsibilities": responsibilities,
        "about_company": about_company or None,
    }


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
        "job_id": None,
        "education": None,
        "qualifications": [],
        "key_responsibilities": [],
        "about_company": None,
    }


def _accordion_text(soup: BeautifulSoup, suffix: str) -> str:
    node = soup.select_one(f"[id$='-{suffix}-content'] .rad-accordion-atom__content")
    return _text(node, separator="\n")


def _extract_hero_location(hero: BeautifulSoup) -> str:
    row = _text(hero.select_one(".job-data__row--two"))
    if not row:
        return ""
    parts = [p.strip() for p in row.split("|") if p.strip()]
    if len(parts) >= 2:
        return parts[1]
    return ""


def _extract_employment_type(hero: BeautifulSoup) -> str:
    row = _text(hero.select_one(".job-data__row--one"))
    if not row:
        return ""
    if "|" not in row:
        return ""
    parts = [p.strip() for p in row.split("|") if p.strip()]
    return parts[1] if len(parts) >= 2 else ""


def _extract_years_of_experience(wrapper: BeautifulSoup) -> str:
    raw = _clean_attr(wrapper.get("data-jobyearsofexperience"))
    if not raw:
        return ""
    match = re.search(r"(\d+\s*[-–to]+\s*\d+\s*years|\d+\+?\s*years)", raw, re.IGNORECASE)
    return match.group(1).strip() if match else raw


def _extract_years_from_description(text: str) -> str:
    if not text:
        return ""
    match = re.search(r"Years of Experience:\s*([^\n<]+)", text, re.IGNORECASE)
    return match.group(1).strip() if match else ""


def _extract_labeled_value(text: str, label: str) -> str:
    if not text:
        return ""
    match = re.search(rf"{re.escape(label)}:\s*([^\n]+)", text, re.IGNORECASE)
    return match.group(1).strip() if match else ""


def _extract_skills(wrapper: BeautifulSoup, description: str) -> list[str]:
    skills: list[str] = []
    primary_skill = _clean_attr(wrapper.get("data-jobskill"))
    if primary_skill:
        skills.append(primary_skill)

    labeled_skill = _extract_labeled_value(description, "Skill required")
    if labeled_skill:
        skills.extend([part.strip() for part in re.split(r"[,/]", labeled_skill) if part.strip()])

    candidates = [
        "Accounting", "Auditing", "Financial Statements", "Books of Accounts",
        "NetSuite", "Excel", "PPT", "ERP", "US GAAP", "Indian GAAP",
        "Consolidation", "Balance Sheet Account Reconciliations",
    ]
    lowered = description.lower()
    for candidate in candidates:
        if candidate.lower() in lowered:
            skills.append(candidate)

    return _dedupe(skills)


def _extract_bullets_after_label(text: str, label: str) -> list[str]:
    if not text or label.lower() not in text.lower():
        return []

    start = text.lower().find(label.lower())
    snippet = text[start + len(label):]
    next_heading = re.search(
        r"\n(?:About Accenture|What would you do\?|What are we looking for\?|Roles and Responsibilities:|Important Notice)\b",
        snippet,
        re.IGNORECASE,
    )
    if next_heading:
        snippet = snippet[: next_heading.start()]

    lines = []
    for raw_line in snippet.splitlines():
        line = re.sub(r"^[•\-\u2022\s]+", "", raw_line).strip()
        if len(line) < 3:
            continue
        lines.append(line)
    return _dedupe(lines)


def _text(node, separator: str = " ") -> str:
    if not node:
        return ""
    return re.sub(r"\s+", " ", node.get_text(separator=separator, strip=True)).strip()


def _clean_attr(value: str | None) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _first_non_empty(*values: str) -> str:
    for value in values:
        if value:
            return value
    return ""


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        clean = value.strip()
        if not clean:
            continue
        key = clean.lower()
        if key in seen:
            continue
        seen.add(key)
        output.append(clean)
    return output
