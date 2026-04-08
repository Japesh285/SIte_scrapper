import asyncio
import json
import logging
import os
from typing import Dict, List, Optional

import pandas as pd
import aiofiles
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from openai import AsyncOpenAI
from dotenv import load_dotenv
from bs4 import BeautifulSoup

# =========================
# ENV
# =========================
load_dotenv()

API_KEY = os.getenv("OPENAI_API_KEY")
if not API_KEY:
    raise ValueError("OPENAI_API_KEY missing in .env")

client = AsyncOpenAI(api_key=API_KEY)

# =========================
# CONFIG
# =========================
MODEL = "gpt-4.1-nano"
CONCURRENT_REQUESTS = 5
REQUEST_DELAY = 0.3
MAX_DESCRIPTION_LENGTH = 4000

# =========================
# LOGGING
# =========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

# =========================
# FASTAPI
# =========================
app = FastAPI(title="Job Processing Worker")

# =========================
# REQUEST MODEL
# =========================
class JobRequest(BaseModel):
    file_path: str
    limit: int = 50  # 👈 default cap

# =========================
# UTILS
# =========================
def clean_html(html: str) -> str:
    return BeautifulSoup(html or "", "html.parser").get_text(" ", strip=True)

def safe_json_load(text: str) -> Optional[Dict]:
    try:
        return json.loads(text)
    except:
        return None

async def write_jsonl(file: str, data: Dict):
    async with aiofiles.open(file, "a", encoding="utf-8") as f:
        await f.write(json.dumps(data, ensure_ascii=False) + "\n")

# =========================
# PROMPT
# =========================
SYSTEM_PROMPT = """
Return ONLY valid JSON. No explanation.

Schema:
{
  "id": string,
  "title": string,
  "company_name": string,
  "job_link": string,
  "experience": string,
  "locations": [string],
  "educational_qualifications": string,
  "required_skill_set": [string],
  "remote_type": string,
  "posted_on": string,
  "job_id": string,
  "salary": string,
  "is_active": boolean,
  "first_seen": string,
  "last_seen": string,
  "additional_sections": [],
  "Scrap_json": object
}
"""

def build_prompt(job: Dict) -> str:
    cleaned_description = clean_html(job.get("description", ""))[:MAX_DESCRIPTION_LENGTH]

    payload = {
        "title": job.get("title"),
        "location": job.get("location"),
        "url": job.get("url"),
        "description": cleaned_description,
        "raw_api": job.get("_raw_api"),
        "jobPostingInfo": job.get("_raw_detail", {}).get("jobPostingInfo"),
        "hiringOrganization": job.get("_raw_detail", {}).get("hiringOrganization"),
    }

    return f"Extract structured job data:\n{json.dumps(payload, ensure_ascii=False)}"

# =========================
# PROCESS ONE JOB
# =========================
async def process_job(job: Dict, semaphore: asyncio.Semaphore, debug_dir: str):
    async with semaphore:
        try:
            prompt = build_prompt(job)

            # Save prompt
            await write_jsonl(f"{debug_dir}/prompts.jsonl", {
                "title": job.get("title"),
                "prompt": prompt
            })

            for attempt in range(3):
                try:
                    response = await client.chat.completions.create(
                        model=MODEL,
                        messages=[
                            {"role": "system", "content": SYSTEM_PROMPT},
                            {"role": "user", "content": prompt}
                        ],
                        temperature=0
                    )

                    raw_output = response.choices[0].message.content

                    # Save raw output
                    await write_jsonl(f"{debug_dir}/outputs.jsonl", {
                        "title": job.get("title"),
                        "response": raw_output
                    })

                    parsed = safe_json_load(raw_output)

                    if not parsed:
                        raise ValueError("Invalid JSON")

                    usage = response.usage
                    parsed["ai_usage"] = {
                        "input_tokens": usage.prompt_tokens,
                        "output_tokens": usage.completion_tokens,
                        "total_tokens": usage.total_tokens
                    }

                    logging.info(f"✅ {job.get('title')}")
                    await asyncio.sleep(REQUEST_DELAY)

                    return parsed

                except Exception as e:
                    logging.warning(f"Retry {attempt+1} failed: {job.get('title')} | {e}")
                    await asyncio.sleep(1)

            logging.error(f"❌ Failed after retries: {job.get('title')}")
            return None

        except Exception as e:
            logging.error(f"🔥 Error: {job.get('title')} | {e}")
            return None

# =========================
# PROCESS FILE
# =========================
async def process_file(file_path: str, limit: int) -> str:
    if not os.path.exists(file_path):
        raise FileNotFoundError("File not found")

    with open(file_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    all_jobs = data.get("jobs", [])
    jobs = all_jobs[:limit]

    logging.info(f"Processing {len(jobs)} out of {len(all_jobs)} jobs")

    site_name = data.get("domain", "default_site")

    # Output dirs
    output_dir = f"./output/{site_name}"
    debug_dir = f"{output_dir}/debug"

    os.makedirs(output_dir, exist_ok=True)
    os.makedirs(debug_dir, exist_ok=True)

    semaphore = asyncio.Semaphore(CONCURRENT_REQUESTS)

    tasks = [process_job(job, semaphore, debug_dir) for job in jobs]
    results = await asyncio.gather(*tasks)

    parsed_jobs = [r for r in results if r]

    # Save JSON
    json_path = f"{output_dir}/parsed.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(parsed_jobs, f, indent=2, ensure_ascii=False)

    # Save Excel
    excel_path = f"{output_dir}/output.xlsx"
    df = pd.json_normalize(parsed_jobs)
    df.to_excel(excel_path, index=False)

    return excel_path

# =========================
# API ENDPOINT
# =========================
@app.post("/process")
async def process_jobs(req: JobRequest):
    try:
        output_path = await process_file(req.file_path, req.limit)

        return {
            "status": "success",
            "processed_limit": req.limit,
            "output_file": output_path
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))