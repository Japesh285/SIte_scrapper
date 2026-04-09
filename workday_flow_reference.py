# ============================
# CONFIG
# ============================
TARGET_URL = "https://careers.nvidia.com/jobs"
MAX_TOTAL_JOBS = 100
MAX_PAGES = 20
REQUEST_DELAY = 1.0

# ============================
# IMPORTS
# ============================
from playwright.sync_api import sync_playwright
import requests
import json
import re
import time
from copy import deepcopy
from urllib.parse import urlparse, parse_qs, urlencode

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "*/*",
    "Content-Type": "application/json",
}
BAD_DOMAINS = ["analytics", "tracking", "cookie", "consent", "demdex", "doubleclick", "nr-data", "i18n", "translations"]

JOB_SIGNALS = {
    "title": ["title", "jobTitle", "positionTitle", "job_title", "PositionTitle", "name"],
    "location": ["location", "locations", "locationName", "primary_location", "workLocation", "city", "country", "address"],
    "id": ["id", "jobId", "requisitionId", "postingId", "job_id", "reqId", "reference", "code"],
    "url": ["applyUrl", "applyURL", "jobUrl", "jobURL", "url", "link", "apply_link"],
    "date": ["postedDate", "datePosted", "publishDate", "publicationDate", "posted_date", "created_at"]
}

# ============================
# UNIVERSAL PARSING
# ============================
def parse_any_json(text):
    if not text or not isinstance(text, str):
        return None
    text = text.strip()
    try:
        return json.loads(text)
    except:
        pass
    # JSONP
    try:
        match = re.search(r'^[a-zA-Z0-9_\.$\[\]]+\s*\(\s*({.*?})\s*\)\s*$', text, re.DOTALL)
        if match:
            return json.loads(match.group(1))
    except:
        pass
    return None

# ============================
# AGGRESSIVE JOB EXTRACTOR
# ============================
def extract_jobs_aggressive(data):
    """
    Recursively traverses ANY JSON structure to find job-like objects.
    Handles GraphQL 'edges/nodes' and standard lists.
    """
    jobs = []
    seen_ids = set()

    def get_val(obj, keys):
        for k in keys:
            if k in obj and obj[k]:
                val = obj[k]
                if isinstance(val, dict) and "name" in val:
                    return val["name"]
                if isinstance(val, list) and val:
                    return val[0] if isinstance(val[0], str) else val
                return val
        return None

    def is_job_like(obj):
        if not isinstance(obj, dict):
            return False
        score = 0
        # Check for title
        if any(k in obj for k in JOB_SIGNALS["title"]):
            score += 2
        # Check for location or ID
        if any(k in obj for k in JOB_SIGNALS["location"]):
            score += 1
        if any(k in obj for k in JOB_SIGNALS["id"]):
            score += 1
        # Check for URL
        if any(k in obj for k in JOB_SIGNALS["url"]):
            score += 1
        
        # Must have Title + at least one other signal
        return score >= 3

    def normalize(obj):
        return {
            "title": get_val(obj, JOB_SIGNALS["title"]),
            "location": get_val(obj, JOB_SIGNALS["location"]),
            "id": get_val(obj, JOB_SIGNALS["id"]),
            "url": get_val(obj, JOB_SIGNALS["url"]),
            "posted_date": get_val(obj, JOB_SIGNALS["date"]),
        }

    def recurse(obj, depth=0):
        if depth > 8:  # Prevent infinite recursion on circular refs
            return
        if isinstance(obj, dict):
            # 1. Check if THIS object is a job
            if is_job_like(obj):
                job = normalize(obj)
                job_id = str(job.get("id") or "")
                if job_id and job_id not in seen_ids:
                    seen_ids.add(job_id)
                    jobs.append(job)
                # Don't recurse further into a confirmed job to avoid duplicates
            
            # 2. Check for common containers
            for key in ["jobs", "results", "items", "positions", "data", "records", "edges", "nodes", "listings"]:
                if key in obj and isinstance(obj[key], list):
                    for item in obj[key]:
                        # Handle GraphQL Edge: { node: { ... } }
                        actual_item = item.get("node") if isinstance(item, dict) and "node" in item else item
                        recurse(actual_item, depth + 1)
                    return  # Stop recursing into this branch, we handled the list

            # 3. Recurse into values
            for v in obj.values():
                recurse(v, depth + 1)

        elif isinstance(obj, list):
            for item in obj:
                recurse(item, depth + 1)

    recurse(data)
    return jobs

def is_valid_job(job):
    """Final validation filter"""
    if not job or not job.get("title"):
        return False
    title = str(job["title"]).lower()
    if len(title) < 5:
        return False
    # Reject junk
    if any(x in title for x in ["overview", "menu", "home", "login", "privacy"]):
        return False
    return True

