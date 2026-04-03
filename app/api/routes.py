from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel
import json
from pathlib import Path

from app.core.site_utils import normalize_site_url, get_domain
from app.db.database import get_session
from app.db.models import Site
from app.services.orchestrator import orchestrate_scrape
from app.services.raw_json_saver import RAW_JSON_DIR
from app.services.job_detail_scraper import scrape_job_details
from app.services.test_scrape import run_test_scrape
from app.core.logger import logger

router = APIRouter()


class ScrapeRequest(BaseModel):
    url: str


class ScrapeResponse(BaseModel):
    domain: str
    type: str
    confidence: float
    jobs_found: int
    status: str


class BulkScrapeResponse(BaseModel):
    total_sites: int
    successful: int
    failed: int
    skipped: int
    results: list[ScrapeResponse]


class JobDetailResponse(BaseModel):
    id: str = ""
    title: str = ""
    company_name: str = ""
    job_link: str = ""
    experience: str = ""
    locations: list = []
    educational_qualifications: str = ""
    required_skill_set: list = []
    remote_type: str = ""
    posted_on: str = ""
    job_id: str = ""
    salary: str = ""
    is_active: bool = True
    first_seen: str = ""
    last_seen: str = ""
    job_summary: str = ""
    key_responsibilities: list = []
    additional_sections: list = []
    about_us: str = ""
    Scrap_json: dict = {}


@router.post("/scrape", response_model=ScrapeResponse)
async def scrape(request: ScrapeRequest, session: AsyncSession = Depends(get_session)):
    result = await orchestrate_scrape(request.url, session)
    return ScrapeResponse(
        domain=result["domain"],
        type=result["type"],
        confidence=result["confidence"],
        jobs_found=result["jobs_found"],
        status=result["status"],
    )


HARD_CODED_URLS = [
    "https://medtronic.wd1.myworkdayjobs.com/MedtronicCareers?locationCountry=c4f78be1a8f14da0ab49ce1162348a5e&jobFamilyGroup=2fe8588f35e84eb98ef535f4d738f243",
    "https://medtronic.wd1.myworkdayjobs.com/MedtronicCareers?jobFamilyGroup=5d03e9707876432d93848a9e7146e1ad",
    "https://jobs.dell.com/en/search-jobs/India/375/2/1269750/22/79/50/2",
    "https://jobs.standardchartered.com/go/Experienced-Professional-jobs/9783657/?feedid=363857&markerViewed=&carouselIndex=&facetFilters=%7B%22cust_region%22%3A%5B%22Asia%22%5D%2C%22jobLocationCountry%22%3A%5B%22India%22%5D%2C%22cust_csb_employmentType%22%3A%5B%22+Permanent%22%5D%7D&pageNumber=0",
]


@router.post("/scrape-hardcoded", response_model=BulkScrapeResponse)
async def scrape_hardcoded_urls(session: AsyncSession = Depends(get_session)):
    """Scrape a fixed list of URLs."""

    results = []
    successful = 0
    failed = 0
    skipped = 0

    for url in HARD_CODED_URLS:
        try:
            scrape_result = await orchestrate_scrape(url, session)
            results.append(ScrapeResponse(
                domain=scrape_result["domain"],
                type=scrape_result["type"],
                confidence=scrape_result["confidence"],
                jobs_found=scrape_result["jobs_found"],
                status=scrape_result["status"],
            ))
            if scrape_result["status"] == "success" and scrape_result["jobs_found"] > 0:
                successful += 1
            elif scrape_result["status"] == "skipped":
                skipped += 1
            else:
                failed += 1
        except Exception as e:
            failed += 1
            results.append(ScrapeResponse(
                domain=url,
                type="ERROR",
                confidence=0.0,
                jobs_found=0,
                status="failed",
            ))

    return BulkScrapeResponse(
        total_sites=len(HARD_CODED_URLS),
        successful=successful,
        failed=failed,
        skipped=skipped,
        results=results,
    )


