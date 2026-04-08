"""
Workday-only job scraper using ONLY Workday API endpoints.

Flow:
1. POST /wday/cxs/{tenant}/{site}/jobs → fetch job listings
2. GET  /wday/cxs/{tenant}/{site}/job/{clean_path} → fetch full job detail JSON
3. Send FULL raw JSON to OpenAI for structured extraction
4. Save parsed results to Excel (.xlsx)

DO NOT use: Playwright, BeautifulSoup, HTML parsing, AI HTML parsing.
"""

import asyncio
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
import openpyxl
from openai import OpenAI
from openpyxl import Workbook
from dotenv import load_dotenv

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
if not OPENAI_API_KEY:
    raise SystemExit("ERROR: OPENAI_API_KEY not set in environment or .env file")

OPENAI_CLIENT = OpenAI(api_key=OPENAI_API_KEY)

# ── Workday API patterns ──────────────────────────────────────────────

WORKDAY_API_PATTERN = re.compile(
    r"(https?:)?//[^\"'\s]+/wday/cxs/[^\"'\s]+",
    re.IGNORECASE,
)

WORKDAY_COMPANY_PATTERN = re.compile(
    r'"company"\s*:\s*"([^"]+)"|"tenant"\s*:\s*"([^"]+)"',
    re.IGNORECASE,
)

# ── Helpers ────────────────────────────────────────────────────────────


def normalize_external_path(path: str) -> str:
    """Normalize externalPath to clean path for detail API call."""
    if not path:
        return ""
    path = path.strip().lstrip("/")
    if path.startswith("job/"):
        path = path[len("job/"):]
    return path


def parse_workday_config_from_api_url(api_url: str) -> dict | None:
    """
    Extract tenant + site + base from:
    https://tenant.wdX.myworkdayjobs.com/wday/cxs/tenant/site/jobs
    """
    try:
        parsed = urlparse(api_url)
        host_parts = parsed.netloc.split(".")
        tenant = host_parts[0]
        server = host_parts[1]

        path_parts = [p for p in parsed.path.split("/") if p]
        # wday / cxs / tenant / site / jobs
        site = path_parts[3]

        base = f"{parsed.scheme}://{tenant}.{server}.myworkdayjobs.com"

        return {
            "tenant": tenant,
            "site": site,
            "base": base,
        }
    except Exception:
        return None


def extract_company_tokens(html: str) -> list[str]:
    """Extract company/tenant tokens from HTML."""
    tokens: list[str] = []
    seen: set[str] = set()
    for match in WORKDAY_COMPANY_PATTERN.finditer(html):
        candidate = (match.group(1) or match.group(2) or "").strip()
        if candidate and candidate not in seen:
            seen.add(candidate)
            tokens.append(candidate)
    return tokens


def build_api_candidates(url: str, html: str, discovered_urls: list[str] | None = None) -> list[str]:
    """Build candidate Workday API URLs."""
    candidates: list[str] = []
    seen: set[str] = set()

    # From discovered URLs
    if discovered_urls:
        for discovered_url in discovered_urls:
            if "workday" in discovered_url.lower():
                _add_candidate(discovered_url, url, candidates, seen)

    # From HTML patterns
    for match in WORKDAY_API_PATTERN.finditer(html):
        _add_candidate(match.group(0), url, candidates, seen)

    # From URL itself
    if "workday" in url.lower():
        parsed = urlparse(url)
        path_segments = [s for s in parsed.path.split("/") if s]
        if path_segments:
            company = path_segments[0]
            fallback = f"{parsed.scheme}://{parsed.netloc}/wday/cxs/{company}/{company}/jobs"
            if fallback not in seen:
                seen.add(fallback)
                candidates.append(fallback)

    # From company tokens in HTML
    parsed = urlparse(url)
    for token in extract_company_tokens(html):
        fallback = f"{parsed.scheme}://{parsed.netloc}/wday/cxs/{token}/{token}/jobs"
        if fallback not in seen:
            seen.add(fallback)
            candidates.append(fallback)

    return candidates


