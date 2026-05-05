"""Confidence scoring for extracted job data."""

from app.job_detail_engine.scoring.validators import (
    is_meaningful_string,
    is_meaningful_description,
    has_valid_list_items,
)


def evaluate_job(job: dict) -> dict:
    """Evaluate job data for confidence and structured completeness.
    
    Returns a dictionary with detailed assessment.
    """
    has_title = is_meaningful_string(job.get("title"))
    has_desc = is_meaningful_description(job.get("description"))
    
    core_ok = has_title and has_desc
    
    # Count valid structured fields
    structured_count = 0
    if is_meaningful_string(job.get("salary")):
        structured_count += 1
    if is_meaningful_string(job.get("experience")):
        structured_count += 1
    if is_meaningful_string(job.get("employment_type")):
        structured_count += 1
    if is_meaningful_string(job.get("location")):
        structured_count += 1
    if has_valid_list_items(job.get("skills")):
        structured_count += 1
        
    # Minimum 2 structured fields required to be "passing" without AI enrichment
    is_passing = core_ok and structured_count >= 2
    
    # Legacy score for backward compatibility (max ~8)
    # Give some points for structured fields to keep older threshold logic somewhat functional
    score_val = 0
    if has_title:
        score_val += 2
    if has_desc:
        score_val += 2
    score_val += structured_count
    
    return {
        "confidence_score": score_val,
        "structured_count": structured_count,
        "core_ok": core_ok,
        "is_passing": is_passing,
    }


def score(job: dict) -> int:
    """Legacy score function wrapper.
    
    Returns the integer confidence score. The orchestrator now uses evaluate_job 
    for more robust routing logic.
    """
    return evaluate_job(job)["confidence_score"]
