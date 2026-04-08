import asyncio
import json
import logging
import os
from typing import Any, Dict, List, Optional

import pandas as pd
import aiofiles
from openai import AsyncOpenAI
from dotenv import load_dotenv
from bs4 import BeautifulSoup

# =========================
# LOAD ENV
# =========================
load_dotenv()

API_KEY = os.getenv("OPENAI_API_KEY")

if not API_KEY:
    raise ValueError("❌ OPENAI_API_KEY not found in .env")

# =========================
# CONFIG
# =========================
INPUT_FILE = "/home/hp_jp/ai_scrapper/raw_json/aig.wd1.myworkdayjobs.com/scrape_result_20260408_082622.json"
OUTPUT_JSON = "parsed_jobs.json"
OUTPUT_XLSX = "aig.parsed_jobs.xlsx"

DEBUG_PROMPTS = "debug_prompts.jsonl"
DEBUG_OUTPUT = "debug_llm_output.jsonl"

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
# CLIENT
# =========================
client = AsyncOpenAI(api_key=API_KEY)

# =========================
# UTIL: CLEAN HTML
# =========================
def clean_html(html: str) -> str:
    return BeautifulSoup(html or "", "html.parser").get_text(" ", strip=True)

# =========================
# UTIL: SAFE JSON
# =========================
def safe_json_load(text: str) -> Optional[Dict]:
    try:
        return json.loads(text)
    except:
        return None

# =========================
# DEBUG LOGGER
# =========================
async def write_jsonl(file: str, data: Dict):
    async with aiofiles.open(file, "a", encoding="utf-8") as f:
        await f.write(json.dumps(data, ensure_ascii=False) + "\n")

# =========================
# PROMPT
# =========================
SYSTEM_PROMPT = """
You are a strict JSON generator.

Return ONLY valid JSON.
No markdown. No explanation.

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
  "additional_sections": [
    {
      "section_title": string,
      "content": string
    }
  ],
  "Scrap_json": object
}
"""

def build_prompt(job: Dict[str, Any]) -> str:
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

    return f"""
Extract structured job data.

Rules:
- Fill all fields
- Clean + infer where needed
- Use "" or [] if missing
- DO NOT output anything except JSON

Job Data:
{json.dumps(payload, ensure_ascii=False)}
"""

# =========================
# PROCESS ONE JOB
# =========================
async def process_job(job: Dict, semaphore: asyncio.Semaphore) -> Optional[Dict]:
    async with semaphore:
        try:
            prompt = build_prompt(job)

            # Save prompt
            await write_jsonl(DEBUG_PROMPTS, {
                "job_title": job.get("title"),
                "prompt": prompt
            })

            # Retry logic
            for attempt in range(3):
                try:
                    response = await client.chat.completions.create(
                        model=MODEL,
                        messages=[
                            {"role": "system", "content": SYSTEM_PROMPT},
                            {"role": "user", "content": prompt},
                        ],
                        temperature=0
                    )

                    raw_output = response.choices[0].message.content

                    # Save raw output
                    await write_jsonl(DEBUG_OUTPUT, {
                        "job_title": job.get("title"),
                        "response": raw_output
                    })

                    parsed = safe_json_load(raw_output)

                    if not parsed:
                        raise ValueError("Invalid JSON")

                    # Add token usage
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
# MAIN
# =========================
async def main():
    # Load input
    with open(INPUT_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    jobs: List[Dict] = data.get("jobs", [])
    logging.info(f"Total jobs: {len(jobs)}")

    semaphore = asyncio.Semaphore(CONCURRENT_REQUESTS)

    tasks = [process_job(job, semaphore) for job in jobs]

    results = await asyncio.gather(*tasks)

    parsed_jobs = [r for r in results if r]

    logging.info(f"Parsed jobs: {len(parsed_jobs)}")

    # Save JSON
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(parsed_jobs, f, indent=2, ensure_ascii=False)

    # Save Excel
    df = pd.json_normalize(parsed_jobs)
    df.to_excel(OUTPUT_XLSX, index=False)

    logging.info("🎉 Pipeline complete")

# =========================
# RUN
# =========================
if __name__ == "__main__":
    asyncio.run(main())