# Remaining ATS Fixes Implementation Plan (Batch 2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix five independent issues: timeouts on slow sites, Workday JS-redirect embed detection, Greenhouse subpage embed resolution, SmartRecruiters API support, and SAP SuccessFactors browser-intercepted API support.

**Architecture:** Tasks 1–3 are one-to-two line fixes to existing files. Tasks 4–5 each add two new files (detector + scraper) and wire them into the orchestrator and classifier using the same pattern as Workday/Greenhouse/Phenom. All tasks are independent — they can be done in any order or in parallel.

**Tech Stack:** Python 3.11+, httpx, Playwright (`playwright.async_api`), FastAPI orchestrator

---

## File Map

| File | Action | Task |
|---|---|---|
| `app/services/orchestrator.py` | Modify (timeout, Workday embed fix, SR step, SAP_SF step) | 1, 2, 4, 5 |
| `app/detectors/greenhouse.py` | Modify (subpath fallback) | 3 |
| `app/detectors/smartrecruiters.py` | Create | 4 |
| `app/scrapers/smartrecruiters.py` | Create | 4 |
| `app/detectors/sap_successfactors.py` | Create | 5 |
| `app/scrapers/sap_successfactors.py` | Create | 5 |
| `app/detectors/__init__.py` | Modify (export new detectors) | 4, 5 |
| `app/services/ai_classifier.py` | Modify (add SR, SAP_SF) | 4, 5 |

---

## Task 1: Fix timeouts on slow sites

**Files:**
- Modify: `app/services/orchestrator.py`

- [ ] **Step 1: Find the httpx client creation in the orchestrator**

Search for `httpx.AsyncClient(timeout=` in `app/services/orchestrator.py`. There will be one or more occurrences where the shared client is created for the detection run. Find the main one used across the detection pipeline.

- [ ] **Step 2: Replace the timeout**

Change every occurrence of:
```python
httpx.AsyncClient(timeout=20, follow_redirects=True)
```
To:
```python
httpx.AsyncClient(
    timeout=httpx.Timeout(connect=10, read=90, write=10, pool=10),
    follow_redirects=True,
)
```

This keeps connection establishment at 10s (fast fail for unreachable hosts) while allowing slow-responding servers up to 90s.

- [ ] **Step 3: Verify change compiles**

```bash
cd /home/japesh/Dev/scrapper/SIte_scrapper
python -c "from app.services.orchestrator import orchestrate_scrape; print('OK')"
```
Expected: `OK`

- [ ] **Step 4: Smoke-test on a previously timing-out site**

```bash
python -c "
import asyncio, time
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from app.core.config import DATABASE_URL
from app.services.orchestrator import orchestrate_scrape

engine = create_async_engine(DATABASE_URL)
Session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

async def run():
    t = time.time()
    async with Session() as session:
        result = await orchestrate_scrape('https://careers.irco.com', session)
    print(f'Completed in {time.time()-t:.1f}s: {result}')

asyncio.run(run())
"
```
Expected: completes without `ReadTimeout` error. Job count may still be 0 if the site is a custom unknown ATS — that's acceptable. The goal is no timeout crash.

- [ ] **Step 5: Commit**

```bash
git add app/services/orchestrator.py
git commit -m "fix: increase httpx read timeout to 90s for slow career sites"
```

---

## Task 2: Fix Workday JS-redirect embed detection

**Files:**
- Modify: `app/services/orchestrator.py`

- [ ] **Step 1: Find the browser probe block in the orchestrator**

Locate this section (around line 353–358):
```python
            discovered_urls = sorted(
                {
                    *browser_probe.get("json_urls", []),
                    *browser_probe.get("request_urls", []),
                }
            )
            browser_final_url = browser_probe.get("final_url", "")
```

- [ ] **Step 2: Add the Workday final_url fix**

Immediately after `browser_final_url = browser_probe.get("final_url", "")`, add:

```python
            # If the page JS-redirected to a Workday subdomain, surface that URL
            # so the Workday detector can construct the API endpoint from it.
            if browser_final_url and "myworkdayjobs" in browser_final_url.lower():
                discovered_urls = sorted({*discovered_urls, browser_final_url})
```

- [ ] **Step 3: Verify change compiles**

