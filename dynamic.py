#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Universal Career Scraper + GPT-4.1 Nano AI Enrichment
✅ Extracts jobs from any career portal
✅ Sends each job to OpenAI GPT-4.1 nano for structured normalization
✅ Outputs exact schema format with token tracking
✅ Processes jobs sequentially (one-by-one) with error handling
"""

# ============================
# CONFIG — CHANGE ONLY THIS
# ============================
TARGET_URL = "https://careers.dxc.com/job-search-results/?compliment[]=India&primary_city[]=Gurgaon"
MAX_TOTAL_JOBS = 50            # Safety limit (AI processing is slower)
MAX_PAGES = 20                 # Max pagination attempts
REQUEST_DELAY = 1.0            # Seconds between API requests
OPENAI_DELAY = 2.0             # Seconds between OpenAI API calls (rate limit safety)
OUTPUT_FILE = "jobs_ai_enriched.json"
FETCH_DETAIL_PAGES = False     # Set True if API lacks description

# === OPENAI CONFIG ===
OPENAI_API_KEY = ""
OPENAI_MODEL = "gpt-4.1-nano"  # or "gpt-4o-mini", "gpt-3.5-turbo"
OPENAI_MAX_TOKENS = 2048
OPENAI_TEMPERATURE = 0.1       # Low temp for consistent structured output

# ============================
# IMPORTS
# ============================
import json
import re
import time
import html
import os
import requests
from copy import deepcopy
from urllib.parse import urlparse, parse_qs, quote_plus
from bs4 import BeautifulSoup
from dotenv import load_dotenv

try:
    from openai import OpenAI
except ImportError:
    print("❌ openai package not installed. Run: pip install openai")
    exit(1)

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("❌ playwright not installed. Run: pip install playwright && playwright install chromium")
    exit(1)

load_dotenv()

# ============================
# GLOBALS & HEADERS
# ============================
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/html, */*",
    "Accept-Language": "en-US,en;q=0.9",
}

BAD_DOMAINS = ["analytics", "tracking", "cookie", "consent", "demdex", "doubleclick", "nr-data"]

JOB_SIGNALS = {
    "title": ["title", "jobTitle", "positionTitle", "job_title", "PositionTitle", "name", "job_name"],
    "location": ["location", "locations", "locationName", "primary_location", "workLocation", "city", "country", "office"],
    "id": ["id", "jobId", "requisitionId", "postingId", "job_id", "reqId", "requisition_id", "ref", "reference"],
    "url": ["applyUrl", "applyURL", "jobUrl", "jobURL", "url", "link", "canonicalUrl", "apply_job_url", "apply_url", "job_url", "detailUrl", "careerSiteUrl"],
    "date": ["postedDate", "datePosted", "publishDate", "publicationDate", "posted_date", "createdDate", "indexed_at", "updated_at"],
    "description": ["description", "jobDescription", "job_description", "details", "summary", "content", "body", "description_stripped"],
    "requirements": ["requirements", "qualifications", "skills", "requiredSkills", "jobRequirements", "essentialCriteria"],
    "responsibilities": ["responsibilities", "duties", "roleResponsibilities", "keyResponsibilities"],
    "employment_type": ["employmentType", "employment_type", "jobType", "type", "contract_type", "contractType"],
    "category": ["category", "department", "businessUnit", "professional_communities", "professionalGroup", "jobCategory", "sbu"],
    "experience": ["experience", "experienceLevel", "yearsOfExperience", "seniority", "experience_level"],
}

# ============================
# OPENAI CLIENT SETUP
# ============================
def get_openai_client():
    """Initialize OpenAI client with API key from config or env."""
    api_key = os.getenv("OPENAI_API_KEY") or OPENAI_API_KEY
    if not api_key:
        raise ValueError("❌ OpenAI API key not set. Add OPENAI_API_KEY to .env or environment.")
    return OpenAI(api_key=api_key)


# ============================
# HTML CLEANER
# ============================
def clean_html_description(raw_text: str) -> str:
    """Decode Unicode-escaped HTML and strip tags to get clean text."""
    if not raw_text or not isinstance(raw_text, str):
        return ""
    try:
        decoded = raw_text.encode('utf-8').decode('unicode_escape')
    except:
        decoded = raw_text
    decoded = html.unescape(decoded)
    soup = BeautifulSoup(decoded, 'html.parser')
    for tag in soup.find_all(['br', 'p', 'div', 'li', 'h1', 'h2', 'h3', 'h4', 'ul', 'ol']):
        tag.insert_before('\n')
    text = soup.get_text(separator=' ', strip=True)
    text = re.sub(r'\s+', ' ', text)
    text = re.sub(r'\n\s*\n', '\n\n', text)
    return text.strip()


# ============================
# UNIVERSAL PARSING
# ============================
def parse_any_json(text):
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
    jobs = []
    seen_ids = set()
    
    def normalize(obj):
        return {
            "title": next((obj[k] for k in JOB_SIGNALS["title"] if k in obj and obj[k]), None),
            "location": next((obj[k] for k in JOB_SIGNALS["location"] if k in obj and obj[k]), None),
            "id": next((str(obj[k]) for k in JOB_SIGNALS["id"] if k in obj and obj[k]), None),
            "url": next((obj[k] for k in JOB_SIGNALS["url"] if k in obj and obj[k]), None),
            "posted_date": next((obj[k] for k in JOB_SIGNALS["date"] if k in obj and obj[k]), None),
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
        score += sum(2 for j in valid_jobs[:5] if j.get("description_raw"))
    return score


# ============================
# API CAPTURE ENGINE
# ============================
def capture_apis_universal(url):
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
# 🤖 AI ENRICHMENT WITH GPT-4.1 NANO
# ============================
def prepare_job_for_ai(job: dict) -> dict:
    """Prepare job data for AI processing - clean and structure input."""
    # Decode description if raw HTML present
    description = job.get("description")
    if not description and job.get("description_raw"):
        description = clean_html_description(job["description_raw"])
    
    # Decode requirements if present
    requirements = job.get("requirements")
    if not requirements and job.get("requirements_raw"):
        requirements = clean_html_description(job["requirements_raw"])
    
    # Build clean input for AI
    return {
        "title": job.get("title") or "",
        "company": "Capgemini",  # Can be extracted from domain if needed
        "location": job.get("location") or job.get("location_text") or "",
        "job_id": job.get("id") or "",
        "url": job.get("url") or "",
        "description": description or "",
        "requirements": requirements or "",
        "responsibilities": job.get("responsibilities") or "",
        "employment_type": job.get("employment_type") or "",
        "category": job.get("category") or "",
        "experience": job.get("experience") or "",
        "posted_date": job.get("posted_date") or "",
        "contract_type": job.get("contract_type") or "",
    }


def format_ai_prompt(job_data: dict) -> str:
    """Format the prompt for GPT-4.1 nano with clear instructions."""
    return f"""You are a job data normalization expert. Extract and structure the following job posting into the exact schema format specified.

