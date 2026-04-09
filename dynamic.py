# ============================
# CONFIG — CHANGE ONLY THIS
# ============================
TARGET_URL = "https://genpact.taleo.net/careersection/sgy_external_career_section/jobsearch.ftl"
MAX_TOTAL_JOBS = 1000  # Safety limit to prevent infinite loops
MAX_PAGES = 100       # Maximum number of pages to attempt
REQUEST_DELAY = 0.5   # Delay between pagination requests to avoid rate limiting

# ============================
# IMPORTS
# ============================
from playwright.sync_api import sync_playwright
import requests
import json
import re
import time
from copy import deepcopy
from urllib.parse import urlparse, parse_qs, urlencode, quote_plus

# ============================
# GLOBALS
# ============================
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
}
BAD_DOMAINS = ["analytics", "tracking", "cookie", "consent", "demdex", "doubleclick", "nr-data"]
PAGINATION_KEYWORDS = ["offset", "start", "page", "skip", "from", "limit", "size", "rows"]

JOB_SIGNALS = {
    "title": ["title", "jobTitle", "positionTitle", "job_title", "PositionTitle"],
    "location": ["location", "locations", "locationName", "primary_location", "workLocation"],
    "id": ["id", "jobId", "requisitionId", "postingId", "job_id", "reqId"],
    "url": ["applyUrl", "applyURL", "jobUrl", "jobURL", "url", "link"],
    "date": ["postedDate", "datePosted", "publishDate", "publicationDate", "posted_date"]
}

# ============================
# UNIVERSAL PARSING AND VALIDATION
# ============================
def parse_any_json(text):
    """Robustly parse JSON or JSONP formatted text."""
    if not text or not isinstance(text, str):
        return None
    text = text.strip()
    try:
        return json.loads(text)
    except:
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
    title = next((str(obj[k]).lower().strip() for k in JOB_SIGNALS["title"] if k in obj and obj[k]), None)
    if not title or len(title) < 5 or len(title) > 200:
        return False
    junk_patterns = [
        r'^(overview|home|menu|navigation|search|login|signup|sign\s*in|contact|about)$',
        r'^(privacy|terms|cookie|consent|preferences|policy|legal)$'
    ]
    if any(re.match(p, title, re.IGNORECASE) for p in junk_patterns):
        return False
    return any(obj.get(k) for k in JOB_SIGNALS["location"]) or any(obj.get(k) for k in JOB_SIGNALS["id"])

def extract_jobs_any(data):
    """Extract all valid jobs from any JSON structure."""
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
            for container in ["jobs", "results", "items", "positions", "data", "records"]:
                if container in obj and isinstance(obj[container], list):
                    for item in obj[container]:
                        if isinstance(item, dict) and is_valid_job(item):
                            job = normalize(item)
                            job_id = job.get("id")
                            if job_id and job_id not in seen_ids:
                                seen_ids.add(job_id)
                                jobs.append(job)
                    return
            if is_valid_job(obj):
                job = normalize(obj)
                job_id = job.get("id")
                if job_id and job_id not in seen_ids:
                    seen_ids.add(job_id)
                    jobs.append(job)
            for v in obj.values():
                recurse(v)
        else:
            for item in obj:
                recurse(item)
    recurse(data)
    return jobs

def score_api_universal(api):
    """Score an API based on job quality, not just quantity."""
    url = api["url"].lower()
    all_jobs = api.get("jobs", [])
    valid_jobs = [j for j in all_jobs if is_valid_job(j)]
    score = 0
    if any(k in url for k in ["job", "career", "position"]): score += 5
    if any(k in url for k in ["search", "api"]): score += 3
    valid_count = len(valid_jobs)
    if valid_count >= 20: score += 20
    elif valid_count >= 10: score += 15
    elif valid_count > 0: score += valid_count * 2
    else: score -= 50
    if api.get("method") == "POST" and api.get("payload"): score += 12
    if valid_jobs: score += sum(1 for j in valid_jobs[:5] if j.get("location")) + sum(1 for j in valid_jobs[:5] if j.get("id"))
    return score

