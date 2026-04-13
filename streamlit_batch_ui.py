from __future__ import annotations

import time
from io import BytesIO

import pandas as pd
import requests
import streamlit as st


DEFAULT_BACKEND_BASE_URL = "http://localhost:8002"
COLUMN_NAME = "Exact India Jobs Link"
POLL_INTERVAL_SECONDS = 5


st.set_page_config(page_title="Scrape Details Batch UI", layout="centered")
st.title("Scrape Details Batch")
st.caption("Upload an Excel file, submit a batch scrape job, poll until it completes, then download the Excel result.")

backend_base_url = st.text_input("Backend Base URL", value=DEFAULT_BACKEND_BASE_URL).rstrip("/")
uploaded_file = st.file_uploader("Open Excel File", type=["xlsx", "xls"])

submit_endpoint = f"{backend_base_url}/scrape-details-batch/jobs"
status_endpoint_template = f"{backend_base_url}/scrape-details-batch/jobs/{{job_id}}"
download_endpoint_template = f"{backend_base_url}/scrape-details-batch/jobs/{{job_id}}/download"


def extract_urls_from_excel(file_bytes: bytes) -> list[str]:
    df = pd.read_excel(BytesIO(file_bytes))
    if COLUMN_NAME not in df.columns:
        raise ValueError(f"Column `{COLUMN_NAME}` not found in uploaded file.")

    urls = []
    for value in df[COLUMN_NAME].tolist():
        if pd.isna(value):
            continue
        url = str(value).strip()
        if url:
            urls.append(url)

    seen = set()
    deduped = []
    for url in urls:
        if url not in seen:
            seen.add(url)
            deduped.append(url)
    return deduped


def poll_job_status(job_id: str) -> dict:
    response = requests.get(status_endpoint_template.format(job_id=job_id), timeout=60)
    response.raise_for_status()
    return response.json()


if uploaded_file is not None:
    file_bytes = uploaded_file.getvalue()

    try:
        urls = extract_urls_from_excel(file_bytes)
    except Exception as exc:
        st.error(str(exc))
        st.stop()

    st.success(f"Found {len(urls)} URLs in `{COLUMN_NAME}`.")
    with st.expander("Preview URLs"):
        st.write(urls)

    if st.button("Submit Batch Job", type="primary", use_container_width=True):
        if not urls:
            st.warning("No URLs found to send.")
        else:
            try:
                response = requests.post(
                    submit_endpoint,
                    json={"urls": urls},
                    timeout=60,
                )
                response.raise_for_status()
                payload = response.json()
            except requests.RequestException as exc:
                st.error(f"Job submission failed: {exc}")
            else:
                st.session_state["batch_job_id"] = payload["job_id"]
                st.session_state["batch_status"] = payload.get("status", "queued")
                st.session_state.pop("result_file_bytes", None)
                st.session_state.pop("result_file_name", None)
                st.success(f"Submitted job `{payload['job_id']}`")

job_id = st.session_state.get("batch_job_id")
if job_id:
    st.info(f"Current job id: `{job_id}`")

    try:
        job_status = poll_job_status(job_id)
        st.session_state["batch_status"] = job_status.get("status", "unknown")
    except requests.RequestException as exc:
        st.error(f"Failed to fetch job status: {exc}")
        st.stop()

    st.write(
        {
            "status": job_status.get("status"),
            "total_sites": job_status.get("total_sites"),
            "successful": job_status.get("successful"),
            "failed": job_status.get("failed"),
            "skipped": job_status.get("skipped"),
            "error": job_status.get("error", ""),
        }
    )

    if job_status.get("status") in {"queued", "running"}:
        st.caption(f"Polling every {POLL_INTERVAL_SECONDS} seconds...")
        time.sleep(POLL_INTERVAL_SECONDS)
        st.rerun()

    if job_status.get("status") == "completed" and not st.session_state.get("result_file_bytes"):
        try:
            response = requests.get(
                download_endpoint_template.format(job_id=job_id),
                timeout=300,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            st.error(f"Failed to download result file: {exc}")
        else:
            st.session_state["result_file_bytes"] = response.content
            st.session_state["result_file_name"] = "master_jobs.xlsx"

    if job_status.get("status") == "failed":
        st.error(job_status.get("error") or "Batch job failed.")

if st.session_state.get("result_file_bytes"):
    st.download_button(
        label="Download Result Excel",
        data=st.session_state["result_file_bytes"],
        file_name=st.session_state.get("result_file_name", "master_jobs.xlsx"),
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True,
    )
