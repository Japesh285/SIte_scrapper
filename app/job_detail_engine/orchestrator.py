"""Job Detail Engine — orchestrator.

Pipeline:
    1. JSON-LD parser  (deterministic, highest trust)
    2. HTML parser     (deterministic fallback)
    3. Confidence score
    4. AI extraction (always, as enrichment layer)
    5. Merge & return
"""

from app.job_detail_engine.parsers.json_ld import parse_json_ld
from app.job_detail_engine.parsers.accenture import parse_accenture_html
from app.job_detail_engine.parsers.html_basic import parse_html_basic
from app.job_detail_engine.scoring.confidence import evaluate_job, score
from app.job_detail_engine.ai.extractor import extract_with_ai, extract_with_ai_workday_full
from app.job_detail_engine.utils.cleaner import prepare_ai_payload
from app.job_detail_engine.utils.normalizer import normalize_job_data

from app.core.logger import logger


async def extract_job_details(html: str, force_ai: bool = False, site_type: str = "", domain: str = "") -> dict:
    """Extract structured job data from raw HTML.

    Parameters
    ----------
    html : str
        Raw HTML of the job page.
    force_ai : bool
        When True, AI is always called as an enrichment layer regardless
        of parser confidence.  Defaults to False (AI only on low score).
    site_type : str
        Site type classification (e.g. "WORKDAY_API", "GREENHOUSE_API").
        Used to enable special handling for specific site types.
    domain : str
        Site domain (e.g. "www.ibm.com"). Used for saving AI input payloads.

    Returns
    -------
    dict with keys:
        title, company_name, location, salary,
        experience, employment_type, posted_on, skills,
        ai_usage  (token usage from AI call)
        _meta     (debug info: parser_used, confidence, ai_used, field_sources)
    """
    # ── Step 1: JSON-LD ────────────────────────────────────────────
    result = parse_json_ld(html)
    parser_used = "json_ld"
    logger.info("[Engine] JSON-LD parser → title=%s", result.get("title"))

    # ── Step 2: If JSON-LD empty, try HTML parser ──────────────────
    eval_result = evaluate_job(result)
    conf = eval_result["confidence_score"]
    
    if conf == 0:
        accenture_result = parse_accenture_html(html)
        accenture_eval = evaluate_job(accenture_result)
        accenture_conf = accenture_eval["confidence_score"]
        
        if accenture_conf > 0:
            logger.info("[Engine] Accenture parser matched → title=%s, score=%d", accenture_result.get("title"), accenture_conf)
            result = accenture_result
            parser_used = "accenture_html"
            eval_result = accenture_eval
            conf = accenture_conf
        else:
            logger.info("[Engine] JSON-LD empty, falling back to HTML parser")
            result = parse_html_basic(html)
            parser_used = "html_basic"
            eval_result = evaluate_job(result)
            conf = eval_result["confidence_score"]
            logger.info("[Engine] HTML parser → title=%s, score=%d", result.get("title"), conf)

    _tag_provenance(result, parser_used)

    # ── Step 3: AI enrichment (always if force_ai, otherwise low-score fallback) ─
    ai_used = False
    ai_usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    
    # Trigger AI if forced OR if the base extraction isn't considered "passing"
    should_call_ai = force_ai or not eval_result["is_passing"]
    
    if should_call_ai:
        logger.info("[Engine] AI enrichment forced=%s, passing=%s — calling AI", force_ai, eval_result["is_passing"])

        # ── DOM / INTERACTIVE_DOM path: use prepare_ai_payload ──
        # ONLY removes script/style/noscript, preserves ALL content
        payload = prepare_ai_payload(html, domain=domain)
        logger.info("[AI PAYLOAD] length=%d source=JOB_DETAIL", len(payload))

        # Save the exact payload sent to AI
        if domain:
            from app.services.ai_payload_saver import save_ai_payload
            save_ai_payload(payload, domain)

        if site_type == "WORKDAY_API":
            if not callable(extract_with_ai_workday_full):
                raise RuntimeError("extract_with_ai_workday_full is not callable")
            ai_result = await extract_with_ai_workday_full(payload, result)
            logger.info("[WORKDAY AI] extract_with_ai_workday_full executed successfully")
        else:
            ai_result = await extract_with_ai(payload, result)

        if ai_result:
            job_id = result.get("job_id", "unknown")
            logger.info("[AI] Enrichment applied for job_id=%s", job_id)
            ai_usage = ai_result.pop("ai_usage", ai_usage)
            _tag_provenance(ai_result, "ai")
            result = _merge(result, ai_result)
            ai_used = True
            
        eval_result = evaluate_job(result)
        conf = eval_result["confidence_score"]
        logger.info("[Engine] After AI — score=%d, passing=%s", conf, eval_result["is_passing"])
    else:
        logger.info("[Engine] Skipping AI (score=%d, passing=%s, not forced)", conf, eval_result["is_passing"])

    # ── Step 4: Normalize & clean data ─────────────────────────────
    result = normalize_job_data(result)

    # ── Step 4b: Remove large text fields to reduce output size ────
    result = _strip_large_text_fields(result)

    # ── Step 5: Attach metadata + ai_usage (keep for saving) ───────
    if "_meta" not in result:
        result["_meta"] = {}
        
    result["_meta"].update({
        "parser_used": parser_used,
        "confidence": conf,
        "ai_used": ai_used,
        "ai_forced": force_ai,
        "structured_count": eval_result["structured_count"]
    })
    result["ai_usage"] = ai_usage

    logger.info(
        "[Engine] Done → parser=%s, score=%d, passing=%s, ai_used=%s, ai_forced=%s, "
        "ai_tokens=%d",
        parser_used, conf, eval_result["is_passing"], ai_used, force_ai, ai_usage.get("total_tokens", 0),
    )
    return result


