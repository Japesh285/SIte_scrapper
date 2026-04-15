import json
import asyncio
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import List
from urllib.parse import parse_qs, urlparse

import pandas as pd
import requests
from fastapi import FastAPI
from pydantic import BaseModel
from openai import AsyncOpenAI
from bs4 import BeautifulSoup
from dotenv import load_dotenv
import os

# ======================
# CONFIG
# ======================

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
client = AsyncOpenAI(api_key=OPENAI_API_KEY)

OUTPUT_DIR = Path("output")
OUTPUT_DIR.mkdir(exist_ok=True)

MASTER_CSV = OUTPUT_DIR / "master_jobs.csv"
MASTER_JSON = OUTPUT_DIR / "master_jobs.json"

FINAL_CSV_COLUMNS = [
    "title",
    "company_name",
    "job_link",
    "experience",
    "locations",
    "educational_qualifications",
    "required_skill_set",
    "remote_type",
    "posted_on",
    "job_id",
    "salary",
    "is_active",
    "first_seen",
    "last_seen",
    "job_summary",
    "key_responsibilities",
    "additional_sections",
    "about_us",
    "Scrap_json",
]

MAX_CONCURRENT = 5
semaphore = asyncio.Semaphore(MAX_CONCURRENT)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


# ======================
# FASTAPI
# ======================

app = FastAPI()


class WorkdayRequest(BaseModel):
    file_path: str


class IngestRequest(BaseModel):
    file_path: str


class SmartRecruitersRequest(BaseModel):
    url: str


# ======================
# MASTER APPEND
# ======================

def append_to_master(jobs: List[dict]):
    if not jobs:
        return

    normalized_jobs = [_normalize_job_record(job) for job in jobs]
    csv_rows = [_to_csv_row(job) for job in normalized_jobs]
    df_new = pd.DataFrame(csv_rows, columns=FINAL_CSV_COLUMNS)

    if MASTER_CSV.exists():
        df_existing = pd.read_csv(MASTER_CSV, keep_default_na=False)
        df_final = pd.concat([df_existing, df_new], ignore_index=True)
    else:
        df_final = df_new

    # optional dedup
    if "job_id" in df_final.columns:
        df_final.drop_duplicates(subset=["job_id"], inplace=True)

    df_final = df_final.reindex(columns=FINAL_CSV_COLUMNS)
    df_final.to_csv(MASTER_CSV, index=False)

    # JSON
    if MASTER_JSON.exists():
        with open(MASTER_JSON, "r", encoding="utf-8") as f:
            existing = json.load(f)
    else:
        existing = []

    existing.extend(normalized_jobs)

    with open(MASTER_JSON, "w", encoding="utf-8") as f:
        json.dump(existing, f, indent=2, ensure_ascii=False)

    logging.info(f"✅ Appended {len(jobs)} jobs to master")


def _normalize_job_record(job: dict) -> dict:
    record = dict(job)
    record.pop("id", None)
    record.pop("ai_usage", None)

    normalized = {
        "title": str(record.get("title") or ""),
        "company_name": str(record.get("company_name") or ""),
        "job_link": str(record.get("job_link") or record.get("url") or ""),
        "experience": str(record.get("experience") or ""),
        "locations": record.get("locations") or [],
        "educational_qualifications": str(record.get("educational_qualifications") or ""),
        "required_skill_set": record.get("required_skill_set") or [],
        "remote_type": str(record.get("remote_type") or ""),
        "posted_on": str(record.get("posted_on") or ""),
        "job_id": str(record.get("job_id") or record.get("id") or ""),
        "salary": str(record.get("salary") or ""),
        "is_active": bool(record.get("is_active", True)),
        "first_seen": str(record.get("first_seen") or ""),
        "last_seen": str(record.get("last_seen") or ""),
        "job_summary": "",
        "key_responsibilities": record.get("key_responsibilities") or [],
        "additional_sections": "",
        "about_us": "",
        "Scrap_json": record.get("Scrap_json") or {},
    }
    return normalized


def _to_csv_row(job: dict) -> dict:
    row = {key: _serialize_cell(job.get(key)) for key in FINAL_CSV_COLUMNS}
    for key in ("posted_on", "first_seen", "last_seen"):
        row[key] = _format_date_for_csv(job.get(key))
    row["job_summary"] = ""
    row["additional_sections"] = ""
    row["about_us"] = ""
    row["Scrap_json"] = ""
    return row


def _serialize_cell(value):
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    if value is None:
        return ""
    return value


