# Design: Phenom People Detector + Scraper

**Date:** 2026-05-13  
**Status:** Approved  
**Scope:** Add Phenom People ATS support to the scraper pipeline

---

## Background

Approximately 6 sites in the Bank Job Data URL list are powered by Phenom People (now "Phenom"), a modern SaaS ATS. Examples: jobs.intuit.com, careers.nike.com, jobs.kraftheinz.com, www.mondelezinternational.com, portal.careers.hsbc.com, careers.marsh.com. All currently return 0 jobs (classified as UNKNOWN).

Phenom sites are JavaScript-heavy; their job listings are fetched via an internal REST or GraphQL API, not present in static HTML. The API URL varies by tenant and must be discovered at runtime via browser network interception.

---

## Approach

**Option A — browser-intercepted API URL (selected)**

1. Fast static HTML check for Phenom signals (no browser cost if not a Phenom site)
2. If signals found, Playwright intercepts the actual API call while loading the jobs page
3. Captured `api_url` passed to scraper, which paginates via plain HTTP — no browser needed for scraping

---

## Section 1: Detector (`app/detectors/phenom.py`)

### Phase 1 — Static signal check

Fetch page HTML with `httpx`. Exit early (`matched=False`) if none of the following are present:
- `phenompeople` anywhere in the HTML
- `phenom-platform` in HTML
- `talent.phenompeople.com` in any `<script src>` or network hint
- `"phenom"` in `<title>` or meta tags

This fast path avoids browser startup for the ~95% of sites that are not Phenom.

### Phase 2 — Browser API interception

If static signals found, launch Playwright using the existing `get_browser()` helper from `app/detectors/browser.py`. Install a route interceptor on `**`. Navigate to the URL. For each intercepted response:

- `Content-Type` must include `application/json`
- Body must be a list or a dict containing a list of 5+ items
- Each item must have a `title` field and at least one of `location`, `city`, `id`, `req_id`

Take the first matching intercepted URL as `api_url`. Extract `jobs_found` from the response count.

**Timeout:** 20 seconds. If no valid API call intercepted → return `matched=False`.

### Return schema

```python
{
    "matched": bool,
    "api_url": str,          # "" if not matched
    "jobs_found": int,       # count from intercepted response
    "api_usable": bool,      # True if matched
    "confidence": float,     # 0.85 if matched, 0.0 otherwise
}
```

### Signature

```python
async def detect_phenom(
    url: str,
    client: httpx.AsyncClient | None = None,
    discovered_urls: list[str] | None = None,
) -> dict:
```

---

## Section 2: Scraper (`app/scrapers/phenom.py`)

### Pagination strategy

Phenom APIs use offset-based pagination. The scraper:

1. Strips existing `offset`, `limit`, `page` params from `api_url`
2. Requests `api_url?limit=100&offset=0`
3. Reads `totalCount` / `total` / `count` from response to determine total pages
4. Loops `offset=100, 200, 300, ...` until all pages fetched or no new jobs returned
5. Hard cap: 10,000 jobs (100 pages × 100) as safety limit

### Job extraction

Imports and reuses `_extract_jobs_from_items` from `app/detectors/simple_api` — no duplication. This handles `title`, `location`, `url`/`absolute_url`, `id`/`req_id`, and the iCIMS URL rewriting logic already in place.

### `api_url` bypass

If `api_url=""` (orchestrator didn't pass one), the scraper runs `detect_phenom()` inline to discover it. Consistent with how `scrape_icims` and `scrape_taleo` handle the bypass.

### Signature

```python
async def scrape_phenom(
    url: str,
    client: httpx.AsyncClient | None = None,
    api_url: str = "",
) -> list[dict]:
```

---

## Section 3: Orchestrator Integration (`app/services/orchestrator.py`)

### Detection step order

Phenom slots in at step 4c — after iCIMS, before WP_JOBS:

```
1. Workday
2. Greenhouse
3. Taleo
4. iCIMS
4c. Phenom   ← new
4d. WP_JOBS
5. DOM detectors
6. Browser probe
7. AI classifier
```

### Browser probe skip

Add `phenom_result.get("matched")` to the existing skip condition so a second browser session is not launched if Phenom already found and captured the API URL.

### Pipeline dispatch branch

```python
elif site_type == "PHENOM":
    jobs = await scrape_phenom(
        normalized_url,
        client=client,
        api_url=phenom_result.get("api_url", ""),
    )
    final_site_type = "PHENOM"
    final_strategy = "api"
```

### AI classifier updates (`app/services/ai_classifier.py`)

- Add `"PHENOM"` to `ALLOWED_TYPES`
- Add `PHENOM` to system prompt priority list between `ICIMS_API` and `DYNAMIC_API`
- Add `("PHENOM", tests.get("phenom", {}))` to `ranked_tests`
- Add `"PHENOM": 5` to `priority` map (same level as DYNAMIC_API; if both match, higher `jobs_found` / `confidence` wins the tiebreak)

### Final result log (applied globally, not just Phenom)

Add immediately before the `return` statement at line 796:

```python
logger.info(
    "[FINAL] url=%s  ats=%-20s  jobs=%d  strategy=%s  confidence=%.2f",
    normalized_url,
    final_site_type,
    len(jobs),
    final_strategy,
    confidence,
)
```

This gives a single grep-able line per run:
```
[FINAL] url=jobs.intuit.com  ats=PHENOM               jobs=156  strategy=api  confidence=0.85
[FINAL] url=www.ibm.com      ats=UNKNOWN              jobs=0    strategy=dom  confidence=0.00
```

---

## Section 4: Error Handling

| Scenario | Handling |
|---|---|
| Browser launch fails | Log warning, return `matched=False`, pipeline falls to DOM detectors |
| Interception timeout (>20s) | Return `matched=False` |
| HTTP pagination error mid-scrape | Return jobs collected so far, log warning |
| Empty page response during pagination | Stop loop, return what we have |

---

## Files Changed

| File | Change |
|---|---|
| `app/detectors/phenom.py` | New — static check + browser interception |
| `app/scrapers/phenom.py` | New — offset-based HTTP pagination |
| `app/detectors/__init__.py` | Add `detect_phenom` to exports and `__all__` |
| `app/services/orchestrator.py` | Step 4c, browser skip, pipeline branch, `[FINAL]` log |
| `app/services/ai_classifier.py` | Add PHENOM to types, prompt, priority map |

---

## Verification

Manual end-to-end test after implementation:

1. Run `detect_phenom("https://jobs.intuit.com")` — expect `matched=True` and non-empty `api_url`
2. Run `scrape_phenom("https://jobs.intuit.com")` — expect jobs list with 50+ entries
3. Spot-check `careers.nike.com` and `jobs.kraftheinz.com` — verify non-zero job counts
4. Grep `[FINAL]` in logs — verify ATS name appears correctly for each tested URL