```bash
python -c "from app.services.orchestrator import orchestrate_scrape; print('OK')"
```
Expected: `OK`

- [ ] **Step 4: Smoke-test**

```bash
python -c "
import asyncio
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from app.core.config import DATABASE_URL
from app.services.orchestrator import orchestrate_scrape

engine = create_async_engine(DATABASE_URL)
Session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

async def run():
    for url in ['https://careers.lilly.com', 'https://www.novartis.com/careers']:
        async with Session() as session:
            result = await orchestrate_scrape(url, session)
            print(f'{url}: type={result[\"type\"]} jobs={result[\"jobs_found\"]}')

asyncio.run(run())
"
```
Expected: `type=WORKDAY_API`, `jobs_found > 0` for sites that redirect to Workday.

- [ ] **Step 5: Commit**

```bash
git add app/services/orchestrator.py
git commit -m "fix: add Workday myworkdayjobs redirect URL to discovered_urls in browser probe"
```

---

## Task 3: Fix Greenhouse embed — careers subpath fallback

**Files:**
- Modify: `app/detectors/greenhouse.py`

- [ ] **Step 1: Add the subpath fallback to `resolve_greenhouse_slug`**

Open `app/detectors/greenhouse.py`. Find the `resolve_greenhouse_slug` function. At the very end of the function, just before the final `return {"slug": "", "source": "not_found", "board_url": ""}`, add:

```python
    # Last resort: try common careers subpaths — handles Greenhouse boards embedded
    # on /careers, /jobs, etc. rather than the homepage.
    _SUBPATHS = ("/careers", "/jobs", "/company/careers", "/about/careers")
    parsed_origin = f"{parsed.scheme}://{parsed.netloc}"
    for subpath in _SUBPATHS:
        subpath_url = f"{parsed_origin}{subpath}"
        if subpath_url == normalized_url:
            continue  # already fetched above
        try:
            sub_resp = await client.get(subpath_url)
            if sub_resp.status_code != 200:
                continue
            match = GREENHOUSE_BOARD_PATTERN.search(sub_resp.text)
            if match:
                return {
                    "slug": match.group(1),
                    "source": "careers_subpath_html",
                    "board_url": match.group(0),
                }
            query_match = GREENHOUSE_QUERY_PATTERN.search(sub_resp.text)
            if query_match:
                return {
                    "slug": query_match.group(1),
                    "source": "careers_subpath_query_param",
                    "board_url": subpath_url,
                }
        except Exception:
            continue
```

The final `return {"slug": "", "source": "not_found", "board_url": ""}` stays after this block.

- [ ] **Step 2: Verify change compiles**

```bash
python -c "from app.detectors.greenhouse import detect_greenhouse; print('OK')"
```
Expected: `OK`

- [ ] **Step 3: Smoke-test**

```bash
python -c "
import asyncio
import httpx
from app.detectors.greenhouse import detect_greenhouse

async def run():
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        for url in ['https://www.okta.com', 'https://www.mongodb.com']:
            result = await detect_greenhouse(url, client=client)
            print(f'{url}: matched={result[\"matched\"]} jobs={result[\"jobs_found\"]} slug={result[\"slug\"]} source={result[\"source\"]}')

asyncio.run(run())
"
```
Expected: `matched=True`, non-empty `slug`, `jobs_found > 0` for both.

- [ ] **Step 4: Commit**

```bash
git add app/detectors/greenhouse.py
git commit -m "fix: add careers subpath fallback to Greenhouse slug resolver"
```

---

## Task 4: Add SmartRecruiters detector and scraper

**Files:**
- Create: `app/detectors/smartrecruiters.py`
- Create: `app/scrapers/smartrecruiters.py`
- Modify: `app/detectors/__init__.py`
- Modify: `app/services/ai_classifier.py`
- Modify: `app/services/orchestrator.py`

### 4a — Detector

- [ ] **Step 1: Write `app/detectors/smartrecruiters.py`**

