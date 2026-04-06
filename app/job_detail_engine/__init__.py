"""Job Detail Extraction Engine — modular, deterministic-first pipeline."""

from app.job_detail_engine.orchestrator import extract_job_details

__all__ = ["extract_job_details"]