# ============================
# GRAPHQL CURSOR FINDER
# ============================
def find_graphql_cursor(data):
    """Recursively find pageInfo.endCursor"""
    if isinstance(data, dict):
        if "pageInfo" in data and isinstance(data["pageInfo"], dict):
            cursor = data["pageInfo"].get("endCursor")
            if cursor:
                return cursor
        for v in data.values():
            res = find_graphql_cursor(v)
            if res:
                return res
    elif isinstance(data, list):
        for item in 
            res = find_graphql_cursor(item)
            if res:
                return res
    return None

# ============================
# API SCORER
# ============================
def score_api(api):
    url = api["url"].lower()
    jobs = api.get("jobs", [])
    valid_jobs = [j for j in jobs if is_valid_job(j)]
    score = 0
    
    if "graphql" in url:
        score += 10  # Prefer GraphQL if it has jobs
    if any(k in url for k in ["job", "career", "search"]):
        score += 5
    
    valid_count = len(valid_jobs)
    if valid_count > 0:
        score += valid_count * 5
    else:
        score -= 100  # Penalize empty APIs heavily
    
    return score

# ============================
# CAPTURE ENGINE
# ============================
def capture_apis(url):
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
                
                # Accept JSON or GraphQL responses
                if "application/json" not in content_type.lower() and "text/plain" not in content_type.lower():
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
                if not 
                    return
                
                # Detect GraphQL by URL or Payload
                is_gql = "graphql" in api_url.lower()
                payload = None
                if req.method == "POST":
                    try:
                        payload = req.post_data_json
                        if not payload and "query" in req.post_
                             # Sometimes payload is stringified JSON
                             try: payload = json.loads(req.post_data)
                             except: pass
                        if isinstance(payload, dict) and "query" in payload:
                            is_gql = True
                    except:
                        pass

                jobs = extract_jobs_aggressive(data)
                
                request_data = {
                    "url": api_url,
                    "method": req.method,
                    "headers": {k: v for k, v in req.headers.items() if k.lower() not in ["content-length", "host"]},
                    "payload": payload,
                    "cookies": context.cookies(),
                    "jobs": jobs,
                    "is_graphql": is_gql,
                    "raw_response": data
                }
                results.append(request_data)
                
                valid_count = len([j for j in jobs if is_valid_job(j)])
                type_label = "GQL" if is_gql else "REST"
                print(f"[+] {type_label} | {api_url[:60]}... | Jobs: {len(jobs)} | Valid: {valid_count}")
                
            except Exception as e:
                pass

        page.on("response", handle_response)
        
        print(f"\n🌐 Loading: {url}")
        page.goto(url, timeout=60000)
        try:
            page.wait_for_load_state("networkidle", timeout=20000)
        except:
            page.wait_for_load_state("domcontentloaded", timeout=10000)
            page.wait_for_timeout(3000)
        
        # Scroll to trigger lazy loads
        for _ in range(10):
            page.mouse.wheel(0, 5000)
            page.wait_for_timeout(1000)
            
        browser.close()
    return results

# ============================
# GRAPHQL PAGINATION
# ============================
def paginate_graphql(api):
    url = api["url"]
    base_payload = api.get("payload")
    cookies = api.get("cookies")
    headers = api.get("headers")
    
    if not base_payload:
        return []
        
    all_jobs = []
    seen_ids = set()
    
    session = requests.Session()
    session.headers.update({**HEADERS, **headers})
    if cookies:
        for c in cookies:
            session.cookies.set(c["name"], c["value"], domain=c.get("domain"), path=c.get("path", "/"))
    
    current_payload = deepcopy(base_payload)
    
    for page_num in range(MAX_PAGES):
        try:
            res = session.post(url, json=current_payload, timeout=30)
            data = parse_any_json(res.text)
            if not 
                break
            
            # Extract Jobs
            jobs = extract_jobs_aggressive(data)
            new_batch = []
            for job in jobs:
                if is_valid_job(job):
                    job_id = str(job.get("id") or "")
                    if job_id and job_id not in seen_ids:
                        seen_ids.add(job_id)
                        new_batch.append(job)
            
            if not new_batch:
                print(f"   [Page {page_num+1}] No new jobs found.")
                break
                
            all_jobs.extend(new_batch)
            print(f"   [Page {page_num+1}] +{len(new_batch)} jobs (Total: {len(all_jobs)})")
            
            if len(all_jobs) >= MAX_TOTAL_JOBS:
                break
                
            # Find Next Cursor
            next_cursor = find_graphql_cursor(data)
            if not next_cursor:
                print(f"   [Page {page_num+1}] No endCursor found. Stopping.")
                break
            
            # Update Payload for Next Page
            if "variables" in current_payload and isinstance(current_payload["variables"], dict):
                # Common variable names for cursors
                for key in ["after", "cursor", "nextCursor", "startCursor"]:
                    if key in current_payload["variables"]:
                        current_payload["variables"][key] = next_cursor
                        break
            else:
                # If no variables object, try root level (rare)
                for key in ["after", "cursor"]:
                    if key in current_payload:
                        current_payload[key] = next_cursor
                        break
                        
        except Exception as e:
            print(f"   [Page {page_num+1}] Error: {e}")
            break
            
        time.sleep(REQUEST_DELAY)
        
    return all_jobs

