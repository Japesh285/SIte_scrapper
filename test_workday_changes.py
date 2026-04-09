# ============================
# UNIVERSAL TALEO SCRAPER (ANTI-BOT + REPLAY)
# ============================

from playwright.sync_api import sync_playwright
import requests
import json
import time

# ============================
# CONFIG
# ============================
START_URL = "https://genpact.taleo.net/careersection/sgy/jobsearch.ftl?lang=en"
MAX_PAGES = 50
DELAY = 0.5

captured_request = None


# ============================
# STEP 1 — CAPTURE REQUEST
# ============================
def capture_taleo_request():
    global captured_request

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)

        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
            java_script_enabled=True
        )

        page = context.new_page()

        # 🔥 Hide automation
        page.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', {
            get: () => undefined
        });
        """)

        def handle_response(response):
            global captured_request

            try:
                url = response.url.lower()

                # ❌ Ignore static files
                if any(x in url for x in [".css", ".js", ".png", ".jpg"]):
                    return

                # ✅ Strict Taleo API detection
                if "searchjobs" in url and response.request.method == "POST":
                    req = response.request

                    captured_request = {
                        "url": response.url,
                        "method": req.method,
                        "headers": dict(req.headers),
                        "payload": req.post_data,
                        "cookies": context.cookies()
                    }

                    print(f"✅ Captured API: {response.url}")

            except:
                pass

        page.on("response", handle_response)

        print(f"🌐 Building session...")

        # 🔥 Step 1: Visit root (important for cookies)
        page.goto("https://genpact.taleo.net", timeout=60000)
        page.wait_for_timeout(3000)

        # 🔥 Step 2: Go to job page
        print(f"🌐 Loading: {START_URL}")
        page.goto(START_URL, timeout=60000)
        page.wait_for_timeout(5000)

        # 🔥 Accept cookies if present
        try:
            btn = page.query_selector("button:has-text('Accept')")
            if btn:
                btn.click()
                page.wait_for_timeout(2000)
        except:
            pass

        # 🔥 Force search trigger
        try:
            btn = page.query_selector("button[type='submit'], input[type='submit']")
            if btn:
                print("🖱 Clicking search button...")
                btn.click()
                page.wait_for_timeout(4000)
        except:
            pass

        # 🔥 Press Enter fallback
        try:
            search_box = page.query_selector("input[type='text']")
            if search_box:
                search_box.click()
                search_box.press("Enter")
                page.wait_for_timeout(3000)
        except:
            pass

        # 🔥 JS fallback trigger
        try:
            page.evaluate("""
            document.querySelectorAll('button').forEach(b => {
                if (b.innerText.toLowerCase().includes('search')) {
                    b.click();
                }
            });
            """)
            page.wait_for_timeout(3000)
        except:
            pass

        # 🔥 Scroll to trigger network
        for _ in range(10):
            page.mouse.wheel(0, 5000)
            page.wait_for_timeout(1500)

        # final wait
        page.wait_for_timeout(5000)

        browser.close()

    return captured_request


# ============================
# STEP 2 — PAGINATION HANDLER
# ============================
def update_payload(payload_str, page):
    try:
        data = json.loads(payload_str)

        if "paging" in data:
            data["paging"]["pageNo"] = page

        elif "pageNo" in data:
            data["pageNo"] = page

        elif "start" in data:
            data["start"] = page * 20

        elif "offset" in data:
            data["offset"] = page * 20

        return json.dumps(data)

    except:
        return payload_str


# ============================
# STEP 3 — REPLAY REQUEST
# ============================
def replay_and_paginate(captured):
    print("\n⬇️ Starting pagination...\n")

    session = requests.Session()

    # Attach cookies
    for c in captured.get("cookies", []):
        session.cookies.set(c["name"], c["value"], domain=c.get("domain"))

    # Clean headers
    headers = {
        k: v for k, v in captured["headers"].items()
        if k.lower() not in ["content-length", "host"]
    }

    session.headers.update(headers)

    all_jobs = []
    seen_ids = set()

    for page in range(0, MAX_PAGES):

        payload = captured["payload"]

        if payload:
            payload = update_payload(payload, page)

        try:
            if captured["method"] == "POST":
                res = session.post(captured["url"], data=payload, timeout=30)
            else:
                res = session.get(captured["url"], timeout=30)

            data = res.json()

        except Exception as e:
            print(f"❌ Failed page {page}: {e}")
            break

        jobs = (
            data.get("requisitionList")
            or data.get("jobs")
            or data.get("items")
            or []
        )

        if not jobs:
            print(f"⏹️ No more jobs at page {page}")
            break

        new_count = 0

        for job in jobs:
            job_id = str(job.get("jobId") or job.get("id") or "")

            if job_id and job_id in seen_ids:
                continue

            if job_id:
                seen_ids.add(job_id)

            all_jobs.append({
                "title": job.get("title") or job.get("jobTitle"),
                "location": job.get("location"),
                "id": job_id,
                "date": job.get("postedDate")
            })

            new_count += 1

        print(f"[Page {page}] +{new_count} jobs (Total: {len(all_jobs)})")

        if new_count == 0:
            print("⏹️ No new jobs — stopping")
            break

        time.sleep(DELAY)

    return all_jobs


# ============================
# MAIN
# ============================
def main():
    print("🚀 UNIVERSAL TALEO SCRAPER (ANTI-BOT)\n")

    captured = capture_taleo_request()

    if not captured:
        print("❌ Failed to capture Taleo API (likely blocked or no trigger)")
        return

    jobs = replay_and_paginate(captured)

    print(f"\n✅ FINAL: {len(jobs)} jobs")

    with open("taleo_jobs.json", "w", encoding="utf-8") as f:
        json.dump(jobs, f, indent=2, ensure_ascii=False)

    print("💾 Saved to taleo_jobs.json")


if __name__ == "__main__":
    main()