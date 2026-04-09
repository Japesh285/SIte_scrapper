# ============================
# CONFIG
# ============================
TARGET_URL = "https://careers.dxc.com/job-search-results/?compliment[]=India"

# ============================
# IMPORTS
# ============================
from playwright.sync_api import sync_playwright
import requests
import json
import re
import urllib.parse as urlparse
import time

HEADERS = {"User-Agent": "Mozilla/5.0"}

BAD_DOMAINS = ["cookielaw", "nr-data", "analytics", "consent", "tracking"]

KEYWORDS = ["job", "career", "position", "opening", "search", "requisition"]

# Job field name variations for schema-aware extraction
JOB_TITLE_KEYS = ["title", "jobTitle", "positionTitle", "PositionTitle", "job_title"]
JOB_LOCATION_KEYS = ["location", "locations", "locationName", "primary_location", "workLocation"]
JOB_ID_KEYS = ["id", "jobId", "requisitionId", "postingId", "job_id", "reqId"]
JOB_URL_KEYS = ["applyUrl", "applyURL", "jobUrl", "jobURL", "url", "link"]
JOB_DATE_KEYS = ["postedDate", "datePosted", "publishDate", "publicationDate", "posted_date"]


# ============================
# JSON + JSONP PARSER
# ============================
def parse_json_safe(text):
    try:
        return json.loads(text)
    except:
        try:
            # Handle JSONP: callback({...})
            json_str = re.search(r'\((.*)\)', text, re.DOTALL).group(1)
            return json.loads(json_str)
        except:
            return None


# ============================
# FILTER BAD APIs
# ============================
def is_valid_api(url):
    url_lower = url.lower()
    # Block tracking/analytics domains
    if any(b in url_lower for b in BAD_DOMAINS):
        return False
    # Block static assets
    if any(ext in url_lower for ext in [".css", ".js", ".png", ".jpg", ".svg", ".woff"]):
        return False
    return True


# ============================
# JOB VALIDATOR (CRITICAL FIX)
# ============================
def is_valid_job(job):
    """
    Validate that extracted object is actually a job posting.
    Filters out nav items, menu labels, UI state, etc.
    """
    if not isinstance(job, dict):
        return False
    
    # Get title with fallbacks
    title = None
    for key in JOB_TITLE_KEYS:
        if key in job and job[key]:
            title = str(job[key]).lower().strip()
            break
    
    if not title or len(title) < 5 or len(title) > 200:
        return False
    
    # Reject navigation/UI junk patterns
    bad_patterns = [
        r'^(overview|home|menu|navigation|search|login|signup|sign\s*in|sign\s*up|contact|about)$',
        r'^(privacy|terms|cookie|consent|preferences|policy|legal)$',
        r'^(load\s*more|show\s*more|view\s*all|see\s*all|explore|browse)$',
        r'^(ai|cloud|data|security|consulting|services|solutions|products)$',
        r'^(careers|students|professionals|leadership|culture)$',
        r'^[a-z]{1,4}$',  # Too short, likely abbreviations
        r'^\d+$'  # Just numbers
    ]
    if any(re.match(p, title, re.IGNORECASE) for p in bad_patterns):
        return False
    
    # Must have at least ONE strong signal: location, ID, or date
    has_location = any(job.get(k) for k in JOB_LOCATION_KEYS)
    has_id = any(job.get(k) for k in JOB_ID_KEYS)
    has_date = any(job.get(k) for k in JOB_DATE_KEYS)
    has_apply_url = any(job.get(k) for k in JOB_URL_KEYS)
    
    if not (has_location or has_id or has_date or has_apply_url):
        return False
    
    return True


