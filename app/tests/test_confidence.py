"""Tests for the job confidence scoring logic."""

from app.job_detail_engine.scoring.confidence import evaluate_job, score

def get_valid_desc():
    # Helper to return a meaningful description string
    return (
        "We are looking for a highly skilled software engineer to join our rapidly growing, fast paced team. "
        "You will be responsible for building cutting edge features, scaling our infrastructure, and "
        "collaborating with cross functional teams across the globe. We value strong communication, "
        "problem solving abilities, and a deep understanding of computer science fundamentals."
    )

def test_evaluate_garbage_html_blob():
    # Only title and a massive but garbage HTML description
    job = {
        "title": "Software Engineer",
        "description": "<br>" * 100 + "<p>&nbsp;</p>" * 20
    }
    eval_result = evaluate_job(job)
    assert eval_result["core_ok"] is False
    assert eval_result["is_passing"] is False
    assert eval_result["structured_count"] == 0

def test_evaluate_title_and_giant_blob_without_structured_data():
    # Valid title and valid description, but NO structured data
    job = {
        "title": "Data Scientist",
        "description": get_valid_desc()
    }
    eval_result = evaluate_job(job)
    assert eval_result["core_ok"] is True
    assert eval_result["is_passing"] is False
    assert eval_result["structured_count"] == 0

def test_evaluate_placeholder_values():
    job = {
        "title": "Data Scientist",
        "description": get_valid_desc(),
        "salary": "N/A",
        "location": "None",
        "experience": "not disclosed",
        "skills": [""]
    }
    eval_result = evaluate_job(job)
    assert eval_result["core_ok"] is True
    assert eval_result["is_passing"] is False
    assert eval_result["structured_count"] == 0

def test_evaluate_valid_high_quality_job():
    job = {
        "title": "Backend Developer",
        "description": get_valid_desc(),
        "salary": "$100k - $150k",
        "location": "New York, NY",
        "skills": ["Python", "Django", "PostgreSQL"],
    }
    eval_result = evaluate_job(job)
    assert eval_result["core_ok"] is True
    assert eval_result["is_passing"] is True
    assert eval_result["structured_count"] >= 2
    assert eval_result["confidence_score"] >= 4

def test_legacy_score_wrapper():
    job = {
        "title": "Backend Developer",
        "description": get_valid_desc(),
        "salary": "$100k - $150k",
        "location": "New York, NY",
    }
    # Has title (+2), desc (+2), 2 structured (+2) => 6
    s = score(job)
    assert s == 6
