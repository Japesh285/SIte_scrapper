"""Lightweight test scraper that extracts basic job info via JSON-LD or HTML parsing."""
import json
import time
import re
from pathlib import Path
from datetime import datetime, timezone

import httpx
from bs4 import BeautifulSoup

from app.core.site_utils import normalize_site_url, get_domain
from app.core.logger import logger
from app.services.orchestrator import orchestrate_scrape
from app.db.database import async_session

TEST_OUTPUT_DIR = Path("data")
MAX_JOBS_PER_DOMAIN = 20
REQUEST_DELAY = 1.5  # seconds between requests


def _parse_json_ld(soup: BeautifulSoup) -> dict | None:
    """Extract job data from JSON-LD JobPosting script tags."""
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            # Handle array or single object
            items = data if isinstance(data, list) else [data]
            for item in items:
                if item.get("@type") == "JobPosting" or "JobPosting" in str(item.get("@type", "")):
                    return item
                # Check nested @graph
                if isinstance(item.get("@graph"), list):
                    for graph_item in item["@graph"]:
                        if graph_item.get("@type") == "JobPosting":
                            return graph_item
        except (json.JSONDecodeError, TypeError):
            continue
    return None


def _extract_from_json_ld(data: dict, url: str) -> dict:
    """Map JSON-LD fields to our schema."""
    title = data.get("title", "") or ""
    company = data.get("hiringOrganization", {})
    if isinstance(company, dict):
        company_name = company.get("name", "") or ""
    else:
        company_name = str(company) if company else ""

    location = data.get("jobLocationAddress", {})
    if isinstance(location, dict):
        loc_str = ", ".join(
            v for v in [
                location.get("addressLocality", ""),
                location.get("addressRegion", ""),
                location.get("addressCountry", ""),
            ]
            if v
        )
    else:
        loc_str = data.get("jobLocation", "") or ""
        if isinstance(loc_str, dict):
            loc_str = loc_str.get("name", "") or ""

    return {
        "title": str(title).strip(),
        "company_name": str(company_name).strip(),
        "job_link": url,
        "location": str(loc_str).strip(),
        "experience": str(data.get("experienceRequirements", "")).strip(),
        "salary": str(data.get("baseSalary", "")).strip() if not isinstance(data.get("baseSalary"), dict) else str(data.get("baseSalary", {}).get("value", {}).get("minValue", "")).strip(),
        "job_summary": str(data.get("description", "")).strip(),
        "remote_type": str(data.get("employmentType", "")).strip().lower() if data.get("employmentType") else "unknown",
        "posted_on": str(data.get("datePosted", "")).strip(),
    }


def _extract_from_html(soup: BeautifulSoup, text: str, url: str) -> dict:
    """Fallback: extract basic fields from HTML structure."""
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

    experience = _extract_experience_text(text)
    salary = _extract_salary_text(text)
    job_summary = _extract_summary_text(text, soup)

    return {
        "title": title.strip() if title else "",
        "company_name": company_name.strip() if company_name else "",
        "job_link": url,
        "location": location.strip() if location else "",
        "experience": experience,
        "salary": salary,
        "job_summary": job_summary,
        "remote_type": _extract_remote_type_text(text),
        "posted_on": _extract_posted_date_text(text),
    }


def _extract_experience_text(text: str) -> str:
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


def _extract_summary_text(text: str, soup: BeautifulSoup) -> str:
    # Try common description containers
    for selector in ["[itemprop='description']", ".job-description", "#job-description", ".description"]:
        el = soup.select_one(selector)
        if el:
            return el.get_text(separator=" ", strip=True)[:1000]

    # Fallback to text patterns
    summary_patterns = [
        r"(?:about\s+the\s+role|job\s+summary|overview|description)\s*[:\-]?\s*([\s\S]{100,1000})(?=\n\s*\n\s*[A-Z])",
    ]
    for pattern in summary_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1).strip()[:1000]
    return ""


def _extract_remote_type_text(text: str) -> str:
    remote_patterns = [
        r"(remote|work\s+from\s+home|wfh|hybrid|on-site|onsite|in-office)",
    ]
    for pattern in remote_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1).strip().lower()
    return "unknown"


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