def _add_candidate(candidate: str, base_url: str, candidates: list[str], seen: set[str]) -> None:
    normalized = candidate
    if normalized.startswith("//"):
        normalized = f"https:{normalized}"
    elif not normalized.startswith("http"):
        from urllib.parse import urljoin as _uj
        parsed_base = urlparse(base_url)
        origin = f"{parsed_base.scheme}://{parsed_base.netloc}"
        normalized = _uj(origin, normalized)

    normalized = normalized.rstrip("/")
    jobs_candidate = normalized if normalized.endswith("/jobs") else f"{normalized}/jobs"
    for item in (normalized, jobs_candidate):
        if item not in seen:
            seen.add(item)
            candidates.append(item)


# ── STEP 1 — Fetch job listings ───────────────────────────────────────


async def fetch_workday_listings(api_url: str, client: httpx.AsyncClient) -> list[dict]:
    """POST /wday/cxs/{tenant}/{site}/jobs to get job listings."""
    jobs: list[dict] = []
    seen_urls: set[str] = set()
    offset = 0
    limit = 20

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    while True:
        payload = {
            "limit": limit,
            "offset": offset,
            "searchText": "",
            "appliedFacets": {},
        }

        try:
            res = await client.post(api_url, json=payload, headers=headers)
            if res.status_code != 200:
                print(f"[API] Listing fetch failed status={res.status_code}")
                break
            data = res.json()
        except Exception as e:
            print(f"[API] Listing fetch error: {e}")
            break

        postings = data.get("jobPostings") or []
        if not postings:
            break

        added = 0
        for posting in postings:
            title = str(posting.get("title") or "").strip()
            external_path = str(posting.get("externalPath") or "").strip()
            location = str(posting.get("locationsText") or posting.get("location") or "").strip()

            if not title or not external_path:
                continue

            job_url = urljoin(api_url, external_path)
            if job_url.lower() in seen_urls:
                continue
            seen_urls.add(job_url.lower())

            jobs.append({
                "title": title,
                "location": location,
                "url": job_url,
                "external_path": external_path,
                "posted_date": str(posting.get("postedOn") or posting.get("posted_date") or ""),
                "_raw_listing": posting,
            })
            added += 1

        if added == 0:
            break
        offset += limit

    return jobs


# ── STEP 2 — Fetch job detail JSON ────────────────────────────────────


async def fetch_job_detail(
    base: str,
    tenant: str,
    site: str,
    external_path: str,
    client: httpx.AsyncClient,
) -> dict | None:
    """GET /wday/cxs/{tenant}/{site}/job/{clean_path} → full raw JSON."""
    clean_path = normalize_external_path(external_path)
    if not clean_path:
        return None

    detail_url = f"{base}/wday/cxs/{tenant}/{site}/job/{clean_path}"
    print(f"[API] Fetching: {detail_url}")

    headers = {
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0",
    }

    try:
        res = await client.get(detail_url, headers=headers)
        if res.status_code != 200:
            print(f"[API] Skipped {detail_url} status={res.status_code}")
            return None

        content_type = res.headers.get("content-type", "")
        if "application/json" not in content_type:
            print(f"[API] Skipped (not JSON) {detail_url}")
            return None

        detail_json = res.json()
        print(f"[API] Success: {detail_url}")
        return detail_json

    except Exception as e:
        print(f"[API] Error {detail_url}: {e}")
        return None


# ── STEP 3 — Send FULL JSON to OpenAI ─────────────────────────────────

SYSTEM_PROMPT = """\
Extract structured job data from this JSON.

Return a JSON object with these exact fields (use null if unavailable):
- job_title (str)
- company_name (str)
- location (str)
- posted_date (str)
- job_id (str)
- employment_type (str)
- department (str)
- description (str) — full job description text
- required_skills (list[str]) — ALL skills, qualifications, requirements found
- experience (str)
- education (str)
- salary (str)
- remote_type (str)
- application_url (str)

Return ONLY valid JSON, no markdown, no explanation."""


async def extract_with_openai(detail_json: dict) -> dict | None:
    """Send entire detail_json to OpenAI, return parsed result."""
    try:
        response = OPENAI_CLIENT.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(detail_json, ensure_ascii=False, default=str)},
            ],
            temperature=0,
        )
        parsed_text = response.choices[0].message.content
        print("[OPENAI] Processed job")

        # Parse the JSON response
        try:
            parsed = json.loads(parsed_text)
        except json.JSONDecodeError:
            # Try to extract JSON from markdown code block
            match = re.search(r"```(?:json)?\s*\n(.*?)\n```", parsed_text, re.DOTALL)
            if match:
                parsed = json.loads(match.group(1))
            else:
                parsed = {"raw_openai_response": parsed_text}

        return parsed

    except Exception as e:
        print(f"[OPENAI] Error: {e}")
        return None