@router.post("/scrape-all", response_model=BulkScrapeResponse)
async def scrape_all_sites(session: AsyncSession = Depends(get_session)):
    """Scrape all sites from the database one at a time."""

    # Fetch all sites from DB
    result = await session.execute(select(Site))
    sites = result.scalars().all()

    results = []
    successful = 0
    failed = 0
    skipped = 0

    for site in sites:
        try:
            url = normalize_site_url(site.domain)
            scrape_result = await orchestrate_scrape(url, session)
            results.append(ScrapeResponse(
                domain=scrape_result["domain"],
                type=scrape_result["type"],
                confidence=scrape_result["confidence"],
                jobs_found=scrape_result["jobs_found"],
                status=scrape_result["status"],
            ))
            if scrape_result["status"] == "success" and scrape_result["jobs_found"] > 0:
                successful += 1
            elif scrape_result["status"] == "skipped":
                skipped += 1
            else:
                failed += 1
        except Exception as e:
            failed += 1
            results.append(ScrapeResponse(
                domain=site.domain,
                type="ERROR",
                confidence=0.0,
                jobs_found=0,
                status="failed",
            ))

    return BulkScrapeResponse(
        total_sites=len(sites),
        successful=successful,
        failed=failed,
        skipped=skipped,
        results=results,
    )


@router.post("/scrape-hardcoded-details")
async def scrape_hardcoded_job_details():
    """Scrape hardcoded URLs, then visit each job URL to extract detailed info - all in one go."""
    from app.db.database import async_session
    from app.scrapers.dom_browser import scrape_dom_browser
    from app.detectors import detect_workday, detect_greenhouse, detect_simple_api, detect_dom_browser
    from app.detectors.workday import fetch_workday_jobs
    from app.detectors.greenhouse import fetch_greenhouse_jobs, resolve_greenhouse_slug
    from app.detectors.simple_api import fetch_simple_api_jobs
    import httpx

    result = {"sites": []}

    for url in HARD_CODED_URLS:
        normalized_url = normalize_site_url(url)
        domain = get_domain(normalized_url)
        logger.info(f"[DetailScraper] Starting: {domain}")

        job_urls = []
        site_type = "UNKNOWN"

        # Step 1: Scrape the job listing page
        try:
            async with async_session() as session:
                scrape_result = await orchestrate_scrape(normalized_url, session)
                site_type = scrape_result["type"]
                logger.info(f"[DetailScraper] {domain} -> {site_type}, jobs={scrape_result['jobs_found']}")
        except Exception as e:
            logger.warning(f"[DetailScraper] Orchestrate failed for {url}: {e}")

        # Step 2: Collect job URLs from the latest saved JSON
        domain_dir = RAW_JSON_DIR / domain
        if domain_dir.exists():
            files = sorted(domain_dir.glob("scrape_result_*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
            if files:
                with open(files[0], "r", encoding="utf-8") as f:
                    data = json.load(f)
                    site_type = data.get("site_type", site_type)
                    job_urls = [job.get("url", "") for job in data.get("jobs", []) if job.get("url")]
                    logger.info(f"[DetailScraper] {domain} collected {len(job_urls)} job URLs")

        # Step 3: Visit each job URL with headless=False and extract details
        site_entry = {
            "domain": domain,
            "site_type": site_type,
            "jobs_count": 0,
            "jobs": []
        }

        if job_urls:
            try:
                from playwright.async_api import async_playwright
                async with async_playwright() as playwright:
                    browser = await playwright.chromium.launch(headless=False)
                    page = await browser.new_page()

                    for i, job_url in enumerate(job_urls):
                        logger.info(f"[DetailScraper] {domain} scraping job {i+1}/{len(job_urls)}: {job_url}")
                        detail = await _scrape_job_detail_with_page(page, job_url)
                        detail["id"] = detail.get("job_id", "") or job_url.split("/")[-1]
                        site_entry["jobs"].append(detail)

                    await browser.close()
            except Exception as e:
                logger.error(f"[DetailScraper] Browser error for {domain}: {e}")
                # Fallback: create empty entries
                for job_url in job_urls:
                    site_entry["jobs"].append(_empty_job_detail(job_url))

        site_entry["jobs_count"] = len(site_entry["jobs"])
        result["sites"].append(site_entry)
        logger.info(f"[DetailScraper] {domain} done -> {site_entry['jobs_count']} jobs detailed")

    return result


async def _scrape_job_detail_with_page(page, job_url: str) -> dict:
    """Scrape a single job page using an existing browser page (headless=False)."""
    try:
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
        return _empty_job_detail(job_url)


def _empty_job_detail(job_url: str) -> dict:
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


# --- Extraction helpers (same as job_detail_scraper but page-based) ---
import re
from datetime import datetime, timezone

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


@router.get("/test-scrape")
async def test_scrape():
    """Run lightweight test scrape on hardcoded URLs.

    Limits to 5 jobs per domain, saves JSON files to /data/,
    and returns the total count of jobs scraped.
    """
    result = await run_test_scrape()
    return result