# ============================
# SCHEMA-AWARE JOB EXTRACTOR
# ============================
def extract_jobs(data):
    """
    Extract jobs with schema-aware detection.
    Handles varied field names and nested structures.
    """
    jobs = []
    seen_ids = set()  # Prevent duplicates
    
    def get_field(obj, keys):
        """Get first non-null value from list of possible keys"""
        for key in keys:
            if key in obj and obj[key]:
                return obj[key]
        return None
    
    def is_job_like(obj):
        """Heuristic: does this dict look like a job posting?"""
        if not isinstance(obj, dict):
            return False
        # Count job-like signals
        signals = 0
        if any(k in obj for k in JOB_TITLE_KEYS): signals += 2
        if any(k in obj for k in JOB_LOCATION_KEYS): signals += 1
        if any(k in obj for k in JOB_ID_KEYS): signals += 1
        if any(k in obj for k in JOB_URL_KEYS): signals += 1
        if any(k in obj for k in JOB_DATE_KEYS): signals += 1
        return signals >= 2  # Need 2+ signals to be confident
    
    def normalize_job(obj):
        """Extract and normalize job fields from varied schemas"""
        return {
            "title": get_field(obj, JOB_TITLE_KEYS),
            "location": get_field(obj, JOB_LOCATION_KEYS),
            "id": get_field(obj, JOB_ID_KEYS),
            "url": get_field(obj, JOB_URL_KEYS),
            "posted_date": get_field(obj, JOB_DATE_KEYS),
            "_raw_keys": list(obj.keys())  # For debugging
        }
    
    def recurse(obj, depth=0):
        if depth > 5:  # Prevent infinite recursion on deep structures
            return
            
        if isinstance(obj, dict):
            # PRIORITY 1: Check for known job list containers
            for container_key in ['jobs', 'results', 'items', 'positions', 'listings', 'data', 'records']:
                if container_key in obj and isinstance(obj[container_key], list):
                    candidates = [i for i in obj[container_key] if isinstance(i, dict) and is_job_like(i)]
                    if len(candidates) >= 2:  # Confident this is a job list
                        for item in candidates:
                            job = normalize_job(item)
                            job_id = job.get("id")
                            if job_id and job_id in seen_ids:
                                continue
                            if job_id:
                                seen_ids.add(job_id)
                            jobs.append(job)
                        return  # Don't recurse into confirmed job list
            
            # PRIORITY 2: Check if current dict is itself a job
            if is_job_like(obj):
                job = normalize_job(obj)
                job_id = job.get("id")
                if not job_id or job_id not in seen_ids:
                    if job_id:
                        seen_ids.add(job_id)
                    jobs.append(job)
            
            # PRIORITY 3: Recurse into values
            for v in obj.values():
                recurse(v, depth + 1)
                
        elif isinstance(obj, list):
            # Check if list contains jobs directly
            if len(obj) > 0 and isinstance(obj[0], dict):
                candidates = [i for i in obj if isinstance(i, dict) and is_job_like(i)]
                if len(candidates) >= 2:
                    for item in candidates:
                        job = normalize_job(item)
                        job_id = job.get("id")
                        if job_id and job_id in seen_ids:
                            continue
                        if job_id:
                            seen_ids.add(job_id)
                        jobs.append(job)
                    return
            # Otherwise recurse into list items
            for item in obj:
                recurse(item, depth + 1)
    
    recurse(data)
    return jobs


# ============================
# QUALITY-WEIGHTED API SCORER
# ============================
def score_api(api):
    """
    Score APIs prioritizing job quality over quantity.
    Prevents fake-heavy APIs from winning.
    """
    url = api["url"].lower()
    all_jobs = api.get("jobs", [])
    valid_jobs = [j for j in all_jobs if is_valid_job(j)]
    method = api.get("method", "GET")
    payload = api.get("payload")
    
    score = 0
    
    # === URL SIGNALS (Strong indicators) ===
    if any(k in url for k in ["job", "career", "position", "requisition", "vacancy"]):
        score += 5
    if any(k in url for k in ["search", "query", "api", "graphql"]):
        score += 3
    
    # === QUALITY OVER QUANTITY ===
    valid_count = len(valid_jobs)
    total_count = len(all_jobs)
    
    if valid_count >= 20:
        score += 20  # Excellent
    elif valid_count >= 10:
        score += 15  # Good
    elif valid_count >= 3:
        score += 8   # Acceptable
    elif valid_count > 0:
        score += valid_count * 2  # Minimal
    else:
        score -= 50  # Heavy penalty: zero valid jobs
    
    # Penalize high junk ratio (many extracted, few valid)
    if total_count > 30 and valid_count / max(total_count, 1) < 0.3:
        score -= 25
    
    # === API CHARACTERISTICS ===
    if method == "POST" and payload:
        score += 12  # POST with payload = likely real search API
    
    # Bonus for pagination support in payload
    if payload and any(k in payload for k in ["offset", "start", "page", "skip", "from", "limit", "rows"]):
        score += 6
    
    # Bonus for rich job data (location + id + url present)
    if valid_jobs:
        sample = valid_jobs[:5]
        richness_score = 0
        for job in sample:
            if job.get("location"): richness_score += 1
            if job.get("id"): richness_score += 2
            if job.get("url"): richness_score += 1
        score += richness_score
    
    # === PENALTIES ===
    if any(b in url for b in BAD_DOMAINS):
        score -= 100  # Hard block
    
    # Penalize suspiciously high counts (likely UI state, not jobs)
    if total_count > 200:
        score -= 15
    
    return score