def _format_date_for_csv(value) -> str:
    if value is None:
        return ""

    raw = str(value).strip()
    if not raw:
        return ""

    candidates = [raw]
    if raw.endswith("Z"):
        candidates.append(raw[:-1] + "+00:00")

    for candidate in candidates:
        try:
            return datetime.fromisoformat(candidate).strftime("%d-%m-%Y")
        except ValueError:
            pass

    try:
        parsed = pd.to_datetime(raw, errors="coerce", utc=False)
        if pd.notna(parsed):
            return parsed.strftime("%d-%m-%Y")
    except Exception:
        pass

    return raw


def reset_master_outputs():
    for path in (MASTER_CSV, MASTER_JSON):
        try:
            if path.exists():
                path.unlink()
                logging.info("🧹 Deleted %s", path)
        except Exception as exc:
            logging.error("❌ Failed deleting %s: %s", path, exc)


# ======================
# LLM CALL (WORKDAY ONLY)
# ======================

PROMPT = PROMPT = """
You are a strict JSON generator.

Convert the input job into EXACTLY this schema.

Return ONLY valid JSON. No explanation.

Schema:
{
  "title": string,
  "company_name": string,
  "job_link": string,
  "experience": string,
  "locations": string[],
  "educational_qualifications": string,
  "required_skill_set": string[],
  "remote_type": string,
  "posted_on": string,
  "job_id": string,
  "salary": string,
  "is_active": boolean,
  "first_seen": string,
  "last_seen": string,
  "key_responsibilities": array,
  "Scrap_json": {
    "url": string,
    "strategy": string,
    "parser_used": string,
    "confidence": number,
    "ai_forced": boolean,
    "preferred_skills": array,
    "tools_and_technologies": array,
    "certifications": array,
    "soft_skills": array,
    "inferred_skills": array,
    "benefits": array
  }
}

Rules:
- If field missing → use "" or []
- Extract skills from description
- Infer experience if possible
- locations must be array
- is_active = true
"""


async def process_job(job: dict):
    async with semaphore:
        try:
            response = await client.chat.completions.create(
                model="gpt-4.1-nano",
                messages=[
                    {"role": "system", "content": PROMPT},
                    {"role": "user", "content": json.dumps(job)}
                ],
                temperature=0
            )

            raw = response.choices[0].message.content.strip()

            parsed = json.loads(raw)
            parsed.setdefault("locations", [])
            parsed.setdefault("required_skill_set", [])
            parsed.setdefault("job_summary", "")
            parsed.setdefault("key_responsibilities", [])
            parsed.setdefault("additional_sections", "")
            parsed.setdefault("about_us", "")
            parsed.setdefault("Scrap_json", {})
            parsed.pop("ai_usage", None)

            logging.info(f"✅ {parsed.get('title')}")

            return parsed

        except Exception as e:
            logging.error(f"❌ Failed job: {e}")
            return None


def extract_company_and_filter(url: str):
    parsed = urlparse(url)
    parts = [p for p in parsed.path.split("/") if p]
    company = parts[-1] if parts else None

    query = parse_qs(parsed.query)
    location_filter = None
    for key in query:
        if "location" in key.lower() and query[key]:
            location_filter = query[key][0]

    return company, location_filter


def fetch_smartrecruiters_jobs(company: str, location_filter: str | None = None):
    jobs = []
    offset = 0
    limit = 100
    list_api = f"https://api.smartrecruiters.com/v1/companies/{company}/postings"
    headers = {"User-Agent": "Mozilla/5.0"}

    while True:
        res = requests.get(
            list_api,
            headers=headers,
            params={"offset": offset, "limit": limit},
            timeout=30,
        )

        if res.status_code != 200:
            logging.error("❌ SmartRecruiters list API failed for %s", company)
            break

        data = res.json()
        postings = data.get("content", [])
        if not postings:
            break

        for job in postings:
            city = job.get("location", {}).get("city", "")
            if location_filter and location_filter.lower() not in city.lower():
                continue
            jobs.append(job)

        offset += limit

    return jobs


def fetch_smartrecruiters_html(job_id: str, company: str):
    url = f"https://jobs.smartrecruiters.com/{company}/{job_id}"
    try:
        res = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
        if res.status_code == 200:
            return res.text
    except Exception as exc:
        logging.error("❌ SmartRecruiters HTML fetch failed for %s: %s", job_id, exc)
    return None


def extract_smartrecruiters_job_data(html: str):
    match = re.search(r'window\.__INITIAL_STATE__\s*=\s*(\{.*\});', html)
    if match:
        try:
            return json.loads(match.group(1))
        except Exception:
            pass

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
                        },
                    }
                }
        except Exception:
            continue

    return {"raw_html": html}


def clean_html(raw_html: str):
    if not raw_html:
        return ""

    soup = BeautifulSoup(raw_html, "html.parser")
    for tag in soup(["script", "style", "header", "footer", "nav"]):
        tag.decompose()

    text = soup.get_text(separator="\n")
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def build_smartrecruiters_ai_input(state: dict):
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


