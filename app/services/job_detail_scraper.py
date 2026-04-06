"""Scrape detailed job information from individual job listing pages.

This module wraps the new modular job_detail_engine while preserving the
original public API (scrape_job_details) for backward compatibility.
"""
import re
from datetime import datetime, timezone

from app.core.logger import logger

try:
    from playwright.async_api import async_playwright
except Exception:  # pragma: no cover
    async_playwright = None


async def scrape_job_details(job_url: str) -> dict:
    """Visit a job listing page and extract detailed job information.

    Uses the new job_detail_engine pipeline internally (JSON-LD → HTML → AI).
    """
    if async_playwright is None:
        logger.warning("[JobDetail] Playwright unavailable")
        return _empty_job(job_url)

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(job_url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(3000)

            html = await page.content()
            text = await page.inner_text("body")

            await browser.close()

            # Delegate to the new engine
            engine_result = await _extract_via_engine(html, job_url)

            return engine_result
    except Exception as exc:
        logger.error(f"[JobDetail] Error scraping {job_url}: {exc}")
        return _empty_job(job_url)


async def _extract_via_engine(html: str, job_url: str) -> dict:
    """Run the new job_detail_engine pipeline and map to legacy schema."""
    try:
        from app.job_detail_engine.orchestrator import extract_job_details
        engine = await extract_job_details(html)
    except Exception as exc:
        logger.warning("[JobDetail] Engine failed, falling back to legacy: %s", exc)
        return await _legacy_extract(html, job_url)

    # Map engine output to the legacy schema expected by callers
    return {
        "title": engine.get("title") or "",
        "company_name": engine.get("company_name") or "",
        "job_link": job_url,
        "experience": engine.get("experience") or "",
        "locations": [engine["location"]] if engine.get("location") else [],
        "educational_qualifications": "",
        "required_skill_set": engine.get("skills") or [],
        "remote_type": engine.get("employment_type") or "unknown",
        "posted_on": engine.get("posted_on") or "",
        "job_id": _extract_job_id(job_url, ""),
        "salary": engine.get("salary") or "",
        "is_active": True,
        "first_seen": datetime.now(timezone.utc).isoformat(),
        "last_seen": datetime.now(timezone.utc).isoformat(),
        "job_summary": engine.get("description") or "",
        "key_responsibilities": [],
        "additional_sections": [],
        "about_us": "",
        "Scrap_json": {"url": job_url, "html_snippet": html[:2000], **engine.get("_meta", {})},
    }


async def _legacy_extract(html: str, job_url: str) -> dict:
    """Fallback: use the old regex/DOM-based extraction if the engine fails."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(separator="\n", strip=True)

    title = ""
    h1 = soup.find("h1")
    if h1:
        title = h1.get_text(separator=" ", strip=True)

    company_name = ""
    for selector in ['[data-testid="company-name"]', ".company-name", ".company", '[itemprop="hiringOrganization"]']:
        el = soup.select_one(selector)
        if el:
            company_name = el.get_text(strip=True)
            break

    location = ""
    for selector in ['[data-testid="location"]', ".location", '[itemprop="jobLocation"]', ".job-location"]:
        el = soup.select_one(selector)
        if el:
            location = el.get_text(strip=True)
            break

    return {
        "title": title,
        "company_name": company_name,
        "job_link": job_url,
        "experience": _extract_experience(text),
        "locations": [location] if location else [],
        "educational_qualifications": "",
        "required_skill_set": _extract_skills(text),
        "remote_type": _extract_remote_type(text),
        "posted_on": _extract_posted_date_text(text),
        "job_id": _extract_job_id(job_url, text),
        "salary": _extract_salary_text(text),
        "is_active": True,
        "first_seen": datetime.now(timezone.utc).isoformat(),
        "last_seen": datetime.now(timezone.utc).isoformat(),
        "job_summary": _extract_summary_text(text, soup),
        "key_responsibilities": [],
        "additional_sections": [],
        "about_us": "",
        "Scrap_json": {"url": job_url, "html_snippet": html[:2000]},
    }


def _extract_experience(text: str) -> str:
    exp_patterns = [
        r"(?:experience|years?|yrs?)\s*[:\-]?\s*([\d+\-]+\s*(?:to?\s*)?[\d+]*\s*(?:years?)?)",
        r"(\d+\+?)\s*\+?\s*(?:years?|yrs?)",
    ]
    for pattern in exp_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(0).strip()
    return ""


def _extract_salary_text(text: str) -> str:
    salary_patterns = [
        r"(?:salary|compensation|pay|ctc|package)\s*[:\-]?\s*([^\n]{5,100})",
        r"(\$|₹|EUR|USD|INR)[\d,\s\-]+(?:per\s+year|per\s+month|annum|annually)?",
    ]
    for pattern in salary_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(0).strip()
    return ""


def _extract_skills(text: str) -> list[str]:
    skills = set()
    common_skills = [
        "Python", "Java", "JavaScript", "TypeScript", "Go", "Golang", "Rust", "C++", "C#",
        "React", "Angular", "Vue", "Node.js", "Django", "Flask", "Spring", "FastAPI",
        "SQL", "NoSQL", "PostgreSQL", "MongoDB", "Redis", "MySQL",
        "AWS", "Azure", "GCP", "Docker", "Kubernetes", "Terraform", "Ansible",
        "Machine Learning", "Deep Learning", "NLP", "Computer Vision",
        "Agile", "Scrum", "CI/CD", "DevOps", "Microservices",
        "REST API", "GraphQL", "gRPC", "Kafka", "RabbitMQ",
        "Git", "Linux", "Bash", "Shell Scripting",
    ]
    for skill in common_skills:
        if re.search(rf"\b{re.escape(skill)}\b", text, re.IGNORECASE):
            skills.add(skill)
    return sorted(skills)


def _extract_summary_text(text: str, soup) -> str:
    for selector in ["[itemprop='description']", ".job-description", "#job-description", ".description"]:
        el = soup.select_one(selector)
        if el:
            return el.get_text(separator=" ", strip=True)[:1000]
    summary_patterns = [
        r"(?:about\s+the\s+role|job\s+summary|overview|description)\s*[:\-]?\s*([\s\S]{100,1000})(?=\n\s*\n\s*[A-Z])",
    ]
    for pattern in summary_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1).strip()[:1000]
    return ""


def _extract_posted_date_text(text: str) -> str:
    date_patterns = [
        r"(?:posted|posted\s+on|date)\s*[:\-]?\s*([\d/\-\.]+\s*[A-Za-z]*)",
        r"(\d{1,2}[/\-\.\s]\d{1,2}[/\-\.\s]\d{2,4})",
    ]
    for pattern in date_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return ""


def _extract_remote_type(text: str) -> str:
    remote_patterns = [
        r"(remote|work\s+from\s+home|wfh|hybrid|on-site|onsite|in-office)",
    ]
    for pattern in remote_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1).strip().lower()
    return "unknown"


def _extract_job_id(job_url: str, text: str) -> str:
    url_id_patterns = [
        r"/(\d{6,})$",
        r"/(\d{6,})/",
        r"req[_\-]?id\s*[:=]?\s*([A-Za-z0-9\-]+)",
    ]
    for pattern in url_id_patterns:
        match = re.search(pattern, job_url, re.IGNORECASE)
        if match:
            return match.group(1)
    return ""


def _empty_job(job_url: str) -> dict:
    return {
        "title": "",
        "company_name": "",
        "job_link": job_url,
        "experience": "",
        "locations": [],
        "educational_qualifications": "",
        "required_skill_set": [],
        "remote_type": "unknown",
        "posted_on": "",
        "job_id": "",
        "salary": "",
        "is_active": True,
        "first_seen": datetime.now(timezone.utc).isoformat(),
        "last_seen": datetime.now(timezone.utc).isoformat(),
        "job_summary": "",
        "key_responsibilities": [],
        "additional_sections": [],
        "about_us": "",
        "Scrap_json": {"url": job_url, "error": "scrape_failed"},
    }