# ============================
# DEBUG HELPER: Inspect API Sample
# ============================
def inspect_api_sample(api):
    """Debug helper to inspect what was extracted from an API"""
    valid = [j for j in api.get("jobs", []) if is_valid_job(j)]
    print(f"\n🔍 API: {api['url'][:70]}...")
    print(f"   Method: {api.get('method', 'GET')}")
    print(f"   Payload keys: {list(api.get('payload', {}).keys()) if api.get('payload') else 'N/A'}")
    print(f"   Raw extracted: {len(api.get('jobs', []))} | Valid after filter: {len(valid)}")
    
    if valid:
        print("   ✅ Sample valid jobs:")
        for job in valid[:3]:
            loc = job.get('location') or 'N/A'
            title = (job.get('title') or '')[:50]
            print(f"      • {title} @ {loc}")
    elif api.get('jobs'):
        print("   ❌ No valid jobs - checking raw samples:")
        for job in api['jobs'][:3]:
            title = job.get('title') or job.get('jobTitle') or '[no title]'
            keys = list(job.keys())[:5]
            print(f"      • Title: '{title}' | Keys: {keys}")


# ============================
# INTERACTION ENGINE (SCROLL + LOAD MORE)
# ============================
def simulate_user(page):
    print("Simulating user interaction...")
    
    # Scroll to trigger lazy-loaded APIs
    for _ in range(5):
        page.mouse.wheel(0, 3000)
        page.wait_for_timeout(1500)
    
    # Click "Load More" buttons if present
    try:
        for _ in range(3):  # Max 3 clicks to avoid infinite loops
            selectors = [
                "text=Load More", "text=Load more", "text=Show More", 
                "button:has-text('Load')", "[aria-label*='load']"
            ]
            btn = None
            for sel in selectors:
                btn = page.query_selector(sel)
                if btn:
                    break
            if not btn or not btn.is_visible():
                break
            btn.click()
            page.wait_for_timeout(2000)
    except Exception as e:
        print(f"   Load More interaction skipped: {e}")


# ============================
# CAPTURE NETWORK APIs (FIXED)
# ============================
def capture_apis(url):
    results = []
    seen_urls = set()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

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
                
                if not is_valid_api(api_url):
                    return
                
                text = response.text()
                if len(text) < 50:  # Skip tiny responses
                    return
                
                data = parse_json_safe(text)
                if not data:  # ✅ FIXED: was incomplete
                    return
                
                # Extract jobs BEFORE filtering for debugging visibility
                all_extracted = extract_jobs(data)
                
                # Get request payload for POST APIs
                payload = None
                try:
                    if req.method == "POST":
                        payload = req.post_data_json
                except:
                    pass
                
                results.append({
                    "url": api_url,
                    "jobs": all_extracted,  # Keep all for inspection
                    "method": req.method,
                    "payload": payload
                })
                
                valid_count = len([j for j in all_extracted if is_valid_job(j)])
                print(f"[+] API: {api_url[:60]}... | Raw: {len(all_extracted)} | Valid: {valid_count} | {req.method}")

            except Exception as e:
                pass  # Silent fail to keep scraping robust

        page.on("response", handle_response)

        print(f"\n🌐 Loading: {url}")
        page.goto(url, timeout=60000)
        
        # ✅ FIXED: Use safer wait strategy with fallback
        try:
            page.wait_for_load_state("networkidle", timeout=20000)
        except:
            page.wait_for_load_state("domcontentloaded", timeout=10000)
            page.wait_for_timeout(3000)

        simulate_user(page)
        
        # Final wait for any straggling APIs
        page.wait_for_timeout(2000)

        browser.close()

    return results


