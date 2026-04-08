import json
import asyncio
import logging
from pathlib import Path
from typing import List

import pandas as pd
from fastapi import FastAPI
from pydantic import BaseModel
from openai import AsyncOpenAI
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

MASTER_XLSX = OUTPUT_DIR / "master_jobs.xlsx"
MASTER_JSON = OUTPUT_DIR / "master_jobs.json"

MAX_CONCURRENT = 5
semaphore = asyncio.Semaphore(MAX_CONCURRENT)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


# ======================
# FASTAPI
# ======================

app = FastAPI()


class WorkdayRequest(BaseModel):
    file_path: str
    limit: int = 50


class IngestRequest(BaseModel):
    file_path: str


# ======================
# MASTER APPEND
# ======================

def append_to_master(jobs: List[dict]):
    if not jobs:
        return

    df_new = pd.DataFrame(jobs)

    if MASTER_XLSX.exists():
        df_existing = pd.read_excel(MASTER_XLSX)
        df_final = pd.concat([df_existing, df_new], ignore_index=True)
    else:
        df_final = df_new

    # optional dedup
    if "job_id" in df_final.columns:
        df_final.drop_duplicates(subset=["job_id"], inplace=True)

    df_final.to_excel(MASTER_XLSX, index=False)

    # JSON
    if MASTER_JSON.exists():
        with open(MASTER_JSON, "r") as f:
            existing = json.load(f)
    else:
        existing = []

    existing.extend(jobs)

    with open(MASTER_JSON, "w") as f:
        json.dump(existing, f, indent=2)

    logging.info(f"✅ Appended {len(jobs)} jobs to master")


# ======================
# LLM CALL (WORKDAY ONLY)
# ======================

PROMPT = PROMPT = """
You are a strict JSON generator.

Convert the input job into EXACTLY this schema.

Return ONLY valid JSON. No explanation.

Schema:
{
  "id": string,
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
  "additional_sections": array,
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
  },
  "ai_usage": {
    "input_tokens": number,
    "output_tokens": number,
    "total_tokens": number
  }
}

Rules:
- If field missing → use "" or []
- Extract skills from description
- Infer experience if possible
- job_id = id
- locations must be array
- is_active = true
- ai_usage can be zeros
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

            logging.info(f"✅ {parsed.get('title')}")

            return parsed

        except Exception as e:
            logging.error(f"❌ Failed job: {e}")
            return None


# ======================
# WORKDAY ENDPOINT
# ======================

@app.post("/process-workday")
async def process_workday(req: WorkdayRequest):
    file_path = Path(req.file_path)

    with open(file_path, "r") as f:
        data = json.load(f)

    jobs = data.get("jobs", [])[:req.limit]

    logging.info(f"🚀 Processing {len(jobs)} Workday jobs")

    tasks = [process_job(job) for job in jobs]
    results = await asyncio.gather(*tasks)

    parsed_jobs = [r for r in results if r]

    append_to_master(parsed_jobs)

    return {
        "status": "done",
        "processed": len(parsed_jobs),
        "output": str(MASTER_XLSX.resolve())
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
        "output": str(MASTER_XLSX.resolve())
    }