```python
# app/detectors/smartrecruiters.py
import re

import httpx

from app.core.logger import logger
from app.core.site_utils import normalize_site_url

_SR_HOST_RE = re.compile(
    r"(?:careers|jobs)\.smartrecruiters\.com/([a-zA-Z0-9_-]+)",
    re.IGNORECASE,
)
_SR_HTML_RE = re.compile(
    r"smartrecruiters\.com/([a-zA-Z0-9_-]+)",
    re.IGNORECASE,
)
_SR_API = "https://api.smartrecruiters.com/v1/companies/{slug}/postings"


async def detect_smartrecruiters(
    url: str,
    client: httpx.AsyncClient | None = None,
    html: str | None = None,
    discovered_urls: list[str] | None = None,
) -> dict:
    slug = _extract_slug(url, html or "", discovered_urls or [])
    if not slug:
        return _not_matched("no_company_slug")

    api_url = _SR_API.format(slug=slug)
    if client is None:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10, read=30, write=10, pool=10),
            follow_redirects=True,
        ) as c:
            return await _probe_api(c, api_url, slug)
    return await _probe_api(client, api_url, slug)


def _extract_slug(url: str, html: str, discovered_urls: list[str]) -> str:
    # 1. From URL path: careers.smartrecruiters.com/{slug}
    m = _SR_HOST_RE.search(url)
    if m and m.group(1).lower() not in ("", "home", "login", "search"):
        return m.group(1)

    # 2. From HTML
    m = _SR_HTML_RE.search(html)
    if m and m.group(1).lower() not in ("", "home", "login", "search"):
        return m.group(1)

    # 3. From discovered_urls (browser probe)
    for du in discovered_urls:
        m = _SR_HOST_RE.search(du)
        if m and m.group(1).lower() not in ("", "home", "login", "search"):
            return m.group(1)

    return ""


async def _probe_api(client: httpx.AsyncClient, api_url: str, slug: str) -> dict:
    try:
        resp = await client.get(api_url, params={"limit": 10, "offset": 0})
        if resp.status_code == 200 and "json" in resp.headers.get("content-type", ""):
            data = resp.json()
            count = len(data.get("content", data if isinstance(data, list) else []))
            total = data.get("totalFound", count) if isinstance(data, dict) else count
            if count > 0:
                logger.info("[SmartRecruiters] slug=%s total=%d", slug, total)
                return {
                    "matched": True,
                    "api_url": api_url,
                    "jobs_found": total,
                    "api_usable": True,
                    "slug": slug,
                    "confidence": 0.90,
                }
    except Exception as exc:
        logger.debug("[SmartRecruiters] API probe failed: %s", exc)

    return _not_matched("api_returned_no_jobs")


def _not_matched(reason: str = "") -> dict:
    return {
        "matched": False,
        "api_url": "",
        "jobs_found": 0,
        "api_usable": False,
        "slug": "",
        "confidence": 0.0,
    }
```

### 4b — Scraper

- [ ] **Step 2: Write `app/scrapers/smartrecruiters.py`**

```python
# app/scrapers/smartrecruiters.py
import httpx

from app.core.logger import logger
from app.core.site_utils import normalize_site_url

_PAGE_SIZE = 100
_MAX_JOBS = 10_000


async def scrape_smartrecruiters(
    url: str,
    client: httpx.AsyncClient | None = None,
    api_url: str = "",
) -> list[dict]:
    if not api_url:
        from app.detectors.smartrecruiters import detect_smartrecruiters
        result = await detect_smartrecruiters(url, client=client)
        api_url = result.get("api_url", "")
    if not api_url:
        return []

    base_url = normalize_site_url(url) or url
    if client is None:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10, read=90, write=10, pool=10),
            follow_redirects=True,
        ) as c:
            return await _paginate(c, api_url, base_url)
    return await _paginate(client, api_url, base_url)


async def _paginate(client: httpx.AsyncClient, api_url: str, base_url: str) -> list[dict]:
    all_jobs: list[dict] = []
    seen: set[str] = set()

    def add_batch(jobs: list[dict]) -> int:
        added = 0
        for j in jobs:
            key = j.get("url") or f"{j.get('title', '')}|{j.get('location', '')}"
            if key not in seen:
                seen.add(key)
                all_jobs.append(j)
                added += 1
        return added

    offset = 0
    while offset < _MAX_JOBS:
        try:
            resp = await client.get(api_url, params={"limit": _PAGE_SIZE, "offset": offset})
            if resp.status_code != 200 or "json" not in resp.headers.get("content-type", ""):
                break
            data = resp.json()
        except Exception as exc:
            logger.warning("[SmartRecruiters] offset=%d failed: %s", offset, exc)
            break

        raw_items = data.get("content", data if isinstance(data, list) else [])
        jobs = _extract_jobs(raw_items, base_url)
        if add_batch(jobs) == 0:
            break

        total = data.get("totalFound", 0) if isinstance(data, dict) else 0
        offset += _PAGE_SIZE
        if total and offset >= total:
            break

    logger.info("[SmartRecruiters] total extracted: %d", len(all_jobs))
    return all_jobs


def _extract_jobs(items: list, base_url: str) -> list[dict]:
    jobs = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = item.get("name") or item.get("title", "")
        loc = item.get("location", {})
        location = (
            loc.get("city") or loc.get("country") or loc.get("region") or ""
            if isinstance(loc, dict) else str(loc)
        )
        apply_url = item.get("applyUrl") or item.get("apply_url") or item.get("url") or ""
        if isinstance(title, str) and title.strip():
            jobs.append({
                "title": title.strip(),
                "location": location.strip(),
                "url": apply_url.strip(),
            })
    return jobs
```

