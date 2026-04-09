# ============================
# SMARTRECRUITERS AI SCRAPER (FINAL STABLE + FIXED JSON)
# ============================

import requests
import json
import time
import re
import os
from urllib.parse import urlparse, parse_qs
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from openai import OpenAI

# ============================
# CONFIG
# ============================
load_dotenv()
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

HEADERS = {
    "User-Agent": "Mozilla/5.0"
}

LIST_API = "https://api.smartrecruiters.com/v1/companies/{company}/postings"
AI_DELAY = 0.5


# ============================
# EXTRACT COMPANY + FILTER
# ============================
def extract_company_and_filter(url):
    parsed = urlparse(url)
    parts = [p for p in parsed.path.split("/") if p]

    company = parts[-1] if parts else None

    query = parse_qs(parsed.query)

    location_filter = None
    for key in query:
        if "location" in key.lower():
            location_filter = query[key][0]

    return company, location_filter


# ============================
# FETCH JOB LIST
# ============================
def fetch_jobs(company, location_filter=None):
    jobs = []
    offset = 0
    limit = 100

    print(f"\n🔍 Fetching jobs for: {company}")
    print(f"🎯 Location filter: {location_filter}\n")

    while True:
        res = requests.get(
            LIST_API.format(company=company),
            headers=HEADERS,
            params={"offset": offset, "limit": limit}
        )

        if res.status_code != 200:
            print("❌ List API failed")
            break

        data = res.json()
        postings = data.get("content", [])

        if not postings:
            break

        for job in postings:
            city = job.get("location", {}).get("city", "")

            if location_filter:
                if location_filter.lower() not in city.lower():
                    continue

            jobs.append(job)

        print(f"[Page] collected: {len(jobs)}")

        offset += limit
        time.sleep(0.2)

    return jobs


# ============================
# FETCH HTML
# ============================
def fetch_html(job_id, company):
    url = f"https://jobs.smartrecruiters.com/{company}/{job_id}"

    try:
        res = requests.get(url, headers=HEADERS)
        if res.status_code == 200:
            return res.text
    except:
        pass

    return None


# ============================
# UNIVERSAL DATA EXTRACTION
# ============================
def extract_job_data(html):
    # OLD method
    match = re.search(r'window\.__INITIAL_STATE__\s*=\s*(\{.*\});', html)
    if match:
        try:
            return json.loads(match.group(1))
        except:
            pass

    # NEW method (JSON-LD)
    soup = BeautifulSoup(html, "html.parser")

    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string)

            if isinstance(data, dict) and data.get("@type") == "JobPosting":
                return {
                    "jobAd": {
                        "title": data.get("title"),
                        "sections": {
                            "description": {"text": data.get("description")}
                        }
                    }
                }
        except:
            continue

    # fallback
    return {"raw_html": html}


# ============================
# CLEAN HTML
# ============================
def clean_html(raw_html):
    if not raw_html:
        return ""

    soup = BeautifulSoup(raw_html, "html.parser")

    for tag in soup(["script", "style", "header", "footer", "nav"]):
        tag.decompose()

    text = soup.get_text(separator="\n")
    text = re.sub(r"\n\s*\n+", "\n\n", text)

    return text.strip()


# ============================
# BUILD AI INPUT
# ============================
def build_ai_input(state):
    if "jobAd" in state:
        job = state.get("jobAd", {})
        sections = job.get("sections", {})

        parts = []

        if job.get("title"):
            parts.append(f"TITLE:\n{job['title']}")

        for section in sections.values():
            text = section.get("text")
            if text:
                parts.append(clean_html(text))

        return "\n\n".join(parts)

    if "raw_html" in state:
        return clean_html(state["raw_html"])

    return ""


# ============================
# AI EXTRACTION (FIXED)
# ============================
def extract_with_ai(text, job, company):
    prompt = f"""
Extract structured job data.

Return ONLY valid JSON.
NO explanations.
NO markdown.
NO comments.

Schema:
{{
"id": "",
"title": "",
"company_name": "",
"job_link": "",
"experience": "",
"locations": [],
"educational_qualifications": "",
"required_skill_set": [],
"remote_type": "",
"posted_on": "",
"job_id": "",
"salary": "",
"is_active": true,
"first_seen": "",
"last_seen": "",
"additional_sections": [],
"Scrap_json": {{
"preferred_skills": [],
"tools_and_technologies": [],
"certifications": [],
"soft_skills": [],
"inferred_skills": [],
"benefits": []
}}
}}

Job Text:
{text}
"""

    try:
        time.sleep(AI_DELAY)

        response = client.responses.create(
            model="gpt-4.1-nano",
            input=prompt
        )

        raw_output = response.output[0].content[0].text.strip()

        # CLEAN OUTPUT
        cleaned = re.sub(r"```.*?```", "", raw_output, flags=re.DOTALL)

        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if match:
            cleaned = match.group(0)

        cleaned = re.sub(r",\s*}", "}", cleaned)
        cleaned = re.sub(r",\s*]", "]", cleaned)

        data = json.loads(cleaned)

        data["id"] = job.get("id")
        data["job_id"] = job.get("id")
        data["company_name"] = company
        data["job_link"] = f"https://jobs.smartrecruiters.com/{company}/{job.get('id')}"

        return data

    except Exception as e:
        print("   ❌ AI parse failed")

        with open("ai_error_dump.txt", "a", encoding="utf-8") as f:
            f.write("\n\n====================\n")
            f.write(raw_output)

        return None


# ============================
# MAIN
# ============================
def main():
    print("🚀 SMARTRECRUITERS AI SCRAPER (FINAL)\n")

    career_url = input("Enter SmartRecruiters URL: ").strip()

    company, location_filter = extract_company_and_filter(career_url)

    if not company:
        print("❌ Could not detect company")
        return

    print(f"✅ Company: {company}")

    jobs = fetch_jobs(company, location_filter)

    print(f"\n📦 Jobs collected: {len(jobs)}\n")

    results = []

    for i, job in enumerate(jobs, 1):
        print(f"[{i}/{len(jobs)}] {job.get('name')}")

        html = fetch_html(job.get("id"), company)
        if not html:
            continue

        state = extract_job_data(html)
        ai_input = build_ai_input(state)

        if not ai_input.strip():
            print("   ❌ Empty content")
            continue

        result = extract_with_ai(ai_input, job, company)

        if result:
            results.append(result)

    with open("final_jobs.json", "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    print(f"\n✅ DONE: {len(results)} jobs saved")


if __name__ == "__main__":
    main()