def _tag_provenance(job: dict, source: str) -> None:
    """Tag fields with their origin parser."""
    if "_meta" not in job:
        job["_meta"] = {}
    if "field_sources" not in job["_meta"]:
        job["_meta"]["field_sources"] = {}
        
    for k, v in job.items():
        if k in ("_meta", "ai_usage"):
            continue
        if v and k not in job["_meta"]["field_sources"]:
            job["_meta"]["field_sources"][k] = source


def _strip_large_text_fields(result: dict) -> dict:
    """Remove large text fields from output to keep response lean.

    These fields are NOT needed downstream:
    - job_description / description (full text)
    - about_company (full text)
    - additional_sections (long paragraphs removed, only short metadata kept)
    """
    # Remove full text fields entirely
    result.pop("job_description", None)
    result.pop("description", None)
    result.pop("about_company", None)

    # Clean additional_sections: keep only short metadata items
    sections = result.get("additional_sections", [])
    if sections:
        filtered_sections = []
        for sec in sections:
            title = sec.get("section_title", "")
            content = sec.get("content", "")
            # Only keep short metadata (e.g. application deadline)
            if len(content) < 200 and len(title) < 100:
                filtered_sections.append({"section_title": title, "content": content})
        result["additional_sections"] = filtered_sections

    return result


def _merge(base: dict, delta: dict) -> dict:
    """Merge AI output into parser results.

    Strategy:
    - AI-enrichment fields always preferred (skills, qualifications, education, etc.)
    - Other fields only fill gaps (never overwrite truthy base values)
    - Provenance tags are also merged appropriately
    """
    # Fields where AI output takes priority (more complete extraction)
    ai_priority_fields = {
        "skills",
        "required_skills",
        "preferred_skills",
        "soft_skills",
        "inferred_skills",
        "experience",
        "qualifications",
        "education",
        "certifications",
        "tools_and_technologies",
        "additional_sections",
        "salary",
        "posted_on",
        "employment_type",
        "remote_type",
        "job_id",
    }
    
    if "_meta" not in base:
        base["_meta"] = {"field_sources": {}}
    if "field_sources" not in base["_meta"]:
        base["_meta"]["field_sources"] = {}
        
    delta_sources = delta.get("_meta", {}).get("field_sources", {})

    for key, value in delta.items():
        if key in ("_meta", "ai_usage"):
            continue
            
        should_override = key in ai_priority_fields and value
        should_fill = not base.get(key) and value
        
        if should_override or should_fill:
            base[key] = value
            if key in delta_sources:
                base["_meta"]["field_sources"][key] = delta_sources[key]

    return base
