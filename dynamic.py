#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DXC Careers Scraper - Full Pipeline
Fetches job listings via dynamic API capture, then enriches each job with detail page content.
"""

# ============================
# CONFIG — CHANGE ONLY THIS
# ============================
TARGET_URL = "https://careers.smartrecruiters.com/Bosch-HomeComfort?search=India"
MAX_TOTAL_JOBS = 10          # Safety limit to prevent infinite loops
MAX_PAGES = 100                # Maximum number of pages to attempt
REQUEST_DELAY = 1.0            # Delay between requests (polite scraping)
OUTPUT_FILE = "dxc_jobs_full.json"

# ============================
# IMPORTS
# ============================
import json
import re
import time
import requests
from copy import deepcopy
from urllib.parse import urlparse, parse_qs, quote_plus
from bs4 import BeautifulSoup

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("❌ playwright not installed. Run: pip install playwright")
    print("   Then run: playwright install chromium")
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
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
}

BAD_DOMAINS = ["analytics", "tracking", "cookie", "consent", "demdex", "doubleclick", "nr-data", "facebook", "twitter", "linkedin"]

PAGINATION_KEYWORDS = ["offset", "start", "page", "skip", "from", "limit", "size", "rows", "pagenum"]

JOB_SIGNALS = {
    "title": ["title", "jobTitle", "positionTitle", "job_title", "PositionTitle", "name"],
    "location": ["location", "locations", "locationName", "primary_location", "workLocation", "city", "country"],
    "id": ["id", "jobId", "requisitionId", "postingId", "job_id", "reqId", "requisition_id"],
    "url": ["applyUrl", "applyURL", "jobUrl", "jobURL", "url", "link", "canonicalUrl"],
    "date": ["postedDate", "datePosted", "publishDate", "publicationDate", "posted_date", "createdDate"]
}

# ============================
# UNIVERSAL PARSING AND VALIDATION
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
    # Try JSONP pattern: callback({...})
    try:
        match = re.search(r'^[a-zA-Z0-9_\.$\[\]]+\s*\(\s*({.*?})\s*\)\s*$', text, re.DOTALL)
        if match:
            return json.loads(match.group(1))
    except:
        pass
    # Try extracting JSON object from mixed content
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
    # Check for valid title
    title = None
    for k in JOB_SIGNALS["title"]:
        if k in obj and obj[k] and isinstance(obj[k], str):
            title = obj[k].lower().strip()
            break
    if not title or len(title) < 5 or len(title) > 200:
        return False
    # Filter out junk/nav titles
    junk_patterns = [
        r'^(overview|home|menu|navigation|search|login|signup|sign\s*in|contact|about)$',
        r'^(privacy|terms|cookie|consent|preferences|policy|legal|careers\s*home)$'
    ]
    if any(re.match(p, title, re.IGNORECASE) for p in junk_patterns):
        return False
    # Must have location OR id
    has_location = any(obj.get(k) for k in JOB_SIGNALS["location"])
    has_id = any(obj.get(k) for k in JOB_SIGNALS["id"])
    return has_location or has_id


def extract_jobs_any(data):
    """Extract all valid jobs from any JSON structure recursively."""
    jobs = []
    seen_ids = set()
    
    def normalize(obj):
        return {
            "title": next((obj[k] for k in JOB_SIGNALS["title"] if k in obj and obj[k]), None),
            "location": next((obj[k] for k in JOB_SIGNALS["location"] if k in obj and obj[k]), None),
            "id": next((str(obj[k]) for k in JOB_SIGNALS["id"] if k in obj and obj[k]), None),
            "url": next((obj[k] for k in JOB_SIGNALS["url"] if k in obj and obj[k]), None),
            "posted_date": next((obj[k] for k in JOB_SIGNALS["date"] if k in obj and obj[k]), None),
        }
    
    def recurse(obj):
        if not isinstance(obj, dict) and not isinstance(obj, list):
            return
        if isinstance(obj, dict):
            # Check for common job list containers
            for container in ["jobs", "results", "items", "positions", "data", "records", "listings", "posts"]:
                if container in obj and isinstance(obj[container], list):
                    for item in obj[container]:
                        if isinstance(item, dict) and is_valid_job(item):
                            job = normalize(item)
                            job_id = job.get("id")
                            if job_id and job_id not in seen_ids:
                                seen_ids.add(job_id)
                                jobs.append(job)
                    return  # Stop deeper recursion if we found a container
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
        else:  # list
            for item in obj:
                recurse(item)
    
    recurse(data)
    return jobs


def score_api_universal(api):
    """Score an API based on job quality and relevance."""
    url = api["url"].lower()
    all_jobs = api.get("jobs", [])
    valid_jobs = [j for j in all_jobs if is_valid_job(j)]
    
    score = 0
    # URL relevance
    if any(k in url for k in ["job", "career", "position", "vacancy"]):
        score += 5
    if any(k in url for k in ["search", "api", "query", "graphql"]):
        score += 3
    # Job count quality
    valid_count = len(valid_jobs)
    if valid_count >= 20:
        score += 20
    elif valid_count >= 10:
        score += 15
    elif valid_count > 0:
        score += valid_count * 2
    else:
        score -= 50
    # POST with payload is often more reliable
    if api.get("method") == "POST" and api.get("payload"):
        score += 12
    # Bonus for jobs with location and id
    if valid_jobs:
        score += sum(1 for j in valid_jobs[:5] if j.get("location"))
        score += sum(1 for j in valid_jobs[:5] if j.get("id"))
    return score


# ============================
# API CAPTURE ENGINE (Playwright)
# ============================
def capture_apis_universal(url):
    """Capture JSON APIs triggered during page load with exact request data."""
    results = []
    seen_urls = set()
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        context = browser.new_context(
            user_agent=HEADERS["User-Agent"],
            viewport={"width": 1920, "height": 1080},
            bypass_csp=True
        )
        page = context.new_page()
        
        def handle_response(response):
            try:
                req = response.request
                content_type = response.headers.get("content-type", "")
                # Only process JSON responses
                if "application/json" not in content_type.lower():
                    return
                api_url = response.url
                if api_url in seen_urls:
                    return
                seen_urls.add(api_url)
                # Filter out tracking/analytics domains
                if any(b in api_url.lower() for b in BAD_DOMAINS):
                    return
                text = response.text()
                if not text or len(text) < 50:
                    return
                data = parse_any_json(text)
                if not data:
                    return
                # Build request snapshot
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
            except Exception as e:
                # Silent fail for non-critical errors
                pass
        
        page.on("response", handle_response)
        
        print(f"\n🌐 Loading target page: {url}")
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            print(f"⚠️ Page load warning: {e}")
        
        # Wait for network to settle
        try:
            page.wait_for_load_state("networkidle", timeout=20000)
        except:
            page.wait_for_timeout(5000)
        
        # Simulate scrolling to trigger lazy-loaded APIs
        for _ in range(5):
            page.evaluate("window.scrollBy(0, 3000)")
            page.wait_for_timeout(800)
        
        # Try clicking "Load More" buttons if present
        try:
            for _ in range(3):
                selectors = [
                    "button:has-text('Load')",
                    "button:has-text('More')",
                    "[data-testid='load-more']",
                    ".load-more",
                    "#load-more"
                ]
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
    """Rebuild URL preserving bracket notation and special params."""
    parsed = urlparse(base_url)
    parts = []
    for key, values in params_dict.items():
        if not isinstance(values, list):
            values = [values]
        for v in values:
            # Preserve bracket notation for arrays: city[]=Value
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
    
    # Setup session with cookies and headers
    session = requests.Session()
    session.headers.update({**HEADERS, **base_headers})
    if cookies:
        for c in cookies:
            session.cookies.set(
                c.get("name", ""), 
                c.get("value", ""), 
                domain=c.get("domain", ""), 
                path=c.get("path", "/")
            )
    
    # Initial request
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
        print("   ❌ Could not parse initial response as JSON")
        return []
    
    # Extract jobs from first response
    first_jobs = extract_jobs_any(first_data)
    for job in first_jobs:
        if is_valid_job(job):
            job_id = job.get("id")
            if job_id and job_id not in seen_job_ids:
                seen_job_ids.add(job_id)
                all_jobs.append(job)
    print(f"   📦 Initial batch: {len(all_jobs)} valid jobs")
    
    # Find pagination parameters
    def find_candidate_params(original_url, payload):
        candidates = []
        parsed = urlparse(original_url)
        query_params = parse_qs(parsed.query, keep_blank_values=True)
        
        # Check URL query params
        for key, values in query_params.items():
            try:
                start_val = int(values[0])
                candidates.append({"type": "url", "key": key, "start": start_val})
            except (ValueError, IndexError):
                continue
        
        # Check POST payload params
        if isinstance(payload, dict):
            for key, value in payload.items():
                try:
                    start_val = int(value)
                    candidates.append({"type": "payload", "key": key, "start": start_val})
                except (ValueError, TypeError):
                    continue
        return candidates
    
    candidates = find_candidate_params(url, base_payload)
    if not candidates:
        print("   ⚠️ No numeric pagination parameters found — returning initial results")
        return all_jobs
    
    print(f"   🔍 Testing {len(candidates)} pagination candidates: {[c['key'] for c in candidates]}")
    
    # Paginate through results
    for page_num in range(1, max_pages + 1):
        if len(all_jobs) >= MAX_TOTAL_JOBS:
            print(f"   ⏹️ Reached MAX_TOTAL_JOBS limit ({MAX_TOTAL_JOBS})")
            break
        
        new_jobs_found = False
        page_size_estimate = len(all_jobs) if all_jobs else 10
        
        for candidate in candidates:
            param_type = candidate["type"]
            key = candidate["key"]
            start_val = candidate["start"]
            
            # Calculate next value
            if key.lower() in ["page", "pagenum", "page_number"]:
                next_val = start_val + page_num
            else:
                next_val = start_val + (page_num * page_size_estimate)
            
            try:
                if param_type == "url":
                    # Modify URL query param
                    parsed = urlparse(url)
                    query_params = parse_qs(parsed.query, keep_blank_values=True)
                    query_params[key] = [str(next_val)]
                    new_url = build_url_preserve_params(
                        f"{parsed.scheme}://{parsed.netloc}{parsed.path}", 
                        query_params
                    )
                    res = session.get(new_url, timeout=30)
                else:
                    # Modify POST payload
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
                    
            except Exception as e:
                # Continue trying other candidates
                continue
        
        if not new_jobs_found:
            print(f"   [Page {page_num}] No new jobs found — pagination complete")
            break
        
        time.sleep(REQUEST_DELAY)
    
    return all_jobs


# ============================
# JOB DETAIL FETCHER (HTML Parsing)
# ============================
def fetch_job_details(job_url, session=None):
    """
    Fetch and parse full job details from DXC careers job detail page.
    Returns dict with description, requirements, location, etc.
    """
    if session is None:
        session = requests.Session()
        session.headers.update(HEADERS)
    
    details = {
        "source_url": job_url,
        "title": None,
        "job_id": None,
        "category": None,
        "employment_type": None,
        "location_text": None,
        "description": None,
        "requirements": [],
        "responsibilities": [],
        "qualifications": [],
        "posted_date": None,
        "apply_url": job_url
    }
    
    try:
        resp = session.get(job_url, timeout=30)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, 'html.parser')
        
        # Extract title from h1
        title_el = soup.find('h1')
        if title_el:
            details["title"] = title_el.get_text(strip=True)
        
        # Get full page text for pattern matching
        page_text = soup.get_text(separator='\n', strip=False)
        
        # Extract Job ID (pattern: "Job ID: XXXXXXX")
        job_id_match = re.search(r'Job\s*ID[:\s]+(\d+)', page_text, re.IGNORECASE)
        if job_id_match:
            details["job_id"] = job_id_match.group(1)
        
        # Extract Category
        category_match = re.search(r'Category[:\s]+([^\n]+)', page_text, re.IGNORECASE)
        if category_match:
            details["category"] = category_match.group(1).strip()
        
        # Extract Employment Type
        emp_match = re.search(r'Employment\s*Type[:\s]+([^\n]+)', page_text, re.IGNORECASE)
        if emp_match:
            details["employment_type"] = emp_match.group(1).strip()
        
        # Extract Location (human-readable)
        loc_match = re.search(r'Location[:\s]+([^\n]+)', page_text, re.IGNORECASE)
        if loc_match:
            details["location_text"] = loc_match.group(1).strip()
        
        # Extract Job Description
        # Look for section headers and capture content until next header
        desc_patterns = [
            r'Job\s*Description[:\s]*',
            r'About\s*the\s*Role[:\s]*',
            r'Role\s*Description[:\s]*',
            r'Position\s*Overview[:\s]*'
        ]
        
        for pattern in desc_patterns:
            match = re.search(pattern, page_text, re.IGNORECASE)
            if match:
                start_pos = match.end()
                # Find next major section header
                next_header = re.search(r'\n\s*(Requirements|Qualifications|Responsibilities|Skills|What\s*You\'ll\s*Do|About\s*You|How\s*to\s*Apply)[:\s]*', page_text[start_pos:], re.IGNORECASE)
                if next_header:
                    end_pos = start_pos + next_header.start()
                else:
                    end_pos = len(page_text)
                desc_text = page_text[start_pos:end_pos].strip()
                if desc_text and len(desc_text) > 50:
                    details["description"] = desc_text
                break
        
        # If description not found, try CSS-based extraction
        if not details["description"]:
            # Common class names for job description containers
            desc_selectors = [
                ".job-description", "#job-description", "[data-field='description']",
                ".content", ".job-details", "article"
            ]
            for selector in desc_selectors:
                desc_el = soup.select_one(selector)
                if desc_el:
                    text = desc_el.get_text(separator=' ', strip=True)
                    if text and len(text) > 100:
                        details["description"] = text
                        break
        
        # Extract Requirements/Qualifications (bullet points)
        req_patterns = [
            r'(?:Requirements|Qualifications|What\s*You\'ll\s*Need|Skills\s*&\s*Experience)[:\s]*',
        ]
        for pattern in req_patterns:
            match = re.search(pattern, page_text, re.IGNORECASE)
            if match:
                start_pos = match.end()
                # Look for next section or end of relevant content
                next_section = re.search(r'\n\s*(?:Responsibilities|How\s*to\s*Apply|Benefits|About\s*Us|Company)[:\s]*', page_text[start_pos:], re.IGNORECASE)
                if next_section:
                    end_pos = start_pos + next_section.start()
                else:
                    end_pos = len(page_text)
                req_block = page_text[start_pos:end_pos].strip()
                
                # Extract bullet points
                bullets = re.findall(r'[\-\•\*]\s*([^\n]+)', req_block)
                if bullets:
                    details["requirements"] = [b.strip() for b in bullets if len(b.strip()) > 10]
                break
        
        # Fallback: extract list items from HTML if bullets not found in text
        if not details["requirements"]:
            # Look for ul/ol near requirement headers
            req_header = soup.find(string=lambda text: text and any(kw in text.lower() for kw in ["requirement", "qualification", "skill"]))
            if req_header:
                list_container = req_header.find_parent().find_next(['ul', 'ol'])
                if list_container:
                    items = list_container.find_all('li')
                    details["requirements"] = [li.get_text(strip=True) for li in items if li.get_text(strip=True) and len(li.get_text(strip=True)) > 10]
        
        # Extract Responsibilities (similar approach)
        resp_match = re.search(r'Responsibilities[:\s]+', page_text, re.IGNORECASE)
        if resp_match:
            start_pos = resp_match.end()
            next_section = re.search(r'\n\s*(?:Qualifications|Requirements|How\s*to\s*Apply|Benefits)[:\s]*', page_text[start_pos:], re.IGNORECASE)
            if next_section:
                end_pos = start_pos + next_section.start()
            else:
                end_pos = len(page_text)
            resp_block = page_text[start_pos:end_pos].strip()
            bullets = re.findall(r'[\-\•\*]\s*([^\n]+)', resp_block)
            if bullets:
                details["responsibilities"] = [b.strip() for b in bullets if len(b.strip()) > 10]
        
        # Extract posted date if available
        date_patterns = [
            r'Posted[:\s]+([A-Za-z]+\s+\d{1,2},?\s+\d{4})',
            r'Posted[:\s]+(\d{1,2}/\d{1,2}/\d{4})',
            r'Posted[:\s]+(\d{4}-\d{2}-\d{2})'
        ]
        for pattern in date_patterns:
            date_match = re.search(pattern, page_text, re.IGNORECASE)
            if date_match:
                details["posted_date"] = date_match.group(1)
                break
        
        return details
        
    except requests.RequestException as e:
        print(f"⚠️ HTTP error fetching {job_url}: {e}")
        return None
    except Exception as e:
        print(f"⚠️ Parse error for {job_url}: {e}")
        return None


# ============================
# MAIN EXECUTION
# ============================
def main():
    print(f"🚀 DXC Careers Full Scraper")
    print(f"   Target: {TARGET_URL}")
    print(f"   Max jobs: {MAX_TOTAL_JOBS}, Max pages: {MAX_PAGES}")
    print(f"   Output: {OUTPUT_FILE}\n")
    
    # Step 1: Capture APIs from page load
    print("🔍 Step 1: Capturing dynamic APIs...")
    apis = capture_apis_universal(TARGET_URL)
    
    if not apis:
        print("❌ No APIs captured. Check the URL, network conditions, or increase wait times.")
        # Fallback: try direct scraping of listing page HTML
        print("💡 Trying fallback: parsing listing page HTML directly...")
        try:
            session = requests.Session()
            session.headers.update(HEADERS)
            resp = session.get(TARGET_URL, timeout=30)
            resp.raise_for_status()
            # Look for embedded JSON in script tags
            soup = BeautifulSoup(resp.text, 'html.parser')
            scripts = soup.find_all('script', type='application/json')
            for script in scripts:
                data = parse_any_json(script.string)
                if data:
                    jobs = extract_jobs_any(data)
                    if jobs:
                        print(f"✅ Found {len(jobs)} jobs in embedded JSON")
                        all_jobs = jobs
                        break
            else:
                print("❌ Fallback also failed. Please verify the target URL is accessible.")
                return
        except Exception as e:
            print(f"❌ Fallback failed: {e}")
            return
    else:
        # Step 2: Rank and select best API
        ranked = sorted(apis, key=score_api_universal, reverse=True)
        best = ranked[0]
        print(f"\n🏆 Selected API (score: {score_api_universal(best)}):")
        print(f"   URL: {best['url'][:80]}...")
        print(f"   Method: {best['method']}")
        valid_sample = len([j for j in best['jobs'] if is_valid_job(j)])
        print(f"   Valid jobs in sample: {valid_sample}/{len(best['jobs'])}")
        
        # Step 3: Paginate to collect all jobs
        print(f"\n⬇️  Starting universal pagination...")
        all_jobs = paginate_universal(best)
        print(f"✅ Listing phase complete: {len(all_jobs)} valid jobs collected")
    
    if not all_jobs:
        print("❌ No jobs found. Exiting.")
        return
    
    # Step 4: Enrich each job with detail page content
    print(f"\n🔍 Step 4: Fetching details for {len(all_jobs)} jobs...")
    detailed_jobs = []
    detail_session = requests.Session()
    detail_session.headers.update(HEADERS)
    
    for i, job in enumerate(all_jobs, 1):
        job_url = job.get("url")
        if not job_url:
            print(f"[{i}/{len(all_jobs)}] ⚠️ Skipping job without URL")
            continue
        
        print(f"[{i}/{len(all_jobs)}] Fetching: {job_url[:70]}...")
        
        details = fetch_job_details(job_url, session=detail_session)
        if details:
            # Merge listing data with detail data (detail data takes precedence for conflicts)
            merged = {**job, **details}
            detailed_jobs.append(merged)
            print(f"   ✅ Title: {details.get('title', 'N/A')[:60]}")
        else:
            print(f"   ⚠️ Failed to fetch details")
        
        # Polite delay to avoid rate limiting
        if i < len(all_jobs):
            time.sleep(REQUEST_DELAY)
    
    # Summary
    success_count = len(detailed_jobs)
    print(f"\n✅ Detail enrichment complete: {success_count}/{len(all_jobs)} jobs successfully fetched")
    
    # Save results
    if detailed_jobs:
        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            json.dump(detailed_jobs, f, indent=2, default=str, ensure_ascii=False)
        print(f"💾 Saved {len(detailed_jobs)} enriched jobs to {OUTPUT_FILE}")
        
        # Print sample
        print(f"\n📋 Sample output (first 3 jobs):")
        for idx, job in enumerate(detailed_jobs[:3], 1):
            print(f"\n{idx}. {job.get('title', 'Untitled')}")
            print(f"   ID: {job.get('id')} | Job ID: {job.get('job_id')}")
            print(f"   Location: {job.get('location_text') or job.get('location') or 'N/A'}")
            print(f"   Type: {job.get('employment_type') or 'N/A'} | Category: {job.get('category') or 'N/A'}")
            print(f"   Description preview: {(job.get('description') or '')[:150]}...")
            print(f"   Requirements: {len(job.get('requirements', []))} items")
    else:
        print("⚠️ No detailed jobs to save")
    
    print(f"\n✨ Done!")


if __name__ == "__main__":
    main()