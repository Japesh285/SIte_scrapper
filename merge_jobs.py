#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DEBUG SCRIPT: Save RAW responses from Capgemini careers
- Raw API JSON response
- Raw HTML for first 5 job detail pages
NO parsing, NO cleaning — just inspect what we're working with.
"""

import json
import time
import requests
from playwright.sync_api import sync_playwright
from urllib.parse import urlparse, parse_qs

# ============================
# CONFIG
# ============================
TARGET_URL = "https://www.capgemini.com/in-en/careers/join-capgemini/job-search/?page=1&size=11&country_code=in-en"
NUM_JOBS_TO_DEBUG = 5
REQUEST_DELAY = 1.5
OUTPUT_PREFIX = "capgemini_debug"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/html, */*",
    "Accept-Language": "en-US,en;q=0.9",
}

# ============================
# STEP 1: Capture the RAW API response
# ============================
def capture_raw_api_response(target_url):
    """Use Playwright to intercept and save the raw JSON API response."""
    print(f"🌐 Loading: {target_url}")
    
    raw_api_data = None
    api_url_found = None
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=HEADERS["User-Agent"])
        page = context.new_page()
        
        def handle_response(response):
            nonlocal raw_api_data, api_url_found
            try:
                content_type = response.headers.get("content-type", "")
                if "application/json" not in content_type.lower():
                    return
                url = response.url
                # Look for the job-search API (not filters)
                if "job-search" in url and "api" in url:
                    text = response.text()
                    if text and len(text) > 100:
                        raw_api_data = text  # Save as raw string, NOT parsed
                        api_url_found = url
                        print(f"✅ Captured raw API: {url[:80]}... ({len(text)} bytes)")
            except:
                pass
        
        page.on("response", handle_response)
        page.goto(target_url, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(3000)  # Let APIs fire
        browser.close()
    
    return raw_api_data, api_url_found


# ============================
# STEP 2: Fetch RAW HTML for job detail pages
# ============================
def fetch_raw_job_html(job_url, session, index):
    """Fetch and save raw HTML for a single job page."""
    try:
        print(f"[{index}] Fetching raw HTML: {job_url[:70]}...")
        resp = session.get(job_url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        
        # Save raw HTML exactly as received
        filename = f"{OUTPUT_PREFIX}_job_{index}_raw.html"
        with open(filename, "w", encoding="utf-8") as f:
            f.write(resp.text)
        print(f"   💾 Saved: {filename} ({len(resp.text)} bytes)")
        return resp.text
    except Exception as e:
        print(f"   ❌ Failed: {e}")
        return None


# ============================
# STEP 3: Extract job URLs from raw API JSON (minimal parsing just for URLs)
# ============================
def extract_job_urls_from_raw_api(raw_api_json_text):
    """Minimal parsing to extract job URLs from raw API response."""
    try:
        data = json.loads(raw_api_json_text)
        jobs = []
        
        # Recursively find job-like objects with URLs
        def recurse(obj):
            if isinstance(obj, dict):
                # Check if this looks like a job object
                has_title = any(k in obj for k in ["title", "jobTitle", "positionTitle"])
                has_url = any(k in obj for k in ["url", "applyUrl", "apply_job_url", "jobUrl"])
                if has_title and has_url:
                    url = next((obj[k] for k in ["url", "applyUrl", "apply_job_url", "jobUrl"] if k in obj and obj[k]), None)
                    title = next((obj[k] for k in ["title", "jobTitle", "positionTitle"] if k in obj and obj[k]), None)
                    job_id = next((str(obj[k]) for k in ["id", "jobId", "requisitionId"] if k in obj and obj[k]), None)
                    if url and title:
                        jobs.append({"url": url, "title": title, "id": job_id})
                # Recurse into values
                for v in obj.values():
                    recurse(v)
            elif isinstance(obj, list):
                for item in obj:
                    recurse(item)
        
        recurse(data)
        return jobs
    except Exception as e:
        print(f"⚠️ Could not parse API JSON for URLs: {e}")
        return []


# ============================
# MAIN
# ============================
def main():
    print(f"🔍 Capgemini RAW Debug Script")
    print(f"   Target: {TARGET_URL}")
    print(f"   Jobs to debug: {NUM_JOBS_TO_DEBUG}\n")
    
    # --- Step 1: Capture raw API response ---
    print("📡 Step 1: Capturing RAW API response...")
    raw_api_text, api_url = capture_raw_api_response(TARGET_URL)
    
    if not raw_api_text:
        print("❌ Failed to capture API response. Check network/target URL.")
        return
    
    # Save raw API JSON
    api_filename = f"{OUTPUT_PREFIX}_api_raw.json"
    with open(api_filename, "w", encoding="utf-8") as f:
        f.write(raw_api_text)
    print(f"💾 Saved raw API: {api_filename} ({len(raw_api_text)} bytes)\n")
    
    # --- Step 2: Extract job URLs from API ---
    print("🔗 Step 2: Extracting job URLs from API...")
    jobs = extract_job_urls_from_raw_api(raw_api_text)
    print(f"   Found {len(jobs)} job entries with URLs\n")
    
    if not jobs:
        print("⚠️ No job URLs found in API. Check API structure.")
        return
    
    # --- Step 3: Fetch raw HTML for first N jobs ---
    print(f"🌐 Step 3: Fetching RAW HTML for first {NUM_JOBS_TO_DEBUG} jobs...\n")
    session = requests.Session()
    
    fetched = 0
    for i, job in enumerate(jobs[:NUM_JOBS_TO_DEBUG], 1):
        url = job["url"]
        title = job["title"][:50]
        print(f"[{i}/{NUM_JOBS_TO_DEBUG}] {title}")
        html = fetch_raw_job_html(url, session, i)
        if html:
            fetched += 1
        time.sleep(REQUEST_DELAY)
    
    # --- Step 4: Generate quick summary ---
    print(f"\n📋 Debug Summary:")
    print(f"   ✅ Raw API saved: {api_filename}")
    print(f"   ✅ Raw HTML saved: {fetched}/{NUM_JOBS_TO_DEBUG} jobs")
    print(f"   🔍 Next: Open the files and inspect:")
    print(f"      - {api_filename} → See what fields the API actually returns")
    print(f"      - *_raw.html files → See the actual HTML structure")
    
    # Bonus: Print top-level keys from API for quick glance
    try:
        api_data = json.loads(raw_api_text)
        if isinstance(api_data, dict):
            print(f"\n🔑 Top-level API keys: {list(api_data.keys())[:15]}")
        if isinstance(api_data, list) and len(api_data) > 0 and isinstance(api_data[0], dict):
            print(f"🔑 First job keys: {list(api_data[0].keys())[:20]}")
    except:
        pass
    
    print(f"\n✨ Done! Inspect the files and let me know what fields you see.")


if __name__ == "__main__":
    main()