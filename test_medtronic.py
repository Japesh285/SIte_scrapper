import re
import json
import asyncio
import httpx
from urllib.parse import urlparse

# 🔧 ================= CONFIG =================
CAREERS_URL = "https://aig.wd1.myworkdayjobs.com/aig"
MAX_JOBS = 20
OUTPUT_FILE = "workday_jobs.json"
# ============================================


def parse_workday_url(url: str):
    """Extract tenant, server, and site from Workday URL"""
    pattern = r"https://([^.]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([^/?]+)"
    match = re.match(pattern, url)

    if not match:
        raise ValueError(f"❌ Could not parse Workday URL: {url}")

    tenant, server, site = match.groups()

    return {
        "tenant": tenant,
        "server": server,
        "site": site,
        "base": f"https://{tenant}.{server}.myworkdayjobs.com"
    }


def normalize_external_path(path: str) -> str:
    """🔥 CRITICAL FIX for your current bug"""
    if not path:
        return ""

    path = path.strip()
    path = path.lstrip("/")  # remove leading /

    if path.startswith("job/"):
        path = path[len("job/"):]

    return path


async def fetch_listings(client, config):
    """Fetch job listings"""
    url = f"{config['base']}/wday/cxs/{config['tenant']}/{config['site']}/jobs"

    print(f"🚀 Listings API: {url}")

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0",
        "Referer": f"{config['base']}/en-US/{config['site']}",
        "Origin": config["base"]
    }

    payload = {
        "limit": MAX_JOBS,
        "offset": 0,
        "appliedFacets": {},
        "searchText": ""
    }

    res = await client.post(url, json=payload, headers=headers)
    res.raise_for_status()

    data = res.json()
    jobs = data.get("jobPostings", [])

    print(f"📦 Fetched {len(jobs)} jobs")

    return jobs


async def fetch_job_detail(client, config, external_path):
    """Fetch job detail using correct API"""
    clean_path = normalize_external_path(external_path)

    url = f"{config['base']}/wday/cxs/{config['tenant']}/{config['site']}/job/{clean_path}"

    print(f"🔎 Fetching: {url}")

    headers = {
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0",
        "Referer": f"{config['base']}/en-US/{config['site']}"
    }

    try:
        res = await client.get(url, headers=headers)
        res.raise_for_status()
        return res.json()
    except Exception as e:
        print(f"⚠️ Failed: {e}")
        return None


def extract_job(listing, detail):
    """Combine listing + detail into clean structure"""
    job = {
        "title": listing.get("title"),
        "location": listing.get("locationsText"),
        "posted_date": listing.get("postedOn"),
        "external_path": listing.get("externalPath"),
        "detail_url": None,
        "description": "",
        "_raw_listing": listing,
        "_raw_detail": detail
    }

    if detail:
        job_info = detail.get("jobPostingInfo", {})

        job["description"] = job_info.get("jobDescription", "")
        job["detail_url"] = job_info.get("externalUrl")

    return job


async def main():
    print(f"🚀 Scraping: {CAREERS_URL}")

    config = parse_workday_url(CAREERS_URL)

    print(f"🔧 Parsed Config: {config}")

    results = []

    async with httpx.AsyncClient(timeout=30.0) as client:
        listings = await fetch_listings(client, config)

        for i, job in enumerate(listings[:MAX_JOBS], 1):
            external_path = job.get("externalPath")

            if not external_path:
                continue

            detail = await fetch_job_detail(client, config, external_path)

            combined = extract_job(job, detail)
            results.append(combined)

            print(f"✅ [{i}] {combined['title']} | Desc length: {len(combined['description'])}")

    # 💾 Save JSON
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    print(f"\n💾 Saved {len(results)} jobs → {OUTPUT_FILE}")


if __name__ == "__main__":
    asyncio.run(main())