## INPUT JOB DATA:
{json.dumps(job_data, indent=2, ensure_ascii=False)}

## OUTPUT SCHEMA REQUIREMENTS:
Return ONLY valid JSON matching this exact structure. Do not include explanations, markdown, or extra text.

{{
  "id": "string (use job_id from input)",
  "title": "string (clean job title)",
  "company_name": "string (extract from input or use 'Capgemini')",
  "job_link": "string (the apply URL)",
  "experience": "string (extract experience requirement, e.g., '4-12 years')",
  "locations": ["string array of location strings, e.g., ['IN, PUNE']"],
  "educational_qualifications": "string (JSON array as string, e.g., '[\"Bachelor's degree\"]' or '[]')",
  "required_skill_set": ["string array of skills/technologies"],
  "remote_type": "string ('On-site', 'Remote', 'Hybrid', or '')",
  "posted_on": "string (format: 'Posted X Days Ago' or date)",
  "job_id": "string (same as id)",
  "salary": "string (extract if present, else '')",
  "is_active": true,
  "first_seen": "string (ISO date or '')",
  "last_seen": "string (ISO date or '')",
  "additional_sections": [
    {{"section_title": "string", "content": "string"}}
  ],
  "Scrap_json": {{
    "url": "string (original job URL)",
    "strategy": "string ('api' or 'html')",
    "site_type": "string (e.g., 'CUSTOM_API', 'WORKDAY_API', 'GREENHOUSE')",
    "department": "string (extract category/department)",
    "qualifications": ["string array"]
  }},
  "ai_usage": {{
    "input_tokens": 0,
    "output_tokens": 0,
    "total_tokens": 0
  }}
}}

