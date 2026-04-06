"""AI-powered field extraction — complete job posting enrichment."""

import json

import httpx

from app.core.config import OPENAI_API_KEY
from app.core.logger import logger
from app.job_detail_engine.utils.content_filter import filter_content_for_ai

_AI_SYSTEM_PROMPT = """\
You are a lightweight job metadata extraction system.

Extract ONLY these fields from the job posting:

{
  "title": "",
  "company_name": "",
  "location": [],
  "experience": "",
  "employment_type": "",
  "salary": "",
  "posted_on": "",
  "remote_type": "",
  "job_id": "",
  "required_skills": [],
  "education": "",
  "qualifications": [],
  "additional_sections": []
}

RULES:
- Return "" for strings and [] for arrays when data is missing
- Keep arrays short (max 15 items, each item max 50 chars)
- additional_sections: only keep short metadata (content < 200 chars)
- Do NOT extract job_description, key_responsibilities, about_company, benefits
- Total output must stay under 600 tokens
- Valid JSON only, no markdown, no explanations

OUTPUT:"""

# Fields the AI can return (reduced set)
_AI_FIELDS = {
    "title",
    "company_name",
    "location",
    "experience",
    "employment_type",
    "salary",
    "posted_on",
    "remote_type",
    "job_id",
    "required_skills",
    "education",
    "qualifications",
    "additional_sections",
}

# Fields that must always be empty in final output
_EMPTY_FIELDS = {
    "job_description": "",
    "description": "",
    "key_responsibilities": [],
    "about_company": "",
    "benefits": [],
}


async def extract_with_ai(text: str, known_fields: dict) -> dict:
    """Call the LLM to extract structured job data with minimal tokens.

    Uses content filtering (micro-RAG) to reduce token usage by sending
    only relevant job sections to the AI instead of full page text.

    Parameters
    ----------
    text : str
        Cleaned plain-text version of the job page.
    known_fields : dict
        Already-extracted values from deterministic parsers.
        Passed as hints so AI doesn't contradict confirmed data.

    Returns
    -------
    dict with reduced AI extraction schema plus ai_usage token info.
    """
    if not OPENAI_API_KEY:
        logger.warning("[AI] No OPENAI_API_KEY — skipping AI extraction")
        return _empty_result()

    # ── Content filtering (micro-RAG) ──────────────────────────────
    filtered = filter_content_for_ai(
        text,
        chunk_size=600,
        max_chunks=4,
        max_total_chars=3600,
    )

    # Build AI input from filtered content
    ai_input = _build_ai_input(filtered, known_fields)

    try:
        async with httpx.AsyncClient(timeout=90) as client:
            resp = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPENAI_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "gpt-4.1-nano",
                    "messages": [
                        {"role": "system", "content": _AI_SYSTEM_PROMPT},
                        {"role": "user", "content": ai_input},
                    ],
                    "temperature": 0,
                    "response_format": {"type": "json_object"},
                },
            )
            resp.raise_for_status()
            payload = resp.json()
            content = payload["choices"][0]["message"]["content"].strip()

        # Strip possible markdown fences
        if content.startswith("```"):
            content = content.strip("`").split("\n", 1)[-1].rsplit("```", 1)[0]

        result = json.loads(content)

        # ── Token tracking ─────────────────────────────────────
        ai_usage = _extract_token_usage(payload, ai_input)
        result["ai_usage"] = ai_usage
        result["content_filter_stats"] = {
            "original_text_length": filtered["original_text_length"],
            "filtered_text_length": filtered["filtered_text_length"],
            "number_of_chunks_selected": filtered["number_of_chunks_selected"],
        }
        logger.info(
            "[AI] Tokens used — input=%d, output=%d, total=%d (filtered from %d chars, %d chunks)",
            ai_usage["input_tokens"],
            ai_usage["output_tokens"],
            ai_usage["total_tokens"],
            filtered["original_text_length"],
            filtered["number_of_chunks_selected"],
        )
        # ──────────────────────────────────────────────────────

        # Enforce empty large fields
        result.update(_EMPTY_FIELDS)

        field_count = sum(1 for v in result.values() if v not in (None, "", []))
        logger.info("[AI] Extraction succeeded — fields filled: %d/%d", field_count, len(_AI_FIELDS))
        return result

    except Exception as exc:
        logger.error("[AI] Extraction failed: %s", exc)
        return _empty_result()


def _build_ai_input(filtered: dict, known_fields: dict) -> str:
    """Build optimized AI input from filtered content.

    Sends only relevant job metadata to minimize input tokens.
    """
    already_found = {k: v for k, v in known_fields.items() if v}
    hint = (
        f"Already confirmed (use these as-is, do NOT contradict):\n"
        f"{json.dumps(already_found, indent=2, default=str)}\n\n" if already_found else ""
    )

    # Build the filtered content section — only job-relevant parts
    content_parts = []

    if filtered.get("job_summary"):
        content_parts.append(f"JOB SUMMARY:\n{filtered['job_summary'][:1200]}")

    if filtered.get("relevant_chunks"):
        for i, chunk in enumerate(filtered["relevant_chunks"], 1):
            content_parts.append(f"SECTION {i}:\n{chunk}")

    combined_content = "\n\n---\n\n".join(content_parts) if content_parts else ""

    user_prompt = (
        f"{hint}"
        f"----------------------------------\n\n"
        f"INPUT (filtered to relevant job content):\n{combined_content}\n\n"
        f"----------------------------------\n\n"
        f"OUTPUT:"
    )

    return user_prompt


def _extract_token_usage(payload: dict, input_text: str) -> dict:
    """Extract or estimate token usage from OpenAI response."""
    try:
        usage = payload.get("usage", {})
        return {
            "input_tokens": int(usage.get("prompt_tokens", 0)),
            "output_tokens": int(usage.get("completion_tokens", 0)),
            "total_tokens": int(usage.get("total_tokens", 0)),
        }
    except (ValueError, TypeError):
        pass

    # Fallback estimation: tokens ≈ len(text) / 4
    estimated_input = len(input_text) // 4 if input_text else 0
    return {
        "input_tokens": estimated_input,
        "output_tokens": 0,
        "total_tokens": estimated_input,
    }


def estimate_tokens(text: str) -> int:
    """Rough token count: ~4 chars per token."""
    if not text:
        return 0
    return len(text) // 4


def _empty_result() -> dict:
    result = {
        "title": "",
        "company_name": "",
        "location": [],
        "experience": "",
        "employment_type": "",
        "salary": "",
        "posted_on": "",
        "remote_type": "",
        "job_id": "",
        "required_skills": [],
        "preferred_skills": [],
        "soft_skills": [],
        "inferred_skills": [],
        "qualifications": [],
        "education": "",
        "certifications": [],
        "tools_and_technologies": [],
        "benefits": [],
        "about_company": "",
        "job_description": "",
        "description": "",
        "key_responsibilities": [],
        "additional_sections": [],
        "ai_usage": {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
        },
    }
    return result
