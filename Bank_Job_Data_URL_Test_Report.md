# Bank Job Data — URL Test Report

**Date:** 2026-05-13  
**Source:** `Bank Job Data.xlsx` (column: `Exact India Jobs Link`)  
**Total URLs tested:** 59  

---

## Summary

| Category | Count |
|---|---|
| Working (jobs extracted) | 8 |
| Workday detected but URL-only (needs scraper) | 2 |
| Oracle HCM (not supported) | 3 |
| Avature (not supported) | 1 |
| SmartRecruiters (not supported) | 1 |
| LinkedIn (blocked) | 1 |
| UNKNOWN / 0 jobs | 43 |

---

## Working Sites

These URLs returned jobs successfully.

| # | URL | Detector | Jobs |
|---|---|---|---|
| 4 | ciena.wd5.myworkdayjobs.com | WORKDAY_API | 25 |
| 14 | goodyear.wd1.myworkdayjobs.com | WORKDAY_API | 407 |
| 15 | job-boards.greenhouse.io | GREENHOUSE_API | 128 |
| 20 | careers.generalmills.com | SIMPLE_API | 10 (paginator gets 369) |
| 31 | lexmark.wd1.myworkdayjobs.com | WORKDAY_API | 49 |
| 35 | kyndryl.wd5.myworkdayjobs.com | WORKDAY_API | 861 |
| 53 | o9solutions.wd5.myworkdayjobs.com | WORKDAY_API | 59 |
| 58 | medtronic.wd1.myworkdayjobs.com | WORKDAY_API | 1119 |

**Notes:**
- General Mills: detection returns 10 (first-page default), but `paginate_simple_api_jobs()` retrieves the full **369 jobs**. The scraper pipeline uses pagination, so the full count is fetched at scrape time.
- All Workday sites use the `WORKDAY_API` strategy which is fully supported and paginated.

---

## Workday URL-Only (Pattern Detected, API Not Called)

These are Workday-hosted domains but the detector returned `WORKDAY_URL` (pattern match only, no API call made during detection). They should work fine through the scraper pipeline — the scraper calls the Workday API directly.

| # | URL | Note |
|---|---|---|
| 8 | guardianlife.wd5.myworkdayjobs.com | `workday_url_pattern_only` |
| 22 | intel.wd1.myworkdayjobs.com | `workday_url_pattern_only` |

**Fix:** No code change needed. The orchestrator recognises `WORKDAY_URL` and routes to `scrape_workday()`.

---

## Not Supported ATS Platforms

### Oracle HCM Cloud

Oracle HCM Cloud uses a closed API requiring authenticated tenant access. Scraping is not feasible without credentials.

| # | URL | Note |
|---|---|---|
| 10 | ibqbjb.fa.ocs.oraclecloud.com | `oracle_hcm_not_supported` |
| 17 | hcog.fa.em2.oraclecloud.com | `oracle_hcm_not_supported` |
| 23 | ejgk.fa.em2.oraclecloud.com | `oracle_hcm_not_supported` |

**Recommendation:** Skip or flag these as unsupported in the pipeline output.

### Avature

Avature's platform blocks API access from unauthenticated scrapers.

| # | URL | Note |
|---|---|---|
| 25 | koch.avature.net | `avature_not_supported` |

**Recommendation:** Skip or flag as unsupported.

### SmartRecruiters

SmartRecruiters detected but returned 0 jobs — likely a tenant-specific gating issue or the URL is an aggregate board, not a specific company page.

| # | URL | Note |
|---|---|---|
| 49 | careers.smartrecruiters.com | Detected but 0 jobs |

---

## Blocked / Rate-Limited

| # | URL | Reason |
|---|---|---|
| 38 | www.linkedin.com | `linkedin_scraping_blocked` — LinkedIn actively blocks scrapers |

---

## UNKNOWN (0 Jobs) — Full List

These sites were not matched by any detector. Most are custom career portals, Phenom-powered sites, SAP SuccessFactors, or sites that block automated requests.