## EXTRACTION RULES:
1. locations: Convert to format "COUNTRY, CITY" (e.g., "IN, PUNE", "US, NEW YORK")
2. experience: Extract numeric range if present (e.g., "4 to 12 years" → "4-12 years")
3. required_skill_set: Extract technical skills, tools, frameworks from description/requirements
4. remote_type: Infer from keywords: "remote", "work from home" → "Remote"; "hybrid", "flexible" → "Hybrid"; "on-site", "office" → "On-site"
5. educational_qualifications: Return as JSON string: '["Bachelor's degree in CS"]' or '[]'
6. additional_sections: Include useful metadata like "Job Description Summary", "Key Responsibilities", "Benefits"
7. Scrap_json.site_type: Detect from URL: "capgemini.com" → "CUSTOM_API", "myworkdayjobs.com" → "WORKDAY_API", "greenhouse.io" → "GREENHOUSE", "lever.co" → "LEVER"
8. ai_usage: You MUST fill these with actual token counts from the API response

## IMPORTANT:
- Return ONLY the JSON object, no markdown, no code blocks, no explanations
- All string fields should be trimmed and clean
- Empty fields should be "" or [] as appropriate
- is_active should always be true for valid jobs
"""


def call_openai_with_retry(prompt: str, max_retries: int = 3) -> tuple[dict, dict]:
    """
    Call OpenAI API with retry logic and token tracking.
    Returns: (parsed_json_response, usage_dict)
    """
    client = get_openai_client()
    
    for attempt in range(max_retries):
        try:
            response = client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=[
                    {"role": "system", "content": "You are a precise JSON output engine. Return ONLY valid JSON, no explanations."},
                    {"role": "user", "content": prompt}
                ],
                temperature=OPENAI_TEMPERATURE,
                max_tokens=OPENAI_MAX_TOKENS,
                response_format={"type": "json_object"}  # Force JSON output
            )
            
            # Extract usage stats
            usage = response.usage
            token_info = {
                "input_tokens": usage.prompt_tokens if usage else 0,
                "output_tokens": usage.completion_tokens if usage else 0,
                "total_tokens": usage.total_tokens if usage else 0
            }
            
            # Parse response
            content = response.choices[0].message.content.strip()
            # Remove markdown code blocks if present
            if content.startswith("```json"):
                content = re.sub(r'^```json\n?', '', content)
                content = re.sub(r'\n?```$', '', content)
            elif content.startswith("```"):
                content = re.sub(r'^```\n?', '', content)
                content = re.sub(r'\n?```$', '', content)
            
            parsed = json.loads(content)
            return parsed, token_info
            
        except json.JSONDecodeError as e:
            print(f"⚠️ JSON parse error (attempt {attempt+1}/{max_retries}): {e}")
            if attempt == max_retries - 1:
                raise
            time.sleep(OPENAI_DELAY * (attempt + 1))
        except Exception as e:
            print(f"⚠️ OpenAI API error (attempt {attempt+1}/{max_retries}): {e}")
            if attempt == max_retries - 1:
                raise
            time.sleep(OPENAI_DELAY * (attempt + 1))
    
    # Fallback return if all retries fail
    return None, {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}


def enrich_job_with_ai(job: dict) -> dict:
    """
    Send job to GPT-4.1 nano and return structured output in target schema.
    Processes one job at a time with error handling.
    """
    # Prepare input
    job_input = prepare_job_for_ai(job)
    prompt = format_ai_prompt(job_input)
    
    try:
        # Call OpenAI
        ai_output, token_usage = call_openai_with_retry(prompt)
        
        if not ai_output:
            print(f"   ❌ AI processing failed for job: {job.get('title', 'Untitled')}")
            # Return fallback structure
            return create_fallback_output(job)
        
        # Merge token usage into output
        ai_output["ai_usage"] = token_usage
        
        # Ensure required fields exist
        ai_output.setdefault("id", job.get("id") or "")
        ai_output.setdefault("job_id", job.get("id") or "")
        ai_output.setdefault("job_link", job.get("url") or "")
        ai_output.setdefault("is_active", True)
        ai_output.setdefault("additional_sections", [])
        ai_output.setdefault("Scrap_json", {
            "url": job.get("url", ""),
            "strategy": "api",
            "site_type": detect_site_type(job.get("url", "")),
            "department": job.get("category") or "",
            "qualifications": []
        })
        
        return ai_output
        
    except Exception as e:
        print(f"❌ Error enriching job: {e}")
        return create_fallback_output(job)


def create_fallback_output(job: dict) -> dict:
    """Create minimal valid output when AI processing fails."""
    return {
        "id": job.get("id") or "",
        "title": job.get("title") or "Untitled",
        "company_name": "Capgemini",
        "job_link": job.get("url") or "",
        "experience": job.get("experience") or "",
        "locations": [job.get("location")] if job.get("location") else [],
        "educational_qualifications": "[]",
        "required_skill_set": [],
        "remote_type": "",
        "posted_on": job.get("posted_date") or "",
        "job_id": job.get("id") or "",
        "salary": "",
        "is_active": True,
        "first_seen": "",
        "last_seen": "",
        "additional_sections": [],
        "Scrap_json": {
            "url": job.get("url", ""),
            "strategy": "api",
            "site_type": detect_site_type(job.get("url", "")),
            "department": job.get("category") or "",
            "qualifications": []
        },
        "ai_usage": {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0
        }
    }


def detect_site_type(url: str) -> str:
    """Detect career platform type from URL."""
    if not url:
        return "UNKNOWN"
    url_lower = url.lower()
    if "myworkdayjobs.com" in url_lower:
        return "WORKDAY_API"
    elif "greenhouse.io" in url_lower or "boards.api.greenhouse.io" in url_lower:
        return "GREENHOUSE"
    elif "lever.co" in url_lower or "api.lever.co" in url_lower:
        return "LEVER"
    elif "icims.com" in url_lower or "job-iframe" in url_lower:
        return "ICIMS"
    elif "smartrecruiters.com" in url_lower:
        return "SMARTRECRUITERS"
    elif "capgemini.com" in url_lower or "cg-jobstream" in url_lower:
        return "CUSTOM_API"
    elif "dxc.com" in url_lower:
        return "CUSTOM_API"
    else:
        return "CUSTOM_API"


# ============================
# MAIN EXECUTION
# ============================
def main():
    print(f"🚀 Universal Career Scraper + GPT-4.1 Nano AI")
    print(f"   Target: {TARGET_URL}")
    print(f"   Max jobs: {MAX_TOTAL_JOBS}, Max pages: {MAX_PAGES}")
    print(f"   Output: {OUTPUT_FILE}")
    print(f"   Model: {OPENAI_MODEL}")
    print(f"   AI Delay: {OPENAI_DELAY}s between calls\n")
    
    # Verify OpenAI key
    try:
        _ = get_openai_client()
        print("✅ OpenAI client initialized")
    except ValueError as e:
        print(f"❌ {e}")
        print("💡 Set your key: export OPENAI_API_KEY='sk-...' or edit script config")
        return
    
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
    
    # Step 4: 🤖 AI Enrichment (ONE-BY-ONE PROCESSING)
    print(f"\n🤖 Step 4: AI enrichment with {OPENAI_MODEL} (processing one-by-one)...")
    enriched_jobs = []
    total_tokens = 0
    
    for i, job in enumerate(all_jobs, 1):
        print(f"\n[{i}/{len(all_jobs)}] Processing: {job.get('title', 'Untitled')[:60]}...")
        
        # Enrich with AI
        enriched = enrich_job_with_ai(job)
        enriched_jobs.append(enriched)
        
        # Track tokens
        usage = enriched.get("ai_usage", {})
        job_tokens = usage.get("total_tokens", 0)
        total_tokens += job_tokens
        
        print(f"   ✅ Enriched | Tokens: {job_tokens} | Total: {total_tokens}")
        
        # Save incrementally (in case of interruption)
        if i % 5 == 0 or i == len(all_jobs):
            with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
                json.dump(enriched_jobs, f, indent=2, default=str, ensure_ascii=False)
            print(f"   💾 Checkpoint saved: {len(enriched_jobs)} jobs")
        
        # Rate limit for OpenAI API
        if i < len(all_jobs):
            time.sleep(OPENAI_DELAY)
    
    # Final save
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(enriched_jobs, f, indent=2, default=str, ensure_ascii=False)
    
    # Summary
    print(f"\n🎉 AI Enrichment Complete!")
    print(f"   ✅ Processed: {len(enriched_jobs)}/{len(all_jobs)} jobs")
    print(f"   🪙 Total tokens used: {total_tokens:,}")
    print(f"   💾 Saved to: {OUTPUT_FILE}")
    
    # Sample output
    if enriched_jobs:
        print(f"\n📋 Sample Output (first job):")
        sample = enriched_jobs[0]
        print(json.dumps({k: v for k, v in sample.items() if k != "Scrap_json"}, indent=2, default=str)[:1500] + "...")
    
    print(f"\n✨ Done!")


if __name__ == "__main__":
    main()
