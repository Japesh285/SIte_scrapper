import json
import time
from typing import Any

import requests

AON_API_URL = "https://jobs.aon.com/api/jobs"
AON_REFERER_URL = "https://jobs.aon.com/jobs"
URL = AON_API_URL

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json",
    "Referer": AON_REFERER_URL,
}

LIMIT: int | None = None
REQUEST_DELAY_SECONDS = 2.0
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 5.0


def is_aon_url(url: str) -> bool:
    return "aon" in (url or "").lower()


def _compute_retry_wait(response: requests.Response | None, attempt: int) -> float:
    if response is not None:
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return max(float(retry_after), RETRY_BACKOFF_SECONDS * attempt)
            except ValueError:
                pass
    return RETRY_BACKOFF_SECONDS * attempt


def fetch_page(
    page: int,
    session: requests.Session | None = None,
    timeout: float = 30.0,
    max_retries: int = MAX_RETRIES,
) -> dict | None:
    params = {
        "page": page,
        "sortBy": "relevance",
        "descending": "false",
        "internal": "false",
    }

    requester = session or requests
    for attempt in range(1, max_retries + 1):
        try:
            res = requester.get(AON_API_URL, headers=HEADERS, params=params, timeout=timeout)

            if res.status_code == 200:
                return res.json()

            wait_seconds = _compute_retry_wait(res, attempt)
            print(
                f"Failed at page {page} with status {res.status_code} "
                f"(attempt {attempt}/{max_retries})"
            )
            if attempt < max_retries:
                print(f"Waiting {wait_seconds:.1f}s before retrying page {page}")
                time.sleep(wait_seconds)
        except requests.RequestException as exc:
            wait_seconds = RETRY_BACKOFF_SECONDS * attempt
            print(f"Request error at page {page} (attempt {attempt}/{max_retries}): {exc}")
            if attempt < max_retries:
                print(f"Waiting {wait_seconds:.1f}s before retrying page {page}")
                time.sleep(wait_seconds)

    return None


def _first_non_empty(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _stringify(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts = [item.strip() for item in value if isinstance(item, str) and item.strip()]
        return "\n".join(parts)
    return ""


def _find_nested_value(obj: Any, key: str) -> str:
    if isinstance(obj, dict):
        value = obj.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        for nested in obj.values():
            found = _find_nested_value(nested, key)
            if found:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _find_nested_value(item, key)
            if found:
                return found
    return ""


def _extract_category(job_data: dict) -> str:
    category = job_data.get("category")
    if isinstance(category, list):
        for item in category:
            if isinstance(item, str) and item.strip():
                return item.strip()
    if isinstance(category, str) and category.strip():
        return category.strip()

    categories = job_data.get("categories")
    if isinstance(categories, list):
        for item in categories:
            if isinstance(item, dict):
                name = item.get("name")
                if isinstance(name, str) and name.strip():
                    return name.strip()
            elif isinstance(item, str) and item.strip():
                return item.strip()
    return ""


def _extract_company_name(job_data: dict) -> str:
    hiring_org = job_data.get("hiring_organization")
    if isinstance(hiring_org, dict):
        return _first_non_empty(
            hiring_org.get("name"),
            hiring_org.get("title"),
            hiring_org.get("company_name"),
        ) or "Aon"
    if isinstance(hiring_org, str) and hiring_org.strip():
        return hiring_org.strip()
    return "Aon"


def _build_job_url(job_data: dict) -> str:
    canonical_url = _find_nested_value(job_data, "canonical_url")
    if canonical_url:
        return canonical_url

    req_id = str(job_data.get("req_id") or job_data.get("id") or "").strip()
    if req_id:
        return f"{AON_REFERER_URL}/{req_id}?lang=en-us"

    return _first_non_empty(job_data.get("apply_url"), AON_REFERER_URL)


def normalize_job_record(job_data: dict) -> dict:
    city = _first_non_empty(job_data.get("city"), job_data.get("state"))
    country = _first_non_empty(job_data.get("country"), job_data.get("country_code"))
    location = _first_non_empty(
        job_data.get("full_location"),
        job_data.get("location_name"),
        job_data.get("short_location"),
        ", ".join(part for part in [city, country] if part),
    )

    return {
        "title": _first_non_empty(job_data.get("title")),
        "location": location,
        "id": str(job_data.get("req_id") or job_data.get("id") or "").strip(),
        "url": _build_job_url(job_data),
        "posted_date": _first_non_empty(
            job_data.get("posted_date"),
            job_data.get("create_date"),
            job_data.get("update_date"),
        ),
        "description_raw": _stringify(job_data.get("description")),
        "requirements_raw": _stringify(job_data.get("responsibilities")),
        "responsibilities": _stringify(job_data.get("responsibilities")),
        "employment_type": _first_non_empty(job_data.get("employment_type")),
        "category": _extract_category(job_data),
        "company_name": _extract_company_name(job_data),
        "_raw_api": job_data,
    }


def scrape_jobs(
    limit: int | None = None,
    request_delay: float = REQUEST_DELAY_SECONDS,
    normalize: bool = False,
    session: requests.Session | None = None,
) -> list[dict]:
    all_data: list[dict] = []
    page = 1

    while limit is None or len(all_data) < limit:
        print(f"Fetching page {page}")
        data = fetch_page(page, session=session)

        if not data or not data.get("jobs"):
            break

        for job in data["jobs"]:
            raw_job = job.get("data") if isinstance(job, dict) else None
            if not isinstance(raw_job, dict):
                continue

            all_data.append(normalize_job_record(raw_job) if normalize else raw_job)

            if limit is not None and len(all_data) >= limit:
                break

        page += 1
        if request_delay > 0:
            time.sleep(request_delay)

    return all_data


def scrape_limited(limit: int | None = LIMIT) -> list[dict]:
    return scrape_jobs(limit=limit, normalize=False)


def scrape_for_dynamic_api(limit: int | None = None) -> list[dict]:
    return scrape_jobs(limit=limit, normalize=True)


if __name__ == "__main__":
    jobs_data = scrape_limited()

    print(f"Total jobs collected: {len(jobs_data)}")

    with open("aon_raw_jobs.json", "w") as f:
        json.dump(jobs_data, f, indent=2)