| # | URL | Time (s) | Likely Platform |
|---|---|---|---|
| 1 | www.schwabjobs.com | 2.5 | Unknown |
| 2 | careers.chevron.com | 2.9 | Unknown / Custom |
| 3 | gtprod.talentrecruit.com | 0.9 | TalentRecruit (Indian ATS) |
| 5 | www.cignex.com | 6.4 | Custom |
| 6 | workwithus.circlek.com | 7.9 | Unknown |
| 7 | www.hersheyland.in | 6.5 | Unknown |
| 9 | career.hmie.in | 1.5 | Custom |
| 11 | portal.careers.hsbc.com | 8.4 | Phenom / Custom |
| 12 | career.huawei.com | 8.3 | Custom |
| 13 | careers.cisco.com | 16.2 | Cisco custom |
| 16 | careers.intuitive.com | 0.7 | Unknown |
| 18 | www.ibm.com | 12.6 | IBM custom |
| 19 | careers.kimberly-clark.com | 1.1 | Unknown |
| 21 | jobs.intuit.com | 9.3 | Phenom |
| 24 | careers.knorr-bremse.com | 7.9 | Unknown |
| 26 | jobs.kraftheinz.com | 9.9 | Phenom |
| 27 | careers.irco.com | 46.6 | Timeout / Custom |
| 28 | careers.loreal.com | 17.5 | Custom |
| 29 | careers.labcorp.com | 12.4 | Unknown |
| 30 | www.infovision.com | 60.4 | Timeout |
| 32 | globalcareers.lge.com | 7.8 | LG custom |
| 33 | careers.lilly.com | 2.9 | Workday embed? |
| 34 | jobs.kuehne-nagel.com | 51.9 | Timeout / Custom |
| 36 | careers.mediatek.com | 4.4 | Unknown |
| 37 | jobs.mercedes-benz.com | 4.4 | SAP SuccessFactors |
| 39 | www.metacareers.com | 12.0 | Meta custom |
| 40 | careers.marsh.com | 27.0 | Workday embed |
| 41 | apply.careers.microsoft.com | 13.0 | Microsoft custom |
| 42 | jobs.kennametal.com | 97.0 | Timeout |
| 43 | jobs.lenovo.com | 61.0 | Timeout |
| 44 | www.mondelezinternational.com | 10.1 | Phenom |
| 45 | career.kiaindia.net | 100.1 | Timeout |
| 46 | www.mongodb.com | 9.2 | Greenhouse (needs path) |
| 47 | careers.kohler.co.in | 97.3 | Timeout |
| 48 | www.nestle.in | 1.9 | Unknown |
| 50 | jobs.natwestgroup.com | 3.4 | Unknown |
| 51 | oaknorth.co.uk | 2.2 | Unknown |
| 52 | www.nipponexpress.com | 4.3 | Unknown |
| 54 | careers.nike.com | 8.1 | Phenom |
| 55 | careers.moodys.com | 13.5 | Unknown |
| 56 | www.novartis.com | 16.1 | Workday embed |
| 57 | www.okta.com | 18.4 | Greenhouse embed |
| 59 | www.metlifecareers.com | 92.2 | Timeout |

---

## Categorised Issues & Next Steps

### 1. Timeout Sites (>30s, 0 jobs)

These sites are either slow servers, bot-detection-delayed responses, or returning 200 with no useful content after a long wait. The pipeline timeout is likely cutting them off.

**Sites:** careers.irco.com, jobs.kuehne-nagel.com, www.infovision.com, jobs.kennametal.com, jobs.lenovo.com, career.kiaindia.net, careers.kohler.co.in, www.metlifecareers.com

**Recommended fix:**
- Increase HTTP client timeout to 120s for the detection phase.
- Or: add a fast pre-check (HEAD request) to detect slow servers and route them to a dedicated slow-lane worker.

---

### 2. Phenom Platform Sites

Phenom People is a modern SaaS ATS. Sites like `jobs.intuit.com`, `careers.nike.com`, `jobs.kraftheinz.com`, `www.mondelezinternational.com`, `portal.careers.hsbc.com` are likely Phenom-powered.

**How to detect:** HTML contains `phenom-people`, `phenompeople`, or `talent.phenompeople.com` API calls.