# ============================
# PAGINATION (GET) - FIXED
# ============================
def paginate_get(url, max_pages=50):
    all_jobs = []
    seen_ids = set()

    parsed = urlparse.urlparse(url)
    params = urlparse.parse_qs(parsed.query)

    # Find pagination parameter
    param = None
    for key in params:
        if key.lower() in ["start", "offset", "page", "skip", "from"]:
            param = key
            break
    
    if not param:
        print(f"   ⚠ No pagination param found in URL")
        return []

    print(f"   📄 Paginating GET with param: '{param}'")
    
    for i in range(max_pages):
        params[param] = [str(i * 10)]  # Assume page size 10
        
        new_url = parsed._replace(
            query=urlparse.urlencode(params, doseq=True)
        ).geturl()

        try:
            res = requests.get(new_url, headers=HEADERS, timeout=30)
            data = parse_json_safe(res.text)
            if not data:  # ✅ FIXED: was incomplete
                break
                
            jobs = extract_jobs(data)
            valid_jobs = [j for j in jobs if is_valid_job(j)]
            
            # Deduplicate by ID
            new_jobs = []
            for job in valid_jobs:
                job_id = job.get("id")
                if job_id and job_id in seen_ids:
                    continue
                if job_id:
                    seen_ids.add(job_id)
                new_jobs.append(job)
            
            if not new_jobs:
                print(f"   [Page {i}] No new valid jobs - stopping")
                break
                
            all_jobs.extend(new_jobs)
            print(f"   [Page {i}] +{len(new_jobs)} valid jobs (total: {len(all_jobs)})")
            
        except Exception as e:
            print(f"   [Page {i}] Error: {e}")
            break

    return all_jobs


# ============================
# PAGINATION (POST) - FIXED
# ============================
def paginate_post(api, max_pages=50):
    url = api["url"]
    base_payload = api.get("payload") or {}
    all_jobs = []
    seen_ids = set()

    # Find pagination parameter in payload
    param = None
    for key in base_payload:
        if key.lower() in ["start", "offset", "page", "skip", "from"]:
            param = key
            break
    
    if not param:
        print(f"   ⚠ No pagination param found in payload")
        return []

    # Estimate page size
    page_size = base_payload.get("limit") or base_payload.get("rows") or 30

    print(f"   📄 Paginating POST with param: '{param}', page_size: {page_size}")
    
    for i in range(max_pages):
        payload = base_payload.copy()
        payload[param] = i * page_size

        try:
            res = requests.post(url, json=payload, headers=HEADERS, timeout=30)
            data = parse_json_safe(res.text)
            if not data:  # ✅ FIXED: was incomplete
                break
                
            jobs = extract_jobs(data)
            valid_jobs = [j for j in jobs if is_valid_job(j)]
            
            # Deduplicate by ID
            new_jobs = []
            for job in valid_jobs:
                job_id = job.get("id")
                if job_id and job_id in seen_ids:
                    continue
                if job_id:
                    seen_ids.add(job_id)
                new_jobs.append(job)
            
            if not new_jobs:
                print(f"   [Page {i}] No new valid jobs - stopping")
                break
                
            all_jobs.extend(new_jobs)
            print(f"   [Page {i}] +{len(new_jobs)} valid jobs (total: {len(all_jobs)})")
            
        except Exception as e:
            print(f"   [Page {i}] Error: {e}")
            break

    return all_jobs


# ============================
# MAIN
# ============================
if __name__ == "__main__":
    print("🚀 Starting Dynamic Job API Scraper\n")
    
    # Step 1: Capture all candidate APIs
    apis = capture_apis(TARGET_URL)

    if not apis:
        print("❌ No APIs found - try increasing wait times or checking URL")
        exit(1)

    # Debug: Show what we found
    print(f"\n📊 Found {len(apis)} candidate APIs - inspecting samples:")
    for api in apis[:5]:  # Show top 5 for debugging
        inspect_api_sample(api)

    # Step 2: Rank by quality-weighted score
    ranked = sorted(apis, key=score_api, reverse=True)
    best_api = ranked[0]
    
    print(f"\n🏆 BEST API (score: {score_api(best_api)}):")
    print(f"   URL: {best_api['url']}")
    print(f"   Method: {best_api.get('method', 'GET')}")
    valid_in_best = len([j for j in best_api.get('jobs', []) if is_valid_job(j)])
    print(f"   Valid jobs in sample: {valid_in_best}")

    # Step 3: Paginate the winning API
    print(f"\n⬇️  Fetching all jobs via pagination...")
    if best_api.get("method") == "POST" and best_api.get("payload"):
        all_jobs = paginate_post(best_api)
    else:
        all_jobs = paginate_get(best_api["url"])

    # Step 4: Output results
    print(f"\n✅ TOTAL VALID JOBS: {len(all_jobs)}")
    
    if all_jobs:
        print(f"\n📋 First 20 jobs:")
        for i, job in enumerate(all_jobs[:20], 1):
            loc = job.get('location') or 'N/A'
            title = job.get('title') or 'Untitled'
            job_id = job.get('id') or 'N/A'
            print(f"{i:2}. [{job_id}] {title[:55]} @ {loc}")
    
    # Optional: Save to file
    # with open("jobs.json", "w") as f:
    #     json.dump(all_jobs, f, indent=2, default=str)
    # print(f"\n💾 Saved to jobs.json")