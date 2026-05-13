# Design: Remaining ATS Fixes — Batch 2

**Date:** 2026-05-13  
**Status:** Approved  
**Scope:** Timeout fix, SmartRecruiters, Greenhouse embed, Workday embed, SAP SuccessFactors

---

## Background

After adding Phenom People support (Batch 1), these five categories of sites still return 0 jobs from the Bank Job Data URL list. This spec covers all remaining actionable fixes.

---

## Fix 1 — Timeout Sites

### Problem

`httpx.AsyncClient(timeout=20)` is hardcoded throughout the codebase. Sites like `careers.irco.com` (46s), `jobs.kuehne-nagel.com` (51s), `jobs.kennametal.com` (97s) time out before returning any content.

### Fix

Change the timeout to `httpx.Timeout(connect=10, read=90, write=10, pool=10)` in the orchestrator's shared client creation. This:
- Keeps connection establishment fast (10s) to fail quickly on unreachable hosts
- Allows slow-responding servers up to 90s to return a response

### Files

| File | Change |
|---|---|
| `app/services/orchestrator.py` | Update `httpx.AsyncClient` timeout at client creation |

### No new detectors or scrapers needed.

---

## Fix 2 — SmartRecruiters

### Problem

The URL `careers.smartrecruiters.com` (no company slug) is the generic SmartRecruiters homepage — no company-specific jobs available. If a company-specific URL like `careers.smartrecruiters.com/{slug}` is provided, SmartRecruiters has a public API:

```
GET https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100&offset=0
```

Response contains `content` array of job objects plus `totalFound` for pagination.

### Detector (`app/detectors/smartrecruiters.py`)

**Phase 1 — Slug extraction:**
1. Parse slug from URL path: `careers.smartrecruiters.com/{slug}` or `jobs.smartrecruiters.com/{slug}`
2. If URL has no slug: check HTML for `smartrecruiters.com/{slug}` pattern
3. If still no slug: check `discovered_urls` from browser probe
4. If no slug found: return `matched=False, reason="no_company_slug"`

**Phase 2 — API call:**
If slug found, call `https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=10` to verify it returns jobs. Return matched result with `api_url`.

**Return schema:**
```python
{
    "matched": bool,
    "api_url": str,
    "jobs_found": int,
    "api_usable": bool,
    "slug": str,
    "confidence": float,
}
```

### Scraper (`app/scrapers/smartrecruiters.py`)

Paginates using `offset=0, 100, 200, ...` reading `totalFound` from the first response. Uses `_extract_jobs_from_items` from `simple_api` for job extraction. Accepts `api_url` bypass.

### Orchestrator integration

- Add step 4e after Phenom (4c) and WP_JOBS (4d)
- Add `"SMARTRECRUITERS"` to AI classifier types, prompt, ranked_tests, priority map (priority=4, between WP_JOBS=3 and SIMPLE_API=4)
- Add pipeline dispatch branch `elif site_type == "SMARTRECRUITERS"`
- Add `smartrecruiters_result.get("matched")` to browser probe skip condition

### Files

| File | Change |
|---|---|
| `app/detectors/smartrecruiters.py` | New |
| `app/scrapers/smartrecruiters.py` | New |
| `app/detectors/__init__.py` | Export `detect_smartrecruiters` |
| `app/services/orchestrator.py` | Step 4e, browser skip, pipeline branch |
| `app/services/ai_classifier.py` | Add SMARTRECRUITERS |

---

## Fix 3 — Greenhouse Embed (Okta, MongoDB)

### Problem

`www.okta.com` and `www.mongodb.com` embed their Greenhouse boards on a careers subpage, not the homepage. The `resolve_greenhouse_slug` function fetches the provided URL and checks for `boards.greenhouse.io/{slug}` patterns. When the URL is the homepage, this pattern is not present.

### Fix

Extend `resolve_greenhouse_slug` in `app/detectors/greenhouse.py` — after all existing checks fail, try fetching a short list of common careers subpaths and check each response for the Greenhouse board pattern:

```python
CAREERS_SUBPATHS = ["/careers", "/jobs", "/company/careers", "/about/careers"]
```

For each subpath:
1. `GET {origin}{subpath}`
2. Check response HTML for `GREENHOUSE_BOARD_PATTERN`
3. If found → return slug immediately, stop trying more paths

Cap: 4 subpaths maximum. Uses the existing shared `client`.

### Files

| File | Change |
|---|---|
| `app/detectors/greenhouse.py` | Extend `resolve_greenhouse_slug` with subpath fallback |

### No new detector or scraper needed.

---

## Fix 4 — Workday Embed (Lilly, Novartis, Marsh)

### Problem

