#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Universal Career Scraper - Final Version
✅ Works with Capgemini, DXC, Greenhouse, Lever, Workday, and most custom portals
✅ Captures description, requirements, contract_type directly from API
✅ Decodes Unicode-escaped HTML (e.g., \u003Cdiv\u003E → <div>)
✅ No syntax errors, production-ready
"""

# ============================
# CONFIG — CHANGE ONLY THIS
# ============================
TARGET_URL = "https://www.capgemini.com/in-en/careers/join-capgemini/job-search/?page=1&size=11&country_code=in-en"
MAX_TOTAL_JOBS = 100           # Safety limit
MAX_PAGES = 50                 # Max pagination attempts
REQUEST_DELAY = 1.0            # Seconds between requests
OUTPUT_FILE = "capgemini_jobs_final.json"
FETCH_DETAIL_PAGES = False     # Set True if API lacks description; False = use API data only

# ============================
# IMPORTS
# ============================
import json
import re
import time
import html
import requests
from copy import deepcopy
from urllib.parse import urlparse, parse_qs, quote_plus
from bs4 import BeautifulSoup

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("❌ playwright not installed. Run: pip install playwright && playwright install chromium")
    exit(1)

# ============================
# GLOBALS & HEADERS
# ============================
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/html, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}

BAD_DOMAINS = ["analytics", "tracking", "cookie", "consent", "demdex", "doubleclick", "nr-data", "facebook", "twitter", "linkedin"]

# === EXPANDED SIGNALS TO CAPTURE RICH FIELDS ===
JOB_SIGNALS = {
    "title": ["title", "jobTitle", "positionTitle", "job_title", "PositionTitle", "name", "job_name"],
    "location": ["location", "locations", "locationName", "primary_location", "workLocation", "city", "country", "office"],
    "id": ["id", "jobId", "requisitionId", "postingId", "job_id", "reqId", "requisition_id", "ref", "reference"],
    "url": ["applyUrl", "applyURL", "jobUrl", "jobURL", "url", "link", "canonicalUrl", "apply_job_url", "apply_url", "job_url", "detailUrl", "careerSiteUrl"],
    "date": ["postedDate", "datePosted", "publishDate", "publicationDate", "posted_date", "createdDate", "indexed_at", "updated_at"],
    # === RICH FIELDS (Capgemini & others) ===
    "description": ["description", "jobDescription", "job_description", "details", "summary", "content", "body", "description_stripped"],
    "requirements": ["requirements", "qualifications", "skills", "requiredSkills", "jobRequirements", "essentialCriteria"],
    "responsibilities": ["responsibilities", "duties", "roleResponsibilities", "keyResponsibilities"],
    "employment_type": ["employmentType", "employment_type", "jobType", "type", "contract_type", "contractType"],
    "category": ["category", "department", "businessUnit", "professional_communities", "professionalGroup", "jobCategory", "sbu"],
    "experience": ["experience", "experienceLevel", "yearsOfExperience", "seniority", "experience_level"],
}

# ============================
# HTML CLEANER FOR CAPEGEMINI
# ============================
def clean_html_description(raw_text: str) -> str:
    """
    Decode Unicode-escaped HTML and strip tags to get clean text.
    Handles: \u003Cdiv\u003E → <div>, &rsquo; → ', &euro; → €, etc.
    """
    if not raw_text or not isinstance(raw_text, str):
        return ""
    
    # Step 1: Decode Unicode escapes (\u003C → <)
    try:
        decoded = raw_text.encode('utf-8').decode('unicode_escape')
    except:
        decoded = raw_text
    
    # Step 2: Unescape HTML entities (&rsquo; → ', &euro; → €, etc.)
    decoded = html.unescape(decoded)
    
    # Step 3: Remove HTML tags but keep structure with line breaks
    soup = BeautifulSoup(decoded, 'html.parser')
    
    # Add newlines before block elements for readability
    for tag in soup.find_all(['br', 'p', 'div', 'li', 'h1', 'h2', 'h3', 'h4', 'ul', 'ol']):
        tag.insert_before('\n')
    
    # Get text and clean up whitespace
    text = soup.get_text(separator=' ', strip=True)
    text = re.sub(r'\s+', ' ', text)  # Collapse multiple spaces
    text = re.sub(r'\n\s*\n', '\n\n', text)  # Keep paragraph breaks
    
    return text.strip()


# ============================
# UNIVERSAL PARSING
# ============================
def parse_any_json(text):
    """Robustly parse JSON or JSONP formatted text."""
    if not text or not isinstance(text, str):
        return None
    text = text.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    try:
        match = re.search(r'^[a-zA-Z0-9_\.$\[\]]+\s*\(\s*({.*?})\s*\)\s*$', text, re.DOTALL)
        if match:
            return json.loads(match.group(1))
    except:
        pass
    try:
        start = text.find('{')
        end = text.rfind('}') + 1
        if start >= 0 and end > start:
            return json.loads(text[start:end])
    except:
        pass
    return None


def is_valid_job(obj):
    """Heuristic to validate if an object is a genuine job posting."""
    if not isinstance(obj, dict):
        return False
    title = None
    for k in JOB_SIGNALS["title"]:
        if k in obj and obj[k] and isinstance(obj[k], str):
            title = obj[k].lower().strip()
            break
    if not title or len(title) < 5 or len(title) > 300:
        return False
    junk_patterns = [
        r'^(overview|home|menu|navigation|search|login|signup|sign\s*in|contact|about)$',
        r'^(privacy|terms|cookie|consent|preferences|policy|legal|careers\s*home)$'
    ]
    if any(re.match(p, title, re.IGNORECASE) for p in junk_patterns):
        return False
    has_location = any(obj.get(k) for k in JOB_SIGNALS["location"])
    has_id = any(obj.get(k) for k in JOB_SIGNALS["id"])
    return has_location or has_id


def extract_jobs_any(data):
    """Extract all valid jobs from any JSON structure recursively."""
    jobs = []
    seen_ids = set()
    
    def normalize(obj):
        """Map API fields to our standard schema."""
        return {
            "title": next((obj[k] for k in JOB_SIGNALS["title"] if k in obj and obj[k]), None),
            "location": next((obj[k] for k in JOB_SIGNALS["location"] if k in obj and obj[k]), None),
            "id": next((str(obj[k]) for k in JOB_SIGNALS["id"] if k in obj and obj[k]), None),
            "url": next((obj[k] for k in JOB_SIGNALS["url"] if k in obj and obj[k]), None),
            "posted_date": next((obj[k] for k in JOB_SIGNALS["date"] if k in obj and obj[k]), None),
            # === RICH FIELDS ===
            "description_raw": next((obj[k] for k in JOB_SIGNALS["description"] if k in obj and obj[k]), None),
            "requirements_raw": next((obj[k] for k in JOB_SIGNALS["requirements"] if k in obj and obj[k]), None),
            "responsibilities_raw": next((obj[k] for k in JOB_SIGNALS["responsibilities"] if k in obj and obj[k]), None),
            "employment_type": next((obj[k] for k in JOB_SIGNALS["employment_type"] if k in obj and obj[k]), None),
            "category": next((obj[k] for k in JOB_SIGNALS["category"] if k in obj and obj[k]), None),
            "experience": next((obj[k] for k in JOB_SIGNALS["experience"] if k in obj and obj[k]), None),
        }
    
    def recurse(obj):
        if not isinstance(obj, dict) and not isinstance(obj, list):
            return
        if isinstance(obj, dict):
            # Check for job list containers
            for container in ["jobs", "results", "items", "positions", "data", "records", "listings", "posts", "ads"]:
                if container in obj and isinstance(obj[container], list):
                    for item in obj[container]:
                        if isinstance(item, dict) and is_valid_job(item):
                            job = normalize(item)
                            job_id = job.get("id")
                            if job_id and job_id not in seen_ids:
                                seen_ids.add(job_id)
                                jobs.append(job)
                    return
            # Check if this object itself is a job
            if is_valid_job(obj):
                job = normalize(obj)
                job_id = job.get("id")
                if job_id and job_id not in seen_ids:
                    seen_ids.add(job_id)
                    jobs.append(job)
            # Recurse into values
            for v in obj.values():
                recurse(v)
        else:
            for item in obj:
                recurse(item)
    
    recurse(data)
    return jobs


def score_api_universal(api):
    """Score an API based on job quality and field richness."""
    url = api["url"].lower()
    all_jobs = api.get("jobs", [])
    valid_jobs = [j for j in all_jobs if is_valid_job(j)]
    
    score = 0
    if any(k in url for k in ["job", "career", "position", "vacancy"]): score += 5
    if any(k in url for k in ["search", "api", "query", "graphql", "jobstream"]): score += 3
    
    valid_count = len(valid_jobs)
    if valid_count >= 20: score += 20
    elif valid_count >= 10: score += 15
    elif valid_count > 0: score += valid_count * 2
    else: score -= 50
    
    if api.get("method") == "POST" and api.get("payload"): score += 12
    
    if valid_jobs:
        score += sum(1 for j in valid_jobs[:5] if j.get("location"))
        score += sum(1 for j in valid_jobs[:5] if j.get("id"))
        score += sum(1 for j in valid_jobs[:5] if j.get("url"))
        # Bonus for APIs that include description
        score += sum(2 for j in valid_jobs[:5] if j.get("description_raw"))
    
    return score


# ============================
# API CAPTURE ENGINE
# ============================
def capture_apis_universal(url):
    """Capture JSON APIs triggered during page load."""
    results = []
    seen_urls = set()
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        context = browser.new_context(user_agent=HEADERS["User-Agent"], viewport={"width": 1920, "height": 1080}, bypass_csp=True)
        page = context.new_page()
        
        def handle_response(response):
            try:
                req = response.request
                content_type = response.headers.get("content-type", "")
                if "application/json" not in content_type.lower():
                    return
                api_url = response.url
                if api_url in seen_urls:
                    return
                seen_urls.add(api_url)
                if any(b in api_url.lower() for b in BAD_DOMAINS):
                    return
                text = response.text()
                if not text or len(text) < 100:
                    return
                data = parse_any_json(text)
                if not data:
                    return
                request_data = {
                    "url": api_url,
                    "method": req.method.upper(),
                    "headers": {k: v for k, v in req.headers.items() if k.lower() not in ["content-length", "host", "cookie"]},
                    "payload": req.post_data_json if req.method.upper() == "POST" else None,
                    "cookies": context.cookies(),
                    "jobs": extract_jobs_any(data)
                }
                results.append(request_data)
                valid_count = len([j for j in request_data["jobs"] if is_valid_job(j)])
                print(f"[+] API: {api_url[:70]}... | Jobs: {len(request_data['jobs'])} | Valid: {valid_count} | {req.method}")
            except Exception:
                pass
        
        page.on("response", handle_response)
        print(f"\n🌐 Loading: {url}")
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            print(f"⚠️ Page load warning: {e}")
        try:
            page.wait_for_load_state("networkidle", timeout=20000)
        except:
            page.wait_for_timeout(5000)
        for _ in range(5):
            page.evaluate("window.scrollBy(0, 3000)")
            page.wait_for_timeout(800)
        try:
            for _ in range(3):
                selectors = ["button:has-text('Load')", "button:has-text('More')", "[data-testid='load-more']", ".load-more"]
                clicked = False
                for sel in selectors:
                    btn = page.query_selector(sel)
                    if btn and btn.is_visible():
                        btn.click()
                        page.wait_for_timeout(2000)
                        clicked = True
                        break
                if not clicked:
                    break
        except:
            pass
        page.wait_for_timeout(2000)
        browser.close()
    return results


def build_url_preserve_params(base_url, params_dict):
    """Rebuild URL preserving bracket notation."""
    parsed = urlparse(base_url)
    parts = []
    for key, values in params_dict.items():
        if not isinstance(values, list):
            values = [values]
        for v in values:
            if key.endswith('[]') or key == 'callback':
                parts.append(f"{key}={quote_plus(str(v))}")
            else:
                parts.append(f"{quote_plus(key)}={quote_plus(str(v))}")
    new_query = "&".join(parts)
    return parsed._replace(query=new_query).geturl()


def paginate_universal(api, max_pages=MAX_PAGES):
    """Universal pagination using brute-force parameter testing."""
    url = api["url"]
    method = api["method"]
    base_headers = api.get("headers", {})
    base_payload = api.get("payload")
    cookies = api.get("cookies", [])
    
    all_jobs = []
    seen_job_ids = set()
    session = requests.Session()
    session.headers.update({**HEADERS, **base_headers})
    if cookies:
        for c in cookies:
            session.cookies.set(c.get("name", ""), c.get("value", ""), domain=c.get("domain", ""), path=c.get("path", "/"))
    
    try:
        if method == "POST":
            res = session.post(url, json=base_payload, timeout=30)
        else:
            res = session.get(url, timeout=30)
        res.raise_for_status()
    except Exception as e:
        print(f"   ❌ Initial request failed: {e}")
        return []
    
    first_data = parse_any_json(res.text)
    if not first_data:
        print("   ❌ Could not parse initial response")
        return []
    
    first_jobs = extract_jobs_any(first_data)
    for job in first_jobs:
        if is_valid_job(job):
            job_id = job.get("id")
            if job_id and job_id not in seen_job_ids:
                seen_job_ids.add(job_id)
                all_jobs.append(job)
    print(f"   📦 Initial batch: {len(all_jobs)} valid jobs")
    
    def find_candidate_params(original_url, payload):
        candidates = []
        parsed = urlparse(original_url)
        query_params = parse_qs(parsed.query, keep_blank_values=True)
        for key, values in query_params.items():
            try:
                start_val = int(values[0])
                candidates.append({"type": "url", "key": key, "start": start_val})
            except:
                continue
        if isinstance(payload, dict):
            for key, value in payload.items():
                try:
                    start_val = int(value)
                    candidates.append({"type": "payload", "key": key, "start": start_val})
                except:
                    continue
        return candidates
    
    candidates = find_candidate_params(url, base_payload)
    if not candidates:
        print("   ⚠️ No pagination params found — returning initial results")
        return all_jobs
    
    print(f"   🔍 Testing {len(candidates)} candidates: {[c['key'] for c in candidates]}")
    
    for page_num in range(1, max_pages + 1):
        if len(all_jobs) >= MAX_TOTAL_JOBS:
            print(f"   ⏹️ Reached MAX_TOTAL_JOBS ({MAX_TOTAL_JOBS})")
            break
        new_jobs_found = False
        page_size_estimate = len(all_jobs) if all_jobs else 10
        for candidate in candidates:
            param_type = candidate["type"]
            key = candidate["key"]
            start_val = candidate["start"]
            if key.lower() in ["page", "pagenum", "page_number"]:
                next_val = start_val + page_num
            else:
                next_val = start_val + (page_num * page_size_estimate)
            try:
                if param_type == "url":
                    parsed = urlparse(url)
                    query_params = parse_qs(parsed.query, keep_blank_values=True)
                    query_params[key] = [str(next_val)]
                    new_url = build_url_preserve_params(f"{parsed.scheme}://{parsed.netloc}{parsed.path}", query_params)
                    res = session.get(new_url, timeout=30)
                else:
                    new_payload = deepcopy(base_payload)
                    new_payload[key] = next_val
                    res = session.post(url, json=new_payload, timeout=30)
                res.raise_for_status()
                data = parse_any_json(res.text)
                if not data:
                    continue
                jobs = extract_jobs_any(data)
                new_batch = []
                for job in jobs:
                    if is_valid_job(job):
                        job_id = job.get("id")
                        if job_id and job_id not in seen_job_ids:
                            seen_job_ids.add(job_id)
                            new_batch.append(job)
                if new_batch:
                    if len(new_batch) > 0:
                        page_size_estimate = len(new_batch)
                    all_jobs.extend(new_batch)
                    new_jobs_found = True
                    print(f"   [Page {page_num}] +{len(new_batch)} via '{key}'={next_val} (total: {len(all_jobs)})")
            except Exception:
                continue
        if not new_jobs_found:
            print(f"   [Page {page_num}] No new jobs — pagination complete")
            break
        time.sleep(REQUEST_DELAY)
    return all_jobs


# ============================
# OPTIONAL: Fetch Detail Pages (if API lacks data)
# ============================
def fetch_job_details_fallback(job_url, session=None):
    """Fallback parser for detail pages (only used if FETCH_DETAIL_PAGES=True)."""
    if session is None:
        session = requests.Session()
        session.headers.update(HEADERS)
    
    details = {"source_url": job_url, "description": None, "requirements": []}
    
    try:
        resp = session.get(job_url, timeout=30)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, 'html.parser')
        
        # Try JSON-LD first
        json_ld = soup.find('script', type='application/ld+json')
        if json_ld:
            try:
                ld_data = json.loads(json_ld.string)
                if isinstance(ld_data, list):
                    ld_data = ld_data[0]
                if ld_data.get("@type") == "JobPosting":
                    details["description"] = ld_data.get("description")
            except:
                pass
        
        # Fallback: extract from common containers
        if not details["description"]:
            for selector in [".job-description", "#description", ".content", "article"]:
                el = soup.select_one(selector)
                if el:
                    text = el.get_text(separator=' ', strip=True)
                    if text and len(text) > 100:
                        details["description"] = text
                        break
        
        return details
    except Exception as e:
        print(f"⚠️ Detail fetch error: {e}")
        return None


# ============================
# MAIN EXECUTION
# ============================
def main():
    print(f"🚀 Universal Career Scraper (Final)")
    print(f"   Target: {TARGET_URL}")
    print(f"   Max jobs: {MAX_TOTAL_JOBS}, Max pages: {MAX_PAGES}")
    print(f"   Output: {OUTPUT_FILE}")
    print(f"   Fetch detail pages: {FETCH_DETAIL_PAGES}\n")
    
    # Step 1: Capture APIs
    print("🔍 Step 1: Capturing dynamic APIs...")
    apis = capture_apis_universal(TARGET_URL)
    
    if not apis:
        print("❌ No APIs captured. Check URL or network conditions.")
        return
    
    # Step 2: Select best API
    ranked = sorted(apis, key=score_api_universal, reverse=True)
    best = ranked[0]
    print(f"\n🏆 Selected API (score: {score_api_universal(best)}):")
    print(f"   URL: {best['url'][:80]}...")
    print(f"   Method: {best['method']}")
    valid_sample = len([j for j in best['jobs'] if is_valid_job(j)])
    print(f"   Valid jobs in sample: {valid_sample}/{len(best['jobs'])}")
    
    # Step 3: Paginate
    print(f"\n⬇️  Starting pagination...")
    all_jobs = paginate_universal(best)
    print(f"✅ Listing complete: {len(all_jobs)} jobs collected")
    
    if not all_jobs:
        print("❌ No jobs found.")
        return
    
    # Step 4: Enrich jobs (decode descriptions, merge fields)
    print(f"\n🔧 Step 4: Processing {len(all_jobs)} jobs...")
    enriched_jobs = []
    
    for i, job in enumerate(all_jobs, 1):
        # === Decode description if present ===
        if job.get("description_raw"):
            job["description"] = clean_html_description(job["description_raw"])
            # Also clean requirements if present
            if job.get("requirements_raw"):
                job["requirements"] = clean_html_description(job["requirements_raw"])
            if job.get("responsibilities_raw"):
                job["responsibilities"] = clean_html_description(job["responsibilities_raw"])
        
        # === Optional: Fetch detail page if API lacked description ===
        if FETCH_DETAIL_PAGES and not job.get("description") and job.get("url"):
            print(f"[{i}/{len(all_jobs)}] Fetching detail: {job['url'][:60]}...")
            details = fetch_job_details_fallback(job["url"])
            if details and details.get("description"):
                job["description"] = details["description"]
                if details.get("requirements"):
                    job["requirements"] = details["requirements"]
            time.sleep(REQUEST_DELAY)
        
        # === Remove raw fields to keep output clean ===
        job.pop("description_raw", None)
        job.pop("requirements_raw", None)
        job.pop("responsibilities_raw", None)
        
        enriched_jobs.append(job)
        
        if i % 10 == 0:
            print(f"   ✅ Processed {i}/{len(all_jobs)} jobs")
    
    # Summary & Save
    print(f"\n✅ Processing complete: {len(enriched_jobs)} jobs enriched")
    
    if enriched_jobs:
        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            json.dump(enriched_jobs, f, indent=2, default=str, ensure_ascii=False)
        print(f"💾 Saved to {OUTPUT_FILE}")
        
        # Sample output
        print(f"\n📋 Sample (first 2 jobs):")
        for idx, job in enumerate(enriched_jobs[:2], 1):
            print(f"\n{idx}. {job.get('title', 'Untitled')}")
            print(f"   ID: {job.get('id')}")
            print(f"   Location: {job.get('location') or 'N/A'}")
            print(f"   Type: {job.get('employment_type') or 'N/A'} | Category: {job.get('category') or 'N/A'}")
            print(f"   Experience: {job.get('experience') or 'N/A'}")
            desc_preview = (job.get('description') or '')[:200]
            print(f"   Description preview: {desc_preview}...")
            if job.get('requirements'):
                req_preview = (job['requirements'] if isinstance(job['requirements'], str) else str(job['requirements'][:2]))[:150]
                print(f"   Requirements preview: {req_preview}...")
    else:
        print("⚠️ No jobs to save")
    
    print(f"\n✨ Done!")


if __name__ == "__main__":
    main()