# ============================
# CAPTURE AND REPLAY ENGINE
# ============================
def capture_apis_universal(url):
    """Capture APIs with their exact request data for replay."""
    results = []
    seen = set()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        
        def handle_response(response):
            try:
                req = response.request
                content_type = response.headers.get("content-type", "")
                if "application/json" not in content_type.lower():
                    return
                api_url = response.url
                if api_url in seen:
                    return
                seen.add(api_url)
                if any(b in api_url.lower() for b in BAD_DOMAINS):
                    return
                text = response.text()
                if len(text) < 50:
                    return
                data = parse_any_json(text)
                if not data:
                    return
                request_data = {
                    "url": api_url,
                    "method": req.method,
                    "headers": {k: v for k, v in req.headers.items() if k.lower() not in ["content-length", "host"]},
                    "payload": req.post_data_json,
                    "cookies": context.cookies(),
                    "jobs": extract_jobs_any(data)
                }
                results.append(request_data)
                valid_count = len([j for j in request_data["jobs"] if is_valid_job(j)])
                print(f"[+] {api_url[:60]}... | Jobs: {len(request_data['jobs'])} | Valid: {valid_count} | {req.method}")
            except:
                pass
        
        page.on("response", handle_response)
        print(f"\n🌐 Loading: {url}")
        page.goto(url, timeout=60000)
        try:
            page.wait_for_load_state("networkidle", timeout=20000)
        except:
            page.wait_for_load_state("domcontentloaded", timeout=10000)
            page.wait_for_timeout(3000)
        for _ in range(5):
            page.mouse.wheel(0, 3000)
            page.wait_for_timeout(1000)
        try:
            for _ in range(3):
                selectors = ["text=Load More", "button:has-text('Load')"]
                btn = None
                for sel in selectors:
                    btn = page.query_selector(sel)
                    if btn and btn.is_visible():
                        break
                if not btn:
                    break
                btn.click()
                page.wait_for_timeout(2000)
        except:
            pass
        page.wait_for_timeout(2000)
        browser.close()
    return results

def build_url_preserve_params(base_url, params_dict):
    """Rebuild URL preserving JSONP callbacks and bracket notation."""
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
    cookies = api.get("cookies")
    
    all_jobs = []
    seen_job_ids = set()
    session = requests.Session()
    session.headers.update({**HEADERS, **base_headers})
    if cookies:
        for c in cookies:
            session.cookies.set(c["name"], c["value"], domain=c.get("domain"), path=c.get("path", "/"))
    
    try:
        if method == "POST":
            res = session.post(url, json=base_payload, timeout=30)
        else:
            res = session.get(url, timeout=30)
    except Exception as e:
        print(f"   ❌ Initial request failed: {e}")
        return []
    
    first_data = parse_any_json(res.text)
    if not first_data:
        return []
        
    first_jobs = extract_jobs_any(first_data)
    for job in first_jobs:
        if is_valid_job(job):
            job_id = job.get("id")
            if job_id and job_id not in seen_job_ids:
                seen_job_ids.add(job_id)
                all_jobs.append(job)
    print(f"   📦 Initial: {len(all_jobs)} valid jobs")

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
        print("   ⚠️ No numeric pagination params found.")
        return all_jobs

    print(f"   🔍 Testing {len(candidates)} candidates: {[c['key'] for c in candidates]}")

    for page_num in range(1, max_pages + 1):
        if len(all_jobs) >= MAX_TOTAL_JOBS:
            print(f"   ⏹️ Reached MAX_TOTAL_JOBS ({MAX_TOTAL_JOBS})")
            break
            
        new_jobs_found = False
        page_size_estimate = len(all_jobs) or 10

        for candidate in candidates:
            param_type = candidate["type"]
            key = candidate["key"]
            start_val = candidate["start"]
            
            if key.lower() in ["page", "pagenum"]:
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
            print(f"   [Page {page_num}] No new jobs from any param — pagination complete")
            break
        time.sleep(REQUEST_DELAY)
        
    return all_jobs

# ============================
# MAIN EXECUTION
# ============================
if __name__ == "__main__":
    print(f"🚀 UNIVERSAL DYNAMIC API SCRAPER\n   Target: {TARGET_URL}\n")
    apis = capture_apis_universal(TARGET_URL)
    if not apis:
        print("❌ No APIs captured. Check the URL or increase wait times.")
        exit(1)
    ranked = sorted(apis, key=score_api_universal, reverse=True)
    best = ranked[0]
    print(f"\n🏆 Selected API (score: {score_api_universal(best)}):")
    print(f"   {best['url'][:80]}...")
    valid_sample = len([j for j in best['jobs'] if is_valid_job(j)])
    print(f"   Method: {best['method']}, Valid in sample: {valid_sample}")
    print(f"\n⬇️  Starting universal pagination...")
    all_jobs = paginate_universal(best)
    print(f"\n✅ FINAL: {len(all_jobs)} valid jobs")
    if all_jobs:
        print(f"\n📋 Sample:")
        for i, job in enumerate(all_jobs[:15], 1):
            loc = job.get("location") or "N/A"
            title = (job.get("title") or "Untitled")[:50]
            jid = job.get("id") or "N/A"
            print(f"{i:2}. [{jid}] {title} @ {loc}")
    with open("jobs_universal.json", "w", encoding="utf-8") as f:
        json.dump(all_jobs, f, indent=2, default=str, ensure_ascii=False)
    print(f"\n💾 Saved to jobs_universal.json")