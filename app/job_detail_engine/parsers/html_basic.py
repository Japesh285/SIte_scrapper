"""Extract job data from raw HTML using BeautifulSoup + regex fallbacks."""

import re
from typing import Optional

from bs4 import BeautifulSoup

from app.core.logger import logger


def parse_html_basic(html: str) -> dict:
    """Parse basic job fields from HTML structure.

    Returns dict with keys:
        title, company_name, location, description, salary,
        experience, employment_type, posted_on, skills
    """
    empty = _empty_result()

    if not html or not html.strip():
        return empty

    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception as exc:
        logger.warning("[HTML] Failed to parse HTML: %s", exc)
        return empty

    # Remove noisy elements
    for tag in soup(["script", "style", "noscript", "iframe"]):
        tag.decompose()

    visible_text = soup.get_text(separator="\n", strip=True)

    title = _extract_title(soup, visible_text)
    company = _extract_company(soup, visible_text)
    location = _extract_location(soup, visible_text)
    description = _extract_description(soup, visible_text)
    salary = _extract_salary(visible_text)
    experience = _extract_experience(visible_text)
    employment_type = _extract_employment_type(visible_text)
    posted_on = _extract_posted_date(visible_text)
    skills = _extract_skills(visible_text)

    return {
        "title": title or None,
        "company_name": company or None,
        "location": location or None,
        "description": description or None,
        "salary": salary or None,
        "experience": experience or None,
        "employment_type": employment_type or None,
        "posted_on": posted_on or None,
        "skills": skills,
    }


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


def _extract_title(soup: BeautifulSoup, text: str) -> Optional[str]:
    # Try h1 first
    for tag_name in ("h1", "h2"):
        for el in soup.find_all(tag_name):
            t = el.get_text(separator=" ", strip=True)
            if t and len(t) > 3:
                return t

    # Fallback: first non-empty line
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    if lines:
        return lines[0]
    return None


def _extract_company(soup: BeautifulSoup, text: str) -> Optional[str]:
    # CSS / data selectors
    for selector in [
        '[data-testid="company-name"]',
        ".company-name",
        ".company",
        '[itemprop="hiringOrganization"]',
        '[itemprop="name"]',
    ]:
        el = soup.select_one(selector)
        if el:
            name = el.get_text(strip=True)
            if name:
                return name

    # Regex patterns
    patterns = [
        r"at\s+([A-Z][A-Za-z\s&]+?)(?:\n|$)",
        r"([A-Z][A-Za-z\s&]+?)\sis\s+(?:hiring|looking|seeking)",
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            return m.group(1).strip()

    return None


def _extract_location(soup: BeautifulSoup, text: str) -> Optional[str]:
    # CSS / data selectors
    for selector in [
        '[data-testid="location"]',
        ".location",
        '[itemprop="jobLocation"]',
        ".job-location",
    ]:
        el = soup.select_one(selector)
        if el:
            loc = el.get_text(strip=True)
            if loc:
                return loc

    # Regex patterns
    patterns = [
        r"(?:Location|City|Place|Where)\s*[:\-]\s*([^\n]{2,80})",
        r"([A-Za-z\s]+,\s*[A-Za-z\s]{2,40})",
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            candidate = m.group(1).strip()
            if len(candidate) > 2 and len(candidate) < 80:
                return candidate

    return None


def _extract_description(soup: BeautifulSoup, text: str) -> Optional[str]:
    # Try structured containers
    for selector in [
        "[itemprop='description']",
        ".job-description",
        "#job-description",
        ".description",
        ".job-details",
    ]:
        el = soup.select_one(selector)
        if el:
            desc = el.get_text(separator=" ", strip=True)
            if len(desc) > 30:
                return desc[:2000]

    # Fallback: look for "About the role" / "Description" sections
    patterns = [
        r"(?:about\s+the\s+role|job\s+summary|overview|description)\s*[:\-]?\s*([\s\S]{100,2000})(?=\n\s*\n\s*[A-Z])",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return m.group(1).strip()[:2000]

    return None


def _extract_salary(text: str) -> Optional[str]:
    patterns = [
        r"(?:salary|compensation|pay|ctc|package)\s*[:\-]?\s*([^\n]{5,100})",
        r"(\$|₹|EUR|USD|INR)\s*[\d,\s\-]+(?:per\s+year|per\s+month|annum|annually)?",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return m.group(0).strip()
    return None


def _extract_experience(text: str) -> Optional[str]:
    patterns = [
        r"(?:experience|years?|yrs?)\s*[:\-]?\s*([\d+\-]+\s*(?:to?\s*)?[\d+]*\s*(?:years?)?)",
        r"(\d+\+?)\s*\+?\s*(?:years?|yrs?)",
        r"(\d+\s*[-–to]+\s*\d+)\s*(?:years?|yrs?)",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return m.group(0).strip()
    return None


def _extract_employment_type(text: str) -> Optional[str]:
    patterns = [
        r"\b(remote|full\s*time|part\s*time|contract|temporary|hybrid|on\s*site)\b",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return m.group(1).strip().lower()
    return None


def _extract_posted_date(text: str) -> Optional[str]:
    patterns = [
        r"(?:posted|posted\s+on|date)\s*[:\-]?\s*([\d/\-\.]+\s*[A-Za-z]*)",
        r"(\d{1,2}[/\-\.\s]\d{1,2}[/\-\.\s]\d{2,4})",
        r"(\d{4}[/\-\.\s]\d{1,2}[/\-\.\s]\d{1,2})",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return None


def _extract_skills(text: str) -> list[str]:
    skills: set[str] = set()
    common = [
        "Python", "Java", "JavaScript", "TypeScript", "Go", "Golang", "Rust", "C++", "C#",
        "React", "Angular", "Vue", "Node.js", "Django", "Flask", "Spring", "FastAPI",
        "SQL", "NoSQL", "PostgreSQL", "MongoDB", "Redis", "MySQL",
        "AWS", "Azure", "GCP", "Docker", "Kubernetes", "Terraform", "Ansible",
        "Machine Learning", "Deep Learning", "NLP", "Computer Vision",
        "Agile", "Scrum", "CI/CD", "DevOps", "Microservices",
        "REST API", "GraphQL", "gRPC", "Kafka", "RabbitMQ",
        "Git", "Linux", "Bash", "Shell Scripting",
    ]
    for skill in common:
        if re.search(rf"\b{re.escape(skill)}\b", text, re.IGNORECASE):
            skills.add(skill)
    return sorted(skills)