SMARTRECRUITERS_PROMPT = """
Extract structured job data.

Return ONLY valid JSON.
NO explanations.
NO markdown.
NO comments.

Schema:
{
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
  "key_responsibilities": [],
  "Scrap_json": {
    "preferred_skills": [],
    "tools_and_technologies": [],
    "certifications": [],
    "soft_skills": [],
    "inferred_skills": [],
    "benefits": []
  }
}
"""


async def process_smartrecruiters_job(job: dict, company: str):
    try:
        html = await asyncio.to_thread(fetch_smartrecruiters_html, job.get("id", ""), company)
        if not html:
            return None

        state = extract_smartrecruiters_job_data(html)
        ai_input = build_smartrecruiters_ai_input(state)
        if not ai_input.strip():
            return None

        response = await client.chat.completions.create(
            model="gpt-4.1-nano",
            messages=[
                {"role": "system", "content": SMARTRECRUITERS_PROMPT},
                {"role": "user", "content": ai_input},
            ],
            temperature=0,
        )

        raw = response.choices[0].message.content.strip()
        cleaned = re.sub(r"```(?:json)?", "", raw).replace("```", "").strip()
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if match:
            cleaned = match.group(0)
        cleaned = re.sub(r",\s*}", "}", cleaned)
        cleaned = re.sub(r",\s*]", "]", cleaned)

        parsed = json.loads(cleaned)
        parsed["job_id"] = job.get("id", "")
        parsed["company_name"] = company
        parsed["job_link"] = f"https://jobs.smartrecruiters.com/{company}/{job.get('id', '')}"
        parsed.setdefault("locations", [])
        parsed.setdefault("required_skill_set", [])
        parsed.setdefault("job_summary", "")
        parsed.setdefault("key_responsibilities", [])
        parsed.setdefault("additional_sections", "")
        parsed.setdefault("about_us", "")
        parsed.setdefault("Scrap_json", {})
        parsed.pop("ai_usage", None)

        logging.info("✅ SmartRecruiters %s", parsed.get("title"))
        return parsed
    except Exception as exc:
        logging.error("❌ SmartRecruiters job failed: %s", exc)
        return None


# ======================
# WORKDAY ENDPOINT
# ======================

@app.post("/process-workday")
async def process_workday(req: WorkdayRequest):
    file_path = Path(req.file_path)

    with open(file_path, "r") as f:
        data = json.load(f)

    jobs = data.get("jobs", [])

    logging.info(f"🚀 Processing {len(jobs)} Workday jobs")

    tasks = [process_job(job) for job in jobs]
    results = await asyncio.gather(*tasks)

    parsed_jobs = [r for r in results if r]

    append_to_master(parsed_jobs)

    return {
        "status": "done",
        "processed": len(parsed_jobs),
        "output": str(MASTER_CSV.resolve())
    }


# ======================
# NON-WORKDAY ENDPOINT
# ======================

@app.post("/ingest-json")
def ingest_json(req: IngestRequest):
    file_path = Path(req.file_path)

    with open(file_path, "r") as f:
        data = json.load(f)

    jobs = data.get("jobs", data)

    append_to_master(jobs)

    return {
        "status": "ingested",
        "count": len(jobs),
        "output": str(MASTER_CSV.resolve())
    }


@app.post("/process-smartrecruiters")
async def process_smartrecruiters(req: SmartRecruitersRequest):
    company, location_filter = extract_company_and_filter(req.url)
    if not company:
        return {
            "status": "failed",
            "processed": 0,
            "json_file": "",
            "output": str(MASTER_CSV.resolve()),
            "error": "could_not_detect_company",
        }

    jobs = await asyncio.to_thread(fetch_smartrecruiters_jobs, company, location_filter)

    logging.info("🚀 Processing %s SmartRecruiters jobs for %s", len(jobs), company)

    tasks = [process_smartrecruiters_job(job, company) for job in jobs]
    results = await asyncio.gather(*tasks)
    parsed_jobs = [r for r in results if r]

    json_file = OUTPUT_DIR / f"smartrecruiters_{company}_jobs.json"
    with open(json_file, "w", encoding="utf-8") as f:
        json.dump(parsed_jobs, f, indent=2, ensure_ascii=False)

    append_to_master(parsed_jobs)

    return {
        "status": "done",
        "processed": len(parsed_jobs),
        "json_file": str(json_file.resolve()),
        "output": str(MASTER_CSV.resolve()),
    }


@app.post("/reset-master")
def reset_master():
    reset_master_outputs()
    return {
        "status": "reset",
        "output": str(MASTER_CSV.resolve()),
    }