async def scrape_job_detail(url: str) -> dict:
    """Visit a job page and extract basic info via JSON-LD or HTML parsing."""
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            html = resp.text
    except Exception as exc:
        logger.warning(f"[TestScrape] Failed to fetch {url}: {exc}")
        return {"job_link": url, "title": "", "company_name": "", "location": "", "experience": "", "salary": "", "job_summary": "", "remote_type": "unknown", "posted_on": "", "error": str(exc)}

    try:
        soup = BeautifulSoup(html, "html.parser")

        # Remove scripts and styles for clean text extraction
        for tag in soup(["script", "style", "noscript", "iframe"]):
            tag.decompose()

        text = soup.get_text(separator="\n", strip=True)

        # Try JSON-LD first
        json_ld = _parse_json_ld(soup)
        if json_ld:
            result = _extract_from_json_ld(json_ld, url)
        else:
            result = _extract_from_html(soup, text, url)

        result["error"] = ""
        return result
    except Exception as exc:
        logger.warning(f"[TestScrape] Parse error for {url}: {exc}")
        return {"job_link": url, "title": "", "company_name": "", "location": "", "experience": "", "salary": "", "job_summary": "", "remote_type": "unknown", "posted_on": "", "error": str(exc)}


async def run_test_scrape() -> dict:
    """Run test scrape on hardcoded URLs, limit 20 jobs per domain, save JSON files."""
    from app.api.routes import HARD_CODED_URLS

    TEST_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    total_jobs = 0
    domain_results = {}

    for url in HARD_CODED_URLS:
        normalized_url = normalize_site_url(url)
        domain = get_domain(normalized_url)
        logger.info(f"[TestScrape] Starting: {domain}")

        try:
            # Step 1: Use orchestrator to get job listings
            async with async_session() as session:
                scrape_result = await orchestrate_scrape(normalized_url, session)

            if scrape_result["status"] != "success" or scrape_result["jobs_found"] == 0:
                logger.info(f"[TestScrape] Skipped {domain} - no jobs found")
                continue

            # Step 2: Read job URLs from latest saved JSON
            from app.services.raw_json_saver import RAW_JSON_DIR
            domain_dir = RAW_JSON_DIR / domain
            if not domain_dir.exists():
                continue

            files = sorted(domain_dir.glob("scrape_result_*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
            if not files:
                continue

            with open(files[0], "r", encoding="utf-8") as f:
                data = json.load(f)
                job_urls = [job.get("url", "") for job in data.get("jobs", []) if job.get("url")]

            # Limit to MAX_JOBS_PER_DOMAIN
            job_urls = job_urls[:MAX_JOBS_PER_DOMAIN]
            logger.info(f"[TestScrape] {domain} processing {len(job_urls)} jobs")

            jobs_detail = []
            for i, job_url in enumerate(job_urls):
                logger.info(f"[TestScrape] {domain} scraping job {i+1}/{len(job_urls)}")
                detail = await scrape_job_detail(job_url)
                jobs_detail.append(detail)
                total_jobs += 1

                # Small delay between requests
                if i < len(job_urls) - 1:
                    await _async_sleep(REQUEST_DELAY)

            # Save domain results
            output_file = TEST_OUTPUT_DIR / f"{domain}.json"
            output_data = {
                "domain": domain,
                "site_type": scrape_result.get("type", "UNKNOWN"),
                "scraped_at": datetime.now(timezone.utc).isoformat(),
                "jobs_count": len(jobs_detail),
                "jobs": jobs_detail,
            }

            with open(output_file, "w", encoding="utf-8") as f:
                json.dump(output_data, f, indent=2, ensure_ascii=False, default=str)

            logger.info(f"[TestScrape] {domain} saved {len(jobs_detail)} jobs to {output_file}")
            domain_results[domain] = len(jobs_detail)

            # Delay between domains
            await _async_sleep(REQUEST_DELAY)

        except Exception as exc:
            logger.error(f"[TestScrape] Error processing {domain}: {exc}")
            continue

    return {"jobs_scraped": total_jobs, "domains": domain_results}


async def _async_sleep(seconds: float):
    """Async sleep without blocking."""
    import asyncio
    await asyncio.sleep(seconds)
