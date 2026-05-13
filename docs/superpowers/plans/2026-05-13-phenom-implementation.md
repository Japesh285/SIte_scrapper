# Phenom People Detector + Scraper Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Phenom People ATS support so sites like jobs.intuit.com, careers.nike.com, jobs.kraftheinz.com return real job counts instead of 0.

**Architecture:** Two-phase detection — fast static HTML signal check (no browser), then Playwright network interception to capture the job-list API URL. Scraper uses captured API URL for plain-HTTP offset pagination, no browser required at scrape time. All new code follows the existing iCIMS/Taleo detector→scraper→orchestrator pattern.

**Tech Stack:** Python 3.11+, httpx, Playwright (`playwright.async_api`), FastAPI orchestrator at `app/services/orchestrator.py`

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `app/detectors/phenom.py` | Create | Static HTML check + Playwright API interception |
| `app/scrapers/phenom.py` | Create | HTTP offset pagination using captured API URL |
| `app/detectors/__init__.py` | Modify | Export `detect_phenom` |
| `app/services/ai_classifier.py` | Modify | Add PHENOM to types, prompt, priority map |
| `app/services/orchestrator.py` | Modify | Step 4c, browser skip, pipeline branch, [FINAL] log |

---

## Task 1: Create the Phenom detector

**Files:**
- Create: `app/detectors/phenom.py`

- [ ] **Step 1: Write the file**

```python
# app/detectors/phenom.py
import httpx
from app.core.logger import logger
from app.core.site_utils import normalize_site_url

PHENOM_SIGNALS = ("phenompeople", "phenom-platform", "talent.phenompeople.com")


async def detect_phenom(
    url: str,
    client: httpx.AsyncClient | None = None,
    discovered_urls: list[str] | None = None,
) -> dict:
    normalized = normalize_site_url(url)
    if not normalized:
        return _not_matched()

    html = await _fetch_html(normalized, client)
    if not _has_phenom_signals(html, discovered_urls or []):
        return _not_matched()

    logger.info("[Phenom] signals found for %s — launching browser interception", normalized)
    return await _intercept_phenom_api(normalized)


def _has_phenom_signals(html: str, discovered_urls: list[str]) -> bool:
    lowered = html.lower()
    if any(s in lowered for s in PHENOM_SIGNALS):
        return True
    return any(s in u.lower() for s in PHENOM_SIGNALS for u in discovered_urls)


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


async def _intercept_phenom_api(url: str) -> dict:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.warning("[Phenom] Playwright not installed")
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
                content_type = response.headers.get("content-type", "")
                if "json" not in content_type:
                    return
                try:
                    body = await response.json()
                    items = _find_job_items(body)
                    if len(items) >= 5:
                        captured = {"url": response.url, "count": len(items)}
                        logger.info("[Phenom] captured API: %s (%d jobs)", response.url, len(items))
                except Exception:
                    pass

            page.on("response", on_response)

            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=20000)
                await page.wait_for_timeout(8000)
            except Exception as exc:
                logger.debug("[Phenom] navigation error (non-fatal): %s", exc)

            await browser.close()

            if captured:
                return {
                    "matched": True,
                    "api_url": captured["url"],
                    "jobs_found": captured["count"],
                    "api_usable": True,
                    "confidence": 0.85,
                }
            logger.info("[Phenom] no job-list API intercepted for %s", url)
            return _not_matched()

    except Exception as exc:
        logger.warning("[Phenom] browser interception failed: %s", exc)
        return _not_matched()


def _find_job_items(body) -> list:
    """Find the first list of 5+ dicts that all have a 'title' field."""
    if isinstance(body, list):
        items = [x for x in body if isinstance(x, dict) and "title" in x]
        if len(items) >= 5:
            return items
    if isinstance(body, dict):
        for value in body.values():
            if isinstance(value, list):
                items = [x for x in value if isinstance(x, dict) and "title" in x]
                if len(items) >= 5:
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

- [ ] **Step 2: Smoke-test the detector manually**

Run from the project root:
```bash
cd /home/japesh/Dev/scrapper/SIte_scrapper
python -c "
import asyncio
from app.detectors.phenom import detect_phenom
result = asyncio.run(detect_phenom('https://jobs.intuit.com'))
print(result)
assert result['matched'], 'Expected matched=True for jobs.intuit.com'
assert result['api_url'], 'Expected non-empty api_url'
print('PASS: Phenom detector works for jobs.intuit.com')
"
```
Expected: `matched=True`, non-empty `api_url`, `jobs_found >= 5`

- [ ] **Step 3: Commit**

```bash
git add app/detectors/phenom.py
git commit -m "feat: add Phenom People detector with browser API interception"
```

---

## Task 2: Create the Phenom scraper

**Files:**
- Create: `app/scrapers/phenom.py`

- [ ] **Step 1: Write the file**

```python
# app/scrapers/phenom.py
from urllib.parse import urlparse, urlencode, parse_qs, urlunparse