### 4c — Register in orchestrator and classifier

- [ ] **Step 3: Export from `app/detectors/__init__.py`**

Add:
```python
from app.detectors.smartrecruiters import detect_smartrecruiters
```

Add `"detect_smartrecruiters"` to `__all__`.

- [ ] **Step 4: Update `app/services/ai_classifier.py`**

**a)** Add to `ALLOWED_TYPES`:
```python
"SMARTRECRUITERS",
```

**b)** Add to `SYSTEM_PROMPT` priority list (after WP_JOBS, before INTERACTIVE_DOM):
```
... > WP_JOBS > SMARTRECRUITERS > INTERACTIVE_DOM > ...
```

**c)** Add to `ranked_tests` in `_heuristic_classify` (after `WP_JOBS`):
```python
("SMARTRECRUITERS", tests.get("smartrecruiters", {})),
```

**d)** Add to `priority` dict:
```python
"SMARTRECRUITERS": 3,
```

- [ ] **Step 5: Update `app/services/orchestrator.py`**

**a)** Add imports:
```python
from app.scrapers.smartrecruiters import scrape_smartrecruiters
from app.detectors.smartrecruiters import detect_smartrecruiters
```

**b)** Add detection step 4e (after WP_JOBS step 4d):
```python
        # 4e. SmartRecruiters (slug from URL + public API probe)
        smartrecruiters_result = await detect_smartrecruiters(
            normalized_url, client=client, html=page_html
        )
        logger.info(
            "SmartRecruiters -> matched=%s jobs=%s slug=%s",
            smartrecruiters_result.get("matched"),
            smartrecruiters_result.get("jobs_found"),
            smartrecruiters_result.get("slug"),
        )
```

**c)** Add `smartrecruiters_result,` to the browser probe skip condition tuple.

**d)** Add to browser-assisted re-run block:
```python
            smartrecruiters_result = await detect_smartrecruiters(
                normalized_url, client=client, html=page_html, discovered_urls=discovered_urls
            )
```

**e)** Add to AI classifier payload under `"tests"`:
```python
            "smartrecruiters": smartrecruiters_result,
```

**f)** Add pipeline dispatch branch (before the final `else:` block):
```python
        elif site_type == "SMARTRECRUITERS":
            jobs = await scrape_smartrecruiters(
                normalized_url,
                client=client,
                api_url=smartrecruiters_result.get("api_url", ""),
            )
            api_url = ""
            final_site_type = "SMARTRECRUITERS"
            final_strategy = "api"
            logger.info("[PIPELINE] SMARTRECRUITERS → %d jobs", len(jobs))
```

- [ ] **Step 6: Smoke-test**

```bash
python -c "
import asyncio
import httpx
from app.detectors.smartrecruiters import detect_smartrecruiters
from app.scrapers.smartrecruiters import scrape_smartrecruiters

async def run():
    # Test with a known SR company slug URL
    url = 'https://careers.smartrecruiters.com/Booking'
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        result = await detect_smartrecruiters(url, client=client)
        print('Detect:', result)
    jobs = await scrape_smartrecruiters(url)
    print(f'Scrape: {len(jobs)} jobs')
    if jobs:
        print('Sample:', jobs[0])

asyncio.run(run())
"
```
Expected: `matched=True`, `jobs_found > 0`, list of job dicts.

