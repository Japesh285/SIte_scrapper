# Workday Full Context Mode - Implementation Summary

## Overview
Modified the Workday job detail extraction pipeline to send FULL context to AI, bypassing aggressive filtering that may be causing missing `required_skill_set`.

## Changes Made

### 1. `app/job_detail_engine/orchestrator.py`
**Modified**: `extract_job_details()` function
- Added `site_type: str = ""` parameter
- Added conditional routing: When `site_type == "WORKDAY_API"`, calls `extract_with_ai_workday_full()` instead of `extract_with_ai()`
- All other site types (GREENHOUSE_API, SIMPLE_API, DOM) use existing pipeline unchanged

**Key code**:
```python
if site_type == "WORKDAY_API":
    ai_result = await extract_with_ai_workday_full(clean_text, result)
else:
    ai_result = await extract_with_ai(clean_text, result)
```

### 2. `app/job_detail_engine/ai/extractor.py`
**Added**: New functions for Workday full context mode

#### `extract_with_ai_workday_full(text, known_fields)`
- **BYPASSES** `filter_content_for_ai()` completely
- Sends FULL cleaned description text to AI (NO chunking, NO truncation)
- Uses `_WORKDAY_SYSTEM_PROMPT` optimized for comprehensive skill extraction
- Includes debug logging:
  ```python
  print("[DEBUG] Workday FULL context mode enabled")
  print("[DEBUG] Description length:", len(text))
  ```
- Longer timeout (180s vs 90s) to handle larger input
- Tracks stats in `workday_full_context_stats` field

#### `_build_ai_input_workday_full(full_text, known_fields)`
- Builds AI input with COMPLETE job posting
- NO filtering, NO chunking applied
- Label: "FULL Workday job posting - NO FILTERING APPLIED"

#### `_WORKDAY_SYSTEM_PROMPT`
- New system prompt optimized for full context extraction
- Emphasizes: "Extract required_skills comprehensively from the FULL description"
- Instructs AI to: "Extract every skill, qualification, and requirement you can find"

### 3. `app/job_detail_engine/utils/cleaner.py`
**Modified**: `clean_html()` function
- Added `truncate: bool = True` parameter
- When `truncate=False`: Returns full cleaned text without 4800 char limit
- When `truncate=True` (default): Existing behavior unchanged

**Key code**:
```python
def clean_html(html: str, truncate: bool = True) -> str:
    # ... cleaning logic ...
    
    # Truncate ONLY if truncate=True
    if truncate and len(text) > _MAX_TEXT_CHARS:
        text = text[:_MAX_TEXT_CHARS]
    
    return text
```

**Note**: Also removed "benefits", "perks", "why work with us", "why join us" from `_IRRELEVANT_SECTIONS` to preserve more content for Workday jobs.

### 4. `app/services/detail_extractor.py`
**Modified**: `_workday_html_detail()` function
- Now passes `site_type="WORKDAY_API"` when calling the orchestrator

**Key code**:
```python
ai_detail = await extract_job_details(html, force_ai=True, site_type="WORKDAY_API")
```

## Pipeline Flow

### Before (Workday jobs):
```
HTML → clean_html() [4800 char truncation]
     → filter_content_for_ai() [chunking, scoring, top-4 chunks, 3600 chars]
     → AI extraction [limited context]
     → required_skills [often empty/incomplete]
```

### After (Workday jobs - TEST MODE):
```
HTML → clean_html(truncate=False) [NO truncation]
     → [BYPASSES filter_content_for_ai()]
     → extract_with_ai_workday_full() [FULL context]
     → AI extraction [complete job posting]
     → required_skills [should be comprehensive]
```

### Other Site Types (UNCHANGED):
- GREENHOUSE_API: Existing pipeline (filtering + AI)
- SIMPLE_API: Existing pipeline (filtering + AI)
- DOM sites: Existing pipeline (filtering + AI)

## Debug Logging

When Workday full context mode is active, you'll see:
```
[DEBUG] Workday FULL context mode enabled
[DEBUG] Description length: 12345
[DEBUG] Workday FULL context mode enabled
[DEBUG] Workday full description length: 12345 chars
[AI] Workday FULL context — Tokens used: input=3086, output=150, total=3236 (from 12345 chars, NO filtering)
[AI] Workday FULL context extraction — fields filled: 8/13
```

## Expected Results

**IF filtering was the issue**:
- `required_skill_set` should now be populated with comprehensive skills
- Skills will be extracted from all sections of the job posting
- No missing skills due to chunk scoring/filtering

**Token usage**:
- Expect higher input tokens (full description vs 3600 chars)
- Output tokens should remain similar
- Total cost per Workday job: ~2-3x higher input tokens

## Testing

Run validation:
```bash
python3 test_workday_changes.py
```

All tests pass ✅

## Important Notes

1. **TEST MODE ONLY**: This is a diagnostic change to verify if filtering causes missing skills
2. **No global changes**: `filter_content_for_ai()` remains unchanged for other site types
3. **No schema changes**: AI output format remains identical
4. **Reversible**: To revert, simply remove the `site_type` conditional in orchestrator.py
5. **Production readiness**: If this works, can be optimized with better token management

## Next Steps

1. Run scraper on Workday sites
2. Check if `required_skill_set` is now populated
3. Compare skill extraction quality vs before
4. Monitor token usage and costs
5. If successful, consider making this permanent or adding as configurable option

## Files Modified

- `app/job_detail_engine/orchestrator.py` (added site_type param, WORKDAY_API routing)
- `app/job_detail_engine/ai/extractor.py` (added workday full context functions)
- `app/job_detail_engine/utils/cleaner.py` (added truncate parameter)
- `app/services/detail_extractor.py` (passes site_type to orchestrator)

## Files Created

- `test_workday_changes.py` (validation test script)