import httpx

from app.core.logger import logger
from app.core.site_utils import normalize_site_url
from app.detectors.simple_api import _extract_jobs_from_items

_PAGE_SIZE = 100
_MAX_JOBS = 10_000
_TOTAL_KEYS = ("totalCount", "total_count", "totalItems", "totalFound", "total", "count")
_LIST_KEYS = ("jobs", "positions", "results", "postings", "data", "items", "content", "requisitions")


async def scrape_phenom(
    url: str,
    client: httpx.AsyncClient | None = None,
    api_url: str = "",
) -> list[dict]:
    if not api_url:
        from app.detectors.phenom import detect_phenom
        result = await detect_phenom(url, client=client)
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

    # First page
    try:
        resp = await client.get(base, params={"limit": _PAGE_SIZE, "offset": 0})
        if resp.status_code != 200 or "json" not in resp.headers.get("content-type", ""):
            resp = await client.get(base)
        if resp.status_code != 200:
            return []
        first_data = resp.json()
    except Exception as exc:
        logger.warning("[Phenom] first page failed: %s", exc)
        return []

    first_items = _get_items(first_data)
    first_jobs = _extract_jobs_from_items(first_items, base_url)
    add_batch(first_jobs)

    total = _get_total(first_data)
    per_page = max(len(first_jobs), 1)
    if total <= per_page:
        return all_jobs

    pages_needed = min((total + per_page - 1) // per_page, _MAX_JOBS // _PAGE_SIZE)
    logger.info("[Phenom] paginating: total=%d per_page=%d pages=%d", total, per_page, pages_needed)

    for page_num in range(1, pages_needed):
        offset = page_num * _PAGE_SIZE
        if offset >= _MAX_JOBS:
            break
        try:
            resp = await client.get(base, params={"limit": _PAGE_SIZE, "offset": offset})
            if resp.status_code != 200 or "json" not in resp.headers.get("content-type", ""):
                break
            data = resp.json()
        except Exception as exc:
            logger.warning("[Phenom] page offset=%d failed: %s", offset, exc)
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
        if k.lower() not in ("offset", "limit", "page", "start", "from")
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

- [ ] **Step 2: Smoke-test the scraper manually**

Run (uses the api_url captured in Task 1 test — replace with actual URL printed there):
```bash
python -c "
import asyncio
from app.scrapers.phenom import scrape_phenom
jobs = asyncio.run(scrape_phenom('https://jobs.intuit.com'))
print(f'Got {len(jobs)} jobs')
if jobs:
    print('Sample:', jobs[0])
assert len(jobs) > 0, 'Expected jobs from jobs.intuit.com'
print('PASS')
"
```
Expected: list of dicts each with `title`, `location`, `url` keys; 50+ jobs.

- [ ] **Step 3: Commit**

```bash
git add app/scrapers/phenom.py
git commit -m "feat: add Phenom People scraper with offset pagination"
```

---

## Task 3: Wire up detector export and AI classifier

**Files:**
- Modify: `app/detectors/__init__.py`
- Modify: `app/services/ai_classifier.py`

- [ ] **Step 1: Export `detect_phenom` from `app/detectors/__init__.py`**

Open `app/detectors/__init__.py`. Add the import alongside the existing detector imports:

```python
from app.detectors.phenom import detect_phenom
```

Add `"detect_phenom"` to the `__all__` list (wherever the other detectors are listed).

- [ ] **Step 2: Update `app/services/ai_classifier.py`**

**a) Add to `ALLOWED_TYPES` set:**
```python
"PHENOM",
```

**b) Update `SYSTEM_PROMPT` — add PHENOM to the priority list between `ICIMS_API` and `DYNAMIC_API`:**
```
WORKDAY_API > GREENHOUSE_API > TALEO_API > ICIMS_API > PHENOM > DYNAMIC_API > SIMPLE_API > WP_JOBS > INTERACTIVE_DOM > DOM_LOAD_MORE > DOM_INFINITE_SCROLL > DOM_BROWSER
```

**c) In `_heuristic_classify`, add to `ranked_tests` list (between iCIMS and DYNAMIC_API):**
```python
("PHENOM", tests.get("phenom", {})),
```

**d) In `_heuristic_classify`, add to `priority` dict:**
```python
"PHENOM": 5,
```
(Same level as DYNAMIC_API; jobs_found/confidence tiebreaks.)

- [ ] **Step 3: Commit**

```bash
git add app/detectors/__init__.py app/services/ai_classifier.py
git commit -m "feat: register Phenom in detector exports and AI classifier"
```

---

## Task 4: Wire up the orchestrator

**Files:**
- Modify: `app/services/orchestrator.py`

