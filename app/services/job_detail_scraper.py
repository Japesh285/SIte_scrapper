"""Scrape detailed job information from individual job listing pages."""
import re
from datetime import datetime, timezone

from app.core.logger import logger

try:
    from playwright.async_api import async_playwright
except Exception:  # pragma: no cover
    async_playwright = None


async def scrape_job_details(job_url: str) -> dict:
    """Visit a job listing page and extract detailed job information."""
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

            title = await _extract_title(page, text)
            company_name = await _extract_company(page, text)
            locations = await _extract_locations(page, text)
            experience = await _extract_experience(text)
            educational_qualifications = await _extract_education(text)
            required_skills = await _extract_skills(text)
            job_summary = await _extract_summary(text)
            responsibilities = await _extract_responsibilities(text)
            posted_on = await _extract_posted_date(text)
            job_id = await _extract_job_id(job_url, text)
            salary = await _extract_salary(text)
            remote_type = await _extract_remote_type(text)
            about_us = await _extract_about_us(text)

            await browser.close()

            return {
                "title": title,
                "company_name": company_name,
                "job_link": job_url,
                "experience": experience,
                "locations": locations,
                "educational_qualifications": educational_qualifications,
                "required_skill_set": required_skills,
                "remote_type": remote_type,
                "posted_on": posted_on,
                "job_id": job_id,
                "salary": salary,
                "is_active": True,
                "first_seen": datetime.now(timezone.utc).isoformat(),
                "last_seen": datetime.now(timezone.utc).isoformat(),
                "job_summary": job_summary,
                "key_responsibilities": responsibilities,
                "additional_sections": [],
                "about_us": about_us,
                "Scrap_json": {"url": job_url, "html_snippet": html[:2000]},
            }
    except Exception as exc:
        logger.error(f"[JobDetail] Error scraping {job_url}: {exc}")
        return _empty_job(job_url)


async def _extract_title(page, text: str) -> str:
    try:
        title_el = await page.query_selector("h1")
        if title_el:
            title = (await title_el.inner_text()).strip()
            if title:
                return title
    except Exception:
        pass
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    return lines[0] if lines else ""


async def _extract_company(page, text: str) -> str:
    try:
        for selector in [
            '[data-testid="company-name"]',
            ".company-name",
            ".company",
            '[itemprop="hiringOrganization"]',
        ]:
            el = await page.query_selector(selector)
            if el:
                name = (await el.inner_text()).strip()
                if name:
                    return name
    except Exception:
        pass
    company_patterns = [
        r"at\s+([A-Z][A-Za-z\s&]+?)(?:\n|$)",
        r"([A-Z][A-Za-z\s&]+?)\sis\s+looking",
    ]
    for pattern in company_patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1).strip()
    return ""


async def _extract_locations(page, text: str) -> list[str]:
    try:
        for selector in [
            '[data-testid="location"]',
            ".location",
            '[itemprop="jobLocation"]',
            ".job-location",
        ]:
            el = await page.query_selector(selector)
            if el:
                loc = (await el.inner_text()).strip()
                if loc:
                    return [loc]
    except Exception:
        pass
    loc_patterns = [
        r"Location[:\s]*([^\n]+)",
        r"([A-Za-z\s]+,\s*[A-Za-z\s]+)",
    ]
    for pattern in loc_patterns:
        match = re.search(pattern, text)
        if match:
            return [match.group(1).strip()]
    return []


async def _extract_experience(text: str) -> str:
    exp_patterns = [
        r"(?:experience|years?|yrs?)\s*[:\-]?\s*([\d+\-]+\s*(?:to?\s*)?[\d+]*\s*(?:years?)?)",
        r"(\d+\+?)\s*\+?\s*(?:years?|yrs?)",
        r"(\d+\s*[-–to]+\s*\d+)\s*(?:years?|yrs?)",
    ]
    for pattern in exp_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(0).strip()
    return ""


async def _extract_education(text: str) -> str:
    edu_patterns = [
        r"(?:education|qualification|degree|bachelor|master|phd)\s*[:\-]?\s*([^\n]{5,200})",
        r"(B\.?S\.?|B\.?Tech|B\.?E\.?|M\.?S\.?|M\.?Tech|M\.?E\.?|MBA|Ph\.?D\.?)[^\n]{0,100}",
    ]
    for pattern in edu_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(0).strip()
    return ""


async def _extract_skills(text: str) -> list[str]:
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
    skill_section_patterns = [
        r"(?:required\s+skills?|qualifications?|what\s+you\s+need|technical\s+skills?)\s*[:\-]?\s*([\s\S]{50,1000})(?=\n\s*\n)",
    ]
    for pattern in skill_section_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            section = match.group(1)
            for skill in common_skills:
                if re.search(rf"\b{re.escape(skill)}\b", section, re.IGNORECASE):
                    skills.add(skill)
    return sorted(skills)


async def _extract_summary(text: str) -> str:
    summary_patterns = [
        r"(?:about\s+the\s+role|job\s+summary|overview|description)\s*[:\-]?\s*([\s\S]{100,1000})(?=\n\s*\n\s*[A-Z])",
        r"(?:we['']re\s+(?:looking|seeking|hiring))[^\n]{0,500}",
    ]
    for pattern in summary_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return ""


async def _extract_responsibilities(text: str) -> list[str]:
    resp_patterns = [
        r"(?:responsibilities?|what\s+you['']ll\s+do|key\s+responsibilities?)\s*[:\-]?\s*([\s\S]{100,2000})(?=\n\s*\n\s*[A-Z])",
    ]
    for pattern in resp_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            section = match.group(1).strip()
            items = [item.strip("•\-\* ").strip() for item in re.split(r"\n\s*[\n•\-\*]", section) if item.strip()]
            return items[:20]
    return []


async def _extract_posted_date(text: str) -> str:
    date_patterns = [
        r"(?:posted|posted\s+on|date)\s*[:\-]?\s*([\d/\-\.]+\s*[A-Za-z]*)",
        r"(\d{1,2}[/\-\.\s]\d{1,2}[/\-\.\s]\d{2,4})",
        r"(\d{4}[/\-\.\s]\d{1,2}[/\-\.\s]\d{1,2})",
    ]
    for pattern in date_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return ""


async def _extract_job_id(job_url: str, text: str) -> str:
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


async def _extract_salary(text: str) -> str:
    salary_patterns = [
        r"(?:salary|compensation|pay|ctc|package)\s*[:\-]?\s*([^\n]{5,100})",
        r"(\$|₹|EUR|USD|INR)[\d,\s\-]+(?:per\s+year|per\s+month|annum|annually)?",
    ]
    for pattern in salary_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(0).strip()
    return ""


async def _extract_remote_type(text: str) -> str:
    remote_patterns = [
        r"(remote|work\s+from\s+home|wfh|hybrid|on-site|onsite|in-office)",
    ]
    for pattern in remote_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1).strip().lower()
    return "unknown"


async def _extract_about_us(text: str) -> str:
    about_patterns = [
        r"(?:about\s+us|about\s+the\s+company|who\s+we\s+are|our\s+company)\s*[:\-]?\s*([\s\S]{100,1000})(?=\n\s*\n\s*[A-Z])",
        r"(?:we\s+are|we['']re)\s+([^\n]{50,500})",
    ]
    for pattern in about_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1).strip()
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
