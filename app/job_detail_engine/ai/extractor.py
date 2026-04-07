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

# Workday-specific system prompt - more lenient for full context
_WORKDAY_SYSTEM_PROMPT = """\
You are a job metadata extraction system analyzing a FULL Workday job posting.

Extract ALL available information into these fields:

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
- Keep arrays max 15 items, each item max 50 chars
- Extract required_skills comprehensively from the FULL description
- additional_sections: only keep short metadata (content < 200 chars)
- Do NOT extract job_description, key_responsibilities, about_company, benefits
- Valid JSON only, no markdown, no explanations

The input contains the COMPLETE job posting with all sections.
Extract every skill, qualification, and requirement you can find.

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
    """Call the LLM to extract structured job data with FULL context.

    NO chunking, NO truncation, NO content filtering.
    Sends the complete cleaned text to the AI to maximize extraction quality.

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

    # ── NO content filtering — send full text ──────────────────────
    # If text is too small (< 500 chars), caller should send full HTML instead
    ai_input = _build_ai_input_full(text, known_fields)

    try:
        async with httpx.AsyncClient(timeout=120) as client:
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
            "original_text_length": len(text),
            "filtered_text_length": len(text),
            "number_of_chunks_selected": 0,
            "filtering_applied": False,
        }
        logger.info(
            "[AI] Tokens used — input=%d, output=%d, total=%d (full text, NO filtering, %d chars)",
            ai_usage["input_tokens"],
            ai_usage["output_tokens"],
            ai_usage["total_tokens"],
            len(text),
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


async def extract_with_ai_workday_full(text: str, known_fields: dict) -> dict:
    """Call the LLM with FULL context for Workday jobs (NO filtering).

    This is a TEST MODE to verify if aggressive filtering causes missing
    required_skill_set. Sends the complete cleaned description to AI.

    Parameters
    ----------
    text : str
        Full cleaned plain-text version of the Workday job page.
        NO chunking, NO truncation, NO filtering applied.
    known_fields : dict
        Already-extracted values from deterministic parsers.
        Passed as hints so AI doesn't contradict confirmed data.

    Returns
    -------
    dict with AI extraction schema plus ai_usage token info.
    """
    if not OPENAI_API_KEY:
        logger.warning("[AI] No OPENAI_API_KEY — skipping AI extraction")
        return _empty_result()

    # DEBUG logging
    print("[DEBUG] Workday FULL context mode enabled")
    print("[DEBUG] Description length:", len(text))
    logger.info("[DEBUG] Workday FULL context mode enabled")
    logger.info("[DEBUG] Workday full description length: %d chars", len(text))

    # Build AI input with FULL text (no filtering)
    ai_input = _build_ai_input_workday_full(text, known_fields)
    logger.info("[AI CALL] payload_length=%d", len(ai_input))

    # ── Call AI API ──
    try:
        async with httpx.AsyncClient(timeout=180) as client:  # Longer timeout for larger input
            resp = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPENAI_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "gpt-4.1-nano",
                    "messages": [
                        {"role": "system", "content": _WORKDAY_SYSTEM_PROMPT},
                        {"role": "user", "content": ai_input},
                    ],
                    "temperature": 0,
                    "response_format": {"type": "json_object"},
                },
            )
            resp.raise_for_status()
            payload = resp.json()
            content = payload["choices"][0]["message"]["content"].strip()
    except Exception as exc:
        logger.error("[AI CALL ERROR] %s", str(exc))
        return _empty_result()

    # ── Log raw AI response ──
    logger.info("[AI RAW RESPONSE] %s", str(content)[:1000])

    # ── Parse response ──
    try:
        # Strip possible markdown fences
        if content.startswith("```"):
            content = content.strip("`").split("\n", 1)[-1].rsplit("```", 1)[0]

        result = json.loads(content)
    except Exception as e:
        logger.error("[AI PARSE ERROR] %s", str(e))
        logger.error("[AI RESPONSE FULL] %s", content)
        return _empty_result()

    # ── Token tracking ─────────────────────────────────────
    try:
        ai_usage = _extract_token_usage(payload, ai_input)
    except Exception as exc:
        logger.error("[AI TOKEN USAGE ERROR] %s", str(exc))
        ai_usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}

    result["ai_usage"] = ai_usage
    result["workday_full_context_stats"] = {
        "original_text_length": len(text),
        "filtering_applied": False,
    }
    logger.info(
        "[AI] Workday FULL context — Tokens used: input=%d, output=%d, total=%d (from %d chars, NO filtering)",
        ai_usage["input_tokens"],
        ai_usage["output_tokens"],
        ai_usage["total_tokens"],
        len(text),
    )
    # ──────────────────────────────────────────────────────

    # Enforce empty large fields
    result.update(_EMPTY_FIELDS)

    field_count = sum(1 for v in result.values() if v not in (None, "", []))
    logger.info("[AI] Workday FULL context extraction — fields filled: %d/%d", field_count, len(_AI_FIELDS))
    logger.info("[AI USAGE FINAL] %s", ai_usage)
    return result


def _build_ai_input_full(full_text: str, known_fields: dict) -> str:
    """Build AI input with FULL text — NO filtering, NO chunking.

    Sends the complete cleaned job posting to maximize extraction quality.
    """
    already_found = {k: v for k, v in known_fields.items() if v}
    hint = (
        f"Already confirmed (use these as-is, do NOT contradict):\n"
        f"{json.dumps(already_found, indent=2, default=str)}\n\n" if already_found else ""
    )

    user_prompt = (
        f"{hint}"
        f"----------------------------------\n\n"
        f"INPUT (FULL job posting - NO FILTERING APPLIED):\n{full_text}\n\n"
        f"----------------------------------\n\n"
        f"OUTPUT:"
    )

    return user_prompt


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


def _build_ai_input_workday_full(full_text: str, known_fields: dict) -> str:
    """Build AI input with FULL description text for Workday jobs.

    NO filtering, NO chunking, NO truncation applied.
    Sends the complete job posting to maximize skill extraction.
    """
    already_found = {k: v for k, v in known_fields.items() if v}
    hint = (
        f"Already confirmed (use these as-is, do NOT contradict):\n"
        f"{json.dumps(already_found, indent=2, default=str)}\n\n" if already_found else ""
    )

    user_prompt = (
        f"{hint}"
        f"----------------------------------\n\n"
        f"INPUT (FULL Workday job posting - NO FILTERING APPLIED):\n{full_text}\n\n"
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