- [ ] **Step 1: Add imports at the top of `orchestrator.py`**

Find the existing scraper imports block (around line 38–41):
```python
from app.scrapers.wp_jobs import scrape_wp_jobs
from app.detectors.wp_jobs import detect_wp_jobs
```

Add after it:
```python
from app.scrapers.phenom import scrape_phenom
from app.detectors.phenom import detect_phenom
```

- [ ] **Step 2: Add detection step 4c (after iCIMS, before WP_JOBS)**

Find this block (around line 312–318):
```python
        # 4. iCIMS (HTTP-only, fast)
        icims_result = await detect_icims(normalized_url, client=client, html=page_html)
        logger.info("iCIMS -> matched=%s jobs=%s usable=%s", ...)

        # 4b. WordPress job_box pages (HTTP-only, static HTML)
        wp_jobs_result = await detect_wp_jobs(...)
```

Insert between them:
```python
        # 4c. Phenom People (static signal check + browser interception if signals found)
        phenom_result = await detect_phenom(normalized_url, client=client)
        logger.info(
            "Phenom -> matched=%s jobs=%s usable=%s",
            phenom_result.get("matched"),
            phenom_result.get("jobs_found"),
            phenom_result.get("api_usable"),
        )
```

- [ ] **Step 3: Add Phenom to browser probe skip condition**

Find the `if not any(result.get("api_usable") for result in (...)):` block (around line 333).

Add `phenom_result,` inside the tuple, after `icims_result,`:
```python
        if not any(
            result.get("api_usable")
            for result in (
                workday_result,
                greenhouse_result,
                taleo_result,
                icims_result,
                phenom_result,      # ← add this line
                wp_jobs_result,
                ...
            )
        ):
```

- [ ] **Step 4: Add Phenom to browser-assisted re-run block**

Inside the browser probe block (around line 378, where Workday/Greenhouse/Taleo/iCIMS are re-run), add after the iCIMS re-run:
```python
            phenom_result = await detect_phenom(
                normalized_url,
                client=client,
                discovered_urls=discovered_urls,
            )
            logger.info(
                "Phenom (browser-assisted) -> matched=%s jobs=%s usable=%s",
                phenom_result.get("matched"),
                phenom_result.get("jobs_found"),
                phenom_result.get("api_usable"),
            )
```

- [ ] **Step 5: Add Phenom to AI classifier payload**

Find the `payload["tests"]` dict (around line 455). Add:
```python
            "phenom": phenom_result,
```

- [ ] **Step 6: Add pipeline dispatch branch**

Find the pipeline dispatch (around line 728), just before the `elif site_type == "WP_JOBS":` block. Add:
```python
        elif site_type == "PHENOM":
            jobs = await scrape_phenom(
                normalized_url,
                client=client,
                api_url=phenom_result.get("api_url", ""),
            )
            api_url = ""
            final_site_type = "PHENOM"
            final_strategy = "api"
            logger.info("[PIPELINE] PHENOM → %d jobs", len(jobs))
```

- [ ] **Step 7: Add [FINAL] log before the return statement**

Find the `return {` at the bottom of the orchestrator function (around line 796). Add immediately before it:
```python
    logger.info(
        "[FINAL] url=%-40s  ats=%-20s  jobs=%d  strategy=%s  confidence=%.2f",
        normalized_url,
        final_site_type,
        len(jobs),
        final_strategy,
        confidence,
    )
```

- [ ] **Step 8: Commit**

```bash
git add app/services/orchestrator.py
git commit -m "feat: integrate Phenom into detection pipeline and add [FINAL] log"
```

---

## Task 5: End-to-end verification

- [ ] **Step 1: Run the full pipeline on a Phenom site**

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
    async with Session() as session:
        result = await orchestrate_scrape('https://jobs.intuit.com', session)
        print(result)
        assert result['type'] == 'PHENOM', f'Expected PHENOM, got {result[\"type\"]}'
        assert result['jobs_found'] > 0, 'Expected jobs > 0'
        print('PASS')

asyncio.run(run())
"
```

- [ ] **Step 2: Spot-check two more sites**

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
    for url in ['https://careers.nike.com', 'https://jobs.kraftheinz.com']:
        async with Session() as session:
            result = await orchestrate_scrape(url, session)
            print(f'{url}: type={result[\"type\"]} jobs={result[\"jobs_found\"]}')

asyncio.run(run())
"
```

Expected: `type=PHENOM`, `jobs_found > 0` for both.

- [ ] **Step 3: Verify [FINAL] log appears**

Check the server logs for lines like:
```
[FINAL] url=https://jobs.intuit.com            ats=PHENOM               jobs=156  strategy=api  confidence=0.85
```

- [ ] **Step 4: Commit**

```bash
git commit --allow-empty -m "chore: Phenom implementation complete and verified"
```