# ============================
# REST PAGINATION (Brute Force)
# ============================
def paginate_rest(api):
    url = api["url"]
    method = api["method"]
    base_payload = api.get("payload")
    cookies = api.get("cookies")
    headers = api.get("headers")
    
    all_jobs = []
    seen_ids = set()
    
    session = requests.Session()
    session.headers.update({**HEADERS, **headers})
    if cookies:
        for c in cookies:
            session.cookies.set(c["name"], c["value"], domain=c.get("domain"), path=c.get("path", "/"))
            
    # Initial Request
    try:
        if method == "POST":
            res = session.post(url, json=base_payload, timeout=30)
        else:
            res = session.get(url, timeout=30)
        data = parse_any_json(res.text)
        if not 
            return []
    except Exception as e:
        print(f"   ❌ Initial Request Failed: {e}")
        return []
        
    jobs = extract_jobs_aggressive(data)
    for job in jobs:
        if is_valid_job(job):
            job_id = str(job.get("id") or "")
            if job_id and job_id not in seen_ids:
                seen_ids.add(job_id)
                all_jobs.append(job)
                
    print(f"   📦 Initial Page: {len(all_jobs)} jobs")
    
    # Brute Force Param Discovery
    parsed = urlparse(url)
    params = parse_qs(parsed.query, keep_blank_values=True)
    candidates = []
    
    for key, vals in params.items():
        if vals and vals[0].isdigit():
            candidates.append({"type": "url", "key": key, "start": int(vals[0])})
            
    if isinstance(base_payload, dict):
        for key, val in base_payload.items():
            if isinstance(val, int):
                candidates.append({"type": "payload", "key": key, "start": val})
                
    if not candidates:
        return all_jobs
        
    print(f"   🔍 Testing params: {[c['key'] for c in candidates]}")
    
    for page_num in range(1, MAX_PAGES):
        if len(all_jobs) >= MAX_TOTAL_JOBS:
            break
            
        new_jobs_found = False
        for cand in candidates:
            key = cand["key"]
            start = cand["start"]
            # Simple increment logic
            next_val = start + (page_num * 10) # Assume page size 10
            
            try:
                if cand["type"] == "url":
                    params[key] = [str(next_val)]
                    new_url = parsed._replace(query=urlencode(params, doseq=True)).geturl()
                    res = session.get(new_url, timeout=30)
                else:
                    new_payload = deepcopy(base_payload)
                    new_payload[key] = next_val
                    res = session.post(url, json=new_payload, timeout=30)
                    
                data = parse_any_json(res.text)
                if not 
                    continue
                    
                jobs = extract_jobs_aggressive(data)
                batch = []
                for job in jobs:
                    if is_valid_job(job):
                        job_id = str(job.get("id") or "")
                        if job_id and job_id not in seen_ids:
                            seen_ids.add(job_id)
                            batch.append(job)
                
                if batch:
                    all_jobs.extend(batch)
                    new_jobs_found = True
                    print(f"   [Page {page_num}] +{len(batch)} via '{key}'={next_val}")
                    break # Success with this param
                    
            except:
                continue
                
        if not new_jobs_found:
            break
        time.sleep(REQUEST_DELAY)
        
    return all_jobs

# ============================
# MAIN
# ============================
if __name__ == "__main__":
    print(f"🚀 UNIVERSAL SCRAPER (GraphQL + REST)\n   Target: {TARGET_URL}\n")
    
    apis = capture_apis(TARGET_URL)
    if not apis:
        print("❌ No APIs Found")
        exit(1)
        
    # Filter out APIs with 0 jobs first
    viable_apis = [api for api in apis if len([j for j in api['jobs'] if is_valid_job(j)]) > 0]
    
    if not viable_apis:
        print("⚠️  No APIs returned valid jobs. Try scrolling more or checking selectors.")
        # Debug: Show top APIs anyway
        ranked = sorted(apis, key=score_api, reverse=True)
        best = ranked[0]
        print(f"   Best Attempt: {best['url']}")
        exit(1)
        
    ranked = sorted(viable_apis, key=score_api, reverse=True)
    best = ranked[0]
    
    print(f"\n🏆 Selected API:")
    print(f"   Type: {'GraphQL' if best.get('is_graphql') else 'REST'}")
    print(f"   URL: {best['url'][:80]}...")
    print(f"   Jobs Found: {len(best['jobs'])}")
    
    print(f"\n⬇️  Starting Pagination...")
    if best.get("is_graphql"):
        all_jobs = paginate_graphql(best)
    else:
        all_jobs = paginate_rest(best)
        
    print(f"\n✅ TOTAL JOBS: {len(all_jobs)}")
    if all_jobs:
        for i, job in enumerate(all_jobs[:10], 1):
            print(f"{i}. {job.get('title')} @ {job.get('location')}")
            
    with open("jobs_output.json", "w") as f:
        json.dump(all_jobs, f, indent=2, default=str)
    print("💾 Saved to jobs_output.json")