- [ ] **Step 7: Commit**

```bash
git add app/detectors/smartrecruiters.py app/scrapers/smartrecruiters.py \
        app/detectors/__init__.py app/services/ai_classifier.py app/services/orchestrator.py
git commit -m "feat: add SmartRecruiters detector and scraper with public postings API"
```

---

## Task 5: Add SAP SuccessFactors detector and scraper

**Files:**
- Create: `app/detectors/sap_successfactors.py`
- Create: `app/scrapers/sap_successfactors.py`
- Modify: `app/detectors/__init__.py`
- Modify: `app/services/ai_classifier.py`
- Modify: `app/services/orchestrator.py`

### 5a — Detector

- [ ] **Step 1: Write `app/detectors/sap_successfactors.py`**

```python
# app/detectors/sap_successfactors.py
import httpx

from app.core.logger import logger
from app.core.site_utils import normalize_site_url

_SAP_SIGNALS = ("successfactors", "sfsf", "jobs.sap.com", "sapjobs")
_SAP_API_DOMAINS = ("successfactors", "sap.com", "sapjobs")


async def detect_sap_sf(
    url: str,
    client: httpx.AsyncClient | None = None,
    discovered_urls: list[str] | None = None,
) -> dict:
    normalized = normalize_site_url(url)
    if not normalized:
        return _not_matched()

    html = await _fetch_html(normalized, client)
    if not _has_sap_signals(html, discovered_urls or []):
        return _not_matched()

    logger.info("[SAP_SF] signals found for %s — launching browser interception", normalized)
    return await _intercept_sap_api(normalized)


def _has_sap_signals(html: str, discovered_urls: list[str]) -> bool:
    lowered = html.lower()
    if any(s in lowered for s in _SAP_SIGNALS):
        return True
    return any(s in u.lower() for s in _SAP_SIGNALS for u in discovered_urls)


async def _fetch_html(url: str, client: httpx.AsyncClient | None) -> str:
    try:
        if client:
            resp = await client.get(url)
            return resp.text if resp.status_code == 200 else ""
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10, read=30, write=10, pool=10),
            follow_redirects=True,
        ) as c:
            resp = await c.get(url)
            return resp.text if resp.status_code == 200 else ""
    except Exception:
        return ""


async def _intercept_sap_api(url: str) -> dict:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.warning("[SAP_SF] Playwright not installed")
        return _not_matched()

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
            )
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1920, "height": 1080},
                ignore_https_errors=True,
            )
            page = await context.new_page()

            captured: dict | None = None

            async def on_response(response):
                nonlocal captured
                if captured:
                    return
                resp_url = response.url.lower()
                if not any(d in resp_url for d in _SAP_API_DOMAINS):
                    return
                content_type = response.headers.get("content-type", "")
                if "json" not in content_type:
                    return
                try:
                    body = await response.json()
                    items = _find_job_items(body)
                    if len(items) >= 3:
                        captured = {"url": response.url, "count": len(items)}
                        logger.info("[SAP_SF] captured API: %s (%d jobs)", response.url, len(items))
                except Exception:
                    pass

            page.on("response", on_response)

            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=25000)
                await page.wait_for_timeout(8000)
            except Exception as exc:
                logger.debug("[SAP_SF] navigation error (non-fatal): %s", exc)

            await browser.close()

            if captured:
                return {
                    "matched": True,
                    "api_url": captured["url"],
                    "jobs_found": captured["count"],
                    "api_usable": True,
                    "confidence": 0.80,
                }
            logger.info("[SAP_SF] no job-list API intercepted for %s", url)
            return _not_matched()

    except Exception as exc:
        logger.warning("[SAP_SF] browser interception failed: %s", exc)
        return _not_matched()


def _find_job_items(body) -> list:
    if isinstance(body, list):
        items = [x for x in body if isinstance(x, dict) and "title" in x]
        if len(items) >= 3:
            return items
    if isinstance(body, dict):
        for value in body.values():
            if isinstance(value, list):
                items = [x for x in value if isinstance(x, dict) and "title" in x]
                if len(items) >= 3:
                    return items
    return []


def _not_matched() -> dict:
    return {
        "matched": False,
        "api_url": "",
        "jobs_found": 0,
        "api_usable": False,
        "confidence": 0.0,
    }
```

### 5b — Scraper

- [ ] **Step 2: Write `app/scrapers/sap_successfactors.py`**

```python
# app/scrapers/sap_successfactors.py
from urllib.parse import urlparse, urlencode, parse_qs, urlunparse

import httpx

from app.core.logger import logger
from app.core.site_utils import normalize_site_url
from app.detectors.simple_api import _extract_jobs_from_items

_PAGE_SIZE = 100
_MAX_JOBS = 10_000
_TOTAL_KEYS = ("totalCount", "total_count", "totalItems", "total", "count", "recordCount")
_LIST_KEYS = ("jobs", "positions", "results", "data", "items", "content", "requisitionList")


async def scrape_sap_sf(
    url: str,
    client: httpx.AsyncClient | None = None,
    api_url: str = "",
) -> list[dict]:
    if not api_url:
        from app.detectors.sap_successfactors import detect_sap_sf
        result = await detect_sap_sf(url, client=client)
        api_url = result.get("api_url", "")
    if not api_url:
        return []

    base_url = normalize_site_url(url) or url
    if client is None:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10, read=90, write=10, pool=10),
            follow_redirects=True,
        ) as c:
            return await _paginate(c, api_url, base_url)
    return await _paginate(client, api_url, base_url)


async def _paginate(client: httpx.AsyncClient, api_url: str, base_url: str) -> list[dict]:
    base = _strip_pagination_params(api_url)
    all_jobs: list[dict] = []
    seen: set[str] = set()

    def add_batch(jobs: list[dict]) -> int:
        added = 0
        for j in jobs:
            key = j.get("url") or f"{j.get('title', '')}|{j.get('location', '')}"
            if key not in seen:
                seen.add(key)
                all_jobs.append(j)
                added += 1
        return added

    # Try offset-based pagination first
    try:
        resp = await client.get(base, params={"limit": _PAGE_SIZE, "offset": 0})
        if resp.status_code != 200 or "json" not in resp.headers.get("content-type", ""):
            resp = await client.get(base)
        if resp.status_code != 200:
            return []
        first_data = resp.json()
    except Exception as exc:
        logger.warning("[SAP_SF] first page failed: %s", exc)
        return []

    first_items = _get_items(first_data)
    first_jobs = _extract_jobs_from_items(first_items, base_url)
    add_batch(first_jobs)

    total = _get_total(first_data)
    per_page = max(len(first_jobs), 1)
    if total <= per_page:
        return all_jobs

    pages_needed = min((total + per_page - 1) // per_page, _MAX_JOBS // _PAGE_SIZE)
    logger.info("[SAP_SF] paginating offset-based: total=%d pages=%d", total, pages_needed)

    for page_num in range(1, pages_needed):
        offset = page_num * _PAGE_SIZE
        try:
            resp = await client.get(base, params={"limit": _PAGE_SIZE, "offset": offset})
            if resp.status_code != 200 or "json" not in resp.headers.get("content-type", ""):
                break
            data = resp.json()
        except Exception as exc:
            logger.warning("[SAP_SF] offset=%d failed: %s", offset, exc)
            break
        items = _get_items(data)
        jobs = _extract_jobs_from_items(items, base_url)
        if add_batch(jobs) == 0:
            break

    return all_jobs


def _strip_pagination_params(api_url: str) -> str:
    parsed = urlparse(api_url)
    qs = {
        k: v for k, v in parse_qs(parsed.query, keep_blank_values=True).items()
        if k.lower() not in ("offset", "limit", "page", "start", "from", "skip")
    }
    return urlunparse(parsed._replace(query=urlencode(qs, doseq=True)))


def _get_items(data) -> list:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in _LIST_KEYS:
            val = data.get(key)
            if isinstance(val, list):
                return val
    return []


def _get_total(data) -> int:
    if isinstance(data, dict):
        for key in _TOTAL_KEYS:
            val = data.get(key)
            if isinstance(val, int) and val > 0:
                return val
    return 0
```

### 5c — Register

- [ ] **Step 3: Export from `app/detectors/__init__.py`**

Add:
```python
from app.detectors.sap_successfactors import detect_sap_sf
```

Add `"detect_sap_sf"` to `__all__`.

- [ ] **Step 4: Update `app/services/ai_classifier.py`**

**a)** Add to `ALLOWED_TYPES`:
```python
"SAP_SF",
```

**b)** Add to `SYSTEM_PROMPT` priority list (after `ICIMS_API`, before `PHENOM`):
```
... > ICIMS_API > SAP_SF > PHENOM > DYNAMIC_API > ...
```

**c)** Add to `ranked_tests` in `_heuristic_classify` (after `ICIMS_API`):
```python
("SAP_SF", tests.get("sap_sf", {})),
```

**d)** Add to `priority` dict:
```python
"SAP_SF": 6,
```

- [ ] **Step 5: Update `app/services/orchestrator.py`**

**a)** Add imports:
```python
from app.scrapers.sap_successfactors import scrape_sap_sf
from app.detectors.sap_successfactors import detect_sap_sf
```

**b)** Add detection step 4f (after SmartRecruiters step 4e):
```python
        # 4f. SAP SuccessFactors (static signal + browser API interception)
        sap_sf_result = await detect_sap_sf(normalized_url, client=client)
        logger.info(
            "SAP_SF -> matched=%s jobs=%s usable=%s",
            sap_sf_result.get("matched"),
            sap_sf_result.get("jobs_found"),
            sap_sf_result.get("api_usable"),
        )
```

**c)** Add `sap_sf_result,` to browser probe skip condition tuple.

**d)** Add to browser-assisted re-run block:
```python
            sap_sf_result = await detect_sap_sf(
                normalized_url,
                client=client,
                discovered_urls=discovered_urls,
            )
```

**e)** Add to AI classifier payload:
```python
            "sap_sf": sap_sf_result,
```

**f)** Add pipeline dispatch branch (before the final `else:` block):
```python
        elif site_type == "SAP_SF":
            jobs = await scrape_sap_sf(
                normalized_url,
                client=client,
                api_url=sap_sf_result.get("api_url", ""),
            )
            api_url = ""
            final_site_type = "SAP_SF"
            final_strategy = "api"
            logger.info("[PIPELINE] SAP_SF → %d jobs", len(jobs))
```

- [ ] **Step 6: Smoke-test**

```bash
python -c "
import asyncio
import httpx
from app.detectors.sap_successfactors import detect_sap_sf

async def run():
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        result = await detect_sap_sf('https://jobs.mercedes-benz.com', client=client)
        print('Detect:', result)
        if result['matched']:
            from app.scrapers.sap_successfactors import scrape_sap_sf
            jobs = await scrape_sap_sf('https://jobs.mercedes-benz.com', api_url=result['api_url'])
            print(f'Scrape: {len(jobs)} jobs')
            if jobs:
                print('Sample:', jobs[0])

asyncio.run(run())
"
```
Expected: `matched=True`, `jobs_found >= 3`, list of job dicts from Mercedes-Benz.

- [ ] **Step 7: Commit**

```bash
git add app/detectors/sap_successfactors.py app/scrapers/sap_successfactors.py \
        app/detectors/__init__.py app/services/ai_classifier.py app/services/orchestrator.py
git commit -m "feat: add SAP SuccessFactors detector and scraper with browser API interception"
```

---

## Self-Review Checklist

- [x] **Spec coverage:** All 5 fixes from the spec have tasks (timeout, Workday embed, Greenhouse subpath, SmartRecruiters, SAP_SF)
- [x] **No placeholders:** All steps contain complete code
- [x] **Type consistency:** `detect_sap_sf` / `scrape_sap_sf` used consistently; `detect_smartrecruiters` / `scrape_smartrecruiters` used consistently
- [x] **API classifier key `"sap_sf"`** used in orchestrator payload matches `tests.get("sap_sf", {})` in classifier
- [x] **API classifier key `"smartrecruiters"`** used in orchestrator payload matches `tests.get("smartrecruiters", {})` in classifier
- [x] **`_extract_jobs_from_items` import:** Both SAP_SF and Phenom scrapers import from `app.detectors.simple_api` — this is a private function but importable; if it moves, update both scrapers
