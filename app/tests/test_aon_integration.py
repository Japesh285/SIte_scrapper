import asyncio

from aonjobs import normalize_job_record
from app.detectors import dynamic_api_detector


def test_normalize_job_record_for_dynamic_api():
    raw_job = {
        "req_id": "97195",
        "title": "Infrastructure Project Manager - M&A",
        "city": "London",
        "country": "United Kingdom",
        "employment_type": "Full time",
        "description": "Aon job description",
        "responsibilities": ["Lead delivery", "Coordinate teams"],
        "category": ["Mergers and Acquisitions Solutions"],
        "hiring_organization": {"name": "Aon"},
        "meta_data": {
            "googlejobs": {
                "canonical_url": "https://jobs.aon.com/jobs/97195?lang=en-us",
            }
        },
    }

    job = normalize_job_record(raw_job)

    assert job["title"] == "Infrastructure Project Manager - M&A"
    assert job["id"] == "97195"
    assert job["url"] == "https://jobs.aon.com/jobs/97195?lang=en-us"
    assert job["location"] == "London, United Kingdom"
    assert job["employment_type"] == "Full time"
    assert job["category"] == "Mergers and Acquisitions Solutions"
    assert job["company_name"] == "Aon"
    assert job["description_raw"] == "Aon job description"
    assert job["responsibilities"] == "Lead delivery\nCoordinate teams"
    assert job["_raw_api"] is raw_job


def test_detect_dynamic_api_short_circuits_for_aon(monkeypatch):
    sample_jobs = [
        {
            "title": "Sample Aon Job",
            "location": "London, United Kingdom",
            "id": "123",
            "url": "https://jobs.aon.com/jobs/123?lang=en-us",
        }
    ]

    monkeypatch.setattr(dynamic_api_detector, "scrape_for_dynamic_api", lambda: sample_jobs)

    result = asyncio.run(dynamic_api_detector.detect_dynamic_api("https://jobs.aon.com/jobs"))

    assert result["matched"] is True
    assert result["api_usable"] is True
    assert result["api_url"] == "https://jobs.aon.com/api/jobs"
    assert result["method"] == "GET"
    assert result["jobs_found"] == 1
    assert result["raw_jobs"] == sample_jobs
    assert result["score"] == 100