Sites like `careers.lilly.com` redirect to `lilly.wd1.myworkdayjobs.com` via a JavaScript redirect. The browser probe captures the `final_url` after navigation, but this URL is currently only used to update `normalized_url` in the orchestrator. It is not added to `discovered_urls` before the Workday detector re-runs, so the Workday detector's `_build_workday_candidates` never sees the Workday subdomain.

### Fix

In the orchestrator, immediately after building `discovered_urls` from the browser probe, add:

```python
if browser_final_url and "myworkdayjobs" in browser_final_url.lower():
    discovered_urls.append(browser_final_url)
```

This ensures the Workday detector's `_build_workday_candidates` receives the redirected Workday URL in `discovered_urls` and can construct the correct `/wday/cxs/{tenant}/{site}/jobs` API endpoint.

### Files

| File | Change |
|---|---|
| `app/services/orchestrator.py` | 2-line addition after `discovered_urls` is built |

### No new detector or scraper needed.

---

## Fix 5 — SAP SuccessFactors (Mercedes-Benz)

### Problem

No SAP SuccessFactors detector exists. SAP SF implementations vary per tenant — there is no universal public API path like Workday's `/wday/cxs/`. Browser network interception is the only reliable way to discover the actual API endpoint.

### Detector (`app/detectors/sap_successfactors.py`)

Same two-phase approach as the Phenom detector:

**Phase 1 — Static signal check:**
Check page HTML for any of:
- `successfactors` (in any case)
- `sfsf`
- `successfactors.com` in `<script src>` attributes

Exit with `matched=False` if no signals (fast path, no browser cost).

**Phase 2 — Browser API interception:**
Launch Playwright via existing `get_browser()`. Intercept all responses. Navigate to the URL. Capture the first intercepted JSON response that:
- Is from a domain containing `successfactors`, `sap.com`, or `sapjobs`
- Contains a list of 5+ objects with a `title` field

Return captured `api_url` and `jobs_found`. Timeout: 25s.

**Return schema:**
```python
{
    "matched": bool,
    "api_url": str,
    "jobs_found": int,
    "api_usable": bool,
    "confidence": float,
}
```

### Scraper (`app/scrapers/sap_successfactors.py`)

Accepts `api_url` from detector. Tries offset-based pagination (`offset=0, 100, ...`), then page-based (`page=1, 2, ...`) if offset doesn't advance. Reads `totalCount` / `total` from response for total count. Cap: 10,000 jobs. Uses `_extract_jobs_from_items` from `simple_api`.

### Orchestrator integration

- Add step 4f (after SmartRecruiters 4e)
- Add `"SAP_SF"` to AI classifier types, prompt, ranked_tests, priority map (priority=6, same level as ICIMS_API)
- Add pipeline dispatch branch `elif site_type == "SAP_SF"`
- Add `sap_sf_result.get("matched")` to browser probe skip condition

### Files

| File | Change |
|---|---|
| `app/detectors/sap_successfactors.py` | New |
| `app/scrapers/sap_successfactors.py` | New |
| `app/detectors/__init__.py` | Export `detect_sap_sf` |
| `app/services/orchestrator.py` | Step 4f, browser skip, pipeline branch |
| `app/services/ai_classifier.py` | Add SAP_SF |

---

## Updated Detection Order

```
1.  Workday
2.  Greenhouse
3.  Taleo
4.  iCIMS
4c. Phenom          (Batch 1)
4d. WP_JOBS
4e. SmartRecruiters  (Batch 2)
4f. SAP_SF           (Batch 2)
5.  DOM detectors
6.  Browser probe   (+ Workday final_url fix)
7.  AI classifier
```

---

## Files Changed — Complete List

| File | Change |
|---|---|
| `app/services/orchestrator.py` | Timeout fix, Workday final_url fix, SmartRecruiters step, SAP_SF step, browser skips |
| `app/detectors/greenhouse.py` | Add careers subpath fallback in `resolve_greenhouse_slug` |
| `app/detectors/smartrecruiters.py` | New |
| `app/scrapers/smartrecruiters.py` | New |
| `app/detectors/sap_successfactors.py` | New |
| `app/scrapers/sap_successfactors.py` | New |
| `app/detectors/__init__.py` | Add exports |
| `app/services/ai_classifier.py` | Add SMARTRECRUITERS, SAP_SF |

---

## Spec Self-Review

- No TBD/TODO placeholders
- Priority for SmartRecruiters (4) matches WP_JOBS priority — acceptable since they're unlikely to both match the same site
- SAP_SF priority (6) equals iCIMS — tiebroken by jobs_found/confidence
- Greenhouse and Workday fixes touch only existing files, no new detectors introduced
- Timeout change affects all HTTP requests globally — acceptable since connect timeout stays at 10s (fast fail for unreachable hosts)