# ── STEP 4 — Save to Excel ────────────────────────────────────────────


def save_to_excel(results: list[dict], output_path: str = "jobs.xlsx") -> str:
    """Save OpenAI-parsed results to Excel."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Job Data"

    # Define columns
    columns = [
        "job_title",
        "company_name",
        "location",
        "posted_date",
        "job_id",
        "employment_type",
        "department",
        "description",
        "required_skills",
        "experience",
        "education",
        "salary",
        "remote_type",
        "application_url",
        "raw_detail_url",
    ]

    # Header row
    ws.append(columns)

    # Data rows
    for row in results:
        ws_row = []
        for col in columns:
            val = row.get(col, "")
            # Convert lists to comma-separated strings for Excel
            if isinstance(val, list):
                val = ", ".join(str(v) for v in val)
            elif val is None:
                val = ""
            else:
                val = str(val)
            ws_row.append(val)
        ws.append(ws_row)

    wb.save(output_path)
    print(f"[EXCEL] Saved {len(results)} jobs to {output_path}")
    return output_path


# ── Main Flow ─────────────────────────────────────────────────────────


async def scrape_workday_jobs(company_url: str, max_jobs: int | None = None) -> list[dict]:
    """
    Full Workday scraping flow using ONLY API endpoints.

    Args:
        company_url: The Workday career site URL (e.g., https://aig.wd1.myworkdayjobs.com/aig)
        max_jobs: Maximum number of jobs to process (None = all)
    """
    async with httpx.AsyncClient(
        timeout=30,
        follow_redirects=True,
        headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        },
    ) as client:
        # 1. Resolve API URL
        print(f"[API] Resolving API URL from: {company_url}")
        try:
            resp = await client.get(company_url)
            resp.raise_for_status()
            html = resp.text
        except Exception as e:
            print(f"[API] Failed to fetch page: {e}")
            return []

        candidates = build_api_candidates(company_url, html)
        config = None
        api_url = None

        for candidate in candidates:
            cfg = parse_workday_config_from_api_url(candidate)
            if cfg:
                config = cfg
                api_url = candidate
                print(f"[API] Found Workday API: {api_url}")
                break

        if not config or not api_url:
            print("[API] No Workday API endpoint found")
            return []

        # 2. Fetch listings
        print(f"[API] Fetching job listings from {api_url}")
        listings = await fetch_workday_listings(api_url, client)
        print(f"[API] Found {len(listings)} jobs")

        if max_jobs:
            listings = listings[:max_jobs]

        # 3. Fetch details + OpenAI extraction
        results = []
        for job in listings:
            external_path = job.get("external_path", "")
            clean_path = normalize_external_path(external_path)
            if not clean_path:
                continue

            # Fetch full job detail JSON
            detail_json = await fetch_job_detail(
                base=config["base"],
                tenant=config["tenant"],
                site=config["site"],
                external_path=external_path,
                client=client,
            )

            if detail_json is None:
                continue

            # Send FULL JSON to OpenAI
            parsed = await extract_with_openai(detail_json)
            if parsed is None:
                continue

            # Add listing info and raw detail URL
            result = {
                **parsed,
                "raw_detail_url": f"{config['base']}/wday/cxs/{config['tenant']}/{config['site']}/job/{clean_path}",
            }
            results.append(result)

        return results


# ── CLI Entry Point ───────────────────────────────────────────────────


def main():
    """CLI entry point."""
    if len(sys.argv) < 2:
        print("Usage: python workday_api_only.py <company_url> [max_jobs]")
        print("Example: python workday_api_only.py https://aig.wd1.myworkdayjobs.com/aig 5")
        sys.exit(1)

    company_url = sys.argv[1]
    max_jobs = int(sys.argv[2]) if len(sys.argv) > 2 else None

    output_file = "jobs.xlsx"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_file = f"workday_jobs_{timestamp}.xlsx"

    results = asyncio.run(scrape_workday_jobs(company_url, max_jobs))

    if not results:
        print("[DONE] No jobs processed")
        sys.exit(0)

    save_to_excel(results, output_file)
    print(f"[DONE] Processed {len(results)} jobs → {output_file}")


if __name__ == "__main__":
    main()