**Phenom API endpoint pattern:**
```
GET https://{tenant}.talent.phenompeople.com/api/v1/jobs?limit=100&offset=0
```

**Recommended fix:** Add a `PhenomPeople` detector + scraper (similar to Workday/Greenhouse).

---

### 3. SAP SuccessFactors Sites

Sites like `jobs.mercedes-benz.com` use SAP SuccessFactors. The public API is:
```
GET https://jobs.mercedes-benz.com/api/v1/listings/?...
```
or via `successfactors.com` tenant APIs.

**Recommended fix:** Add SAP SuccessFactors detector (look for `sap-successfactors`, `sfsf`, or `successfactors.com` in page source).

---

### 4. Workday Embed Sites

Some company sites embed Workday in an iframe or redirect to a `*.myworkdayjobs.com` subdomain on interaction. Detection fails because the top-level page doesn't expose the Workday API directly.

**Suspected sites:** careers.lilly.com, careers.marsh.com, www.novartis.com

**Recommended fix:** In the browser probe, detect `myworkdayjobs.com` iframes or JS redirects and extract the Workday tenant subdomain for API calls.

---

### 5. Greenhouse Embed Sites

Sites like `www.okta.com` and `www.mongodb.com` likely embed Greenhouse boards at a subpath (e.g. `/company/jobs` or `/careers`). The detector was given the homepage URL rather than the Greenhouse board URL.

**Recommended fix:** Feed the direct board URL (e.g. `boards.greenhouse.io/{company}`) rather than the marketing homepage.

---

### 6. iCIMS Sites to Test

Two sites that may be iCIMS-powered were in the UNKNOWN list. Run them through the full scraper pipeline (not just detection) to confirm.

**Sites to test:**  
- careers.labcorp.com  
- careers.kimberly-clark.com  

---

### 7. TalentRecruit (Indian ATS)

`gtprod.talentrecruit.com` is a homegrown Indian ATS used by many mid-size Indian companies. It has a REST API but requires knowing the company ID.

**Recommended fix:** Add a TalentRecruit detector that reads the company/tenant ID from the HTML and constructs the API URL.

---

## What Was Fixed In This Session

| Issue | Fix Applied |
|---|---|
| General Mills returning only 10 of 369 jobs | Added `paginate_simple_api_jobs()` — now fetches all pages via `page=N&limit=100` |
| General Mills iCIMS login URLs in API response | `_canonical_job_url()` rewrites `*.icims.com/jobs/{id}/login` → `{company}/careers/jobs/{id}` |
| `_contains_ui_noise` false positive on city "Paranavai" | Changed to word-boundary regex (`\b`) — prevents matching "nav" inside city names |
| iCIMS detector Strategy 1 always 401 (OAuth) | Removed OAuth strategy entirely; Strategy 2 now leads |
| iCIMS Strategy 2 false positives | Added strong-signal gate: requires `icims.com` in HTML or discovered URLs |
| GlobalLogic 0 jobs (DOM scraper can't match `/careers/slug-irc12345/`) | Added new `WP_JOBS` detector + scraper using static HTML parsing of `job_box` elements |
| Taleo no pagination cap | Added `max_rows = 10_000` hard limit |
| Orchestrator double-detection for iCIMS/Taleo | Added `api_url` bypass parameter to scrapers |

---

## Coverage Summary

| ATS / Platform | Status |
|---|---|
| Workday | Fully supported |
| Greenhouse | Fully supported |
| iCIMS | Supported (Strategies 2–3) |
| Taleo | Supported (paginated) |
| Simple API (General Mills-style) | Supported (paginated) |
| WordPress job_box (GlobalLogic-style) | Supported (new WP_JOBS scraper) |
| Oracle HCM Cloud | Not supported |
| Avature | Not supported |
| Phenom People | Not supported — high priority to add |
| SAP SuccessFactors | Not supported — medium priority to add |
| TalentRecruit | Not supported — low priority |
| LinkedIn | Blocked (intentionally) |
| SmartRecruiters | Detected but 0 jobs (needs investigation) |
