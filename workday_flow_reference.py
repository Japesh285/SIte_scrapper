#!/usr/bin/env python3
"""
Quick reference: How the Workday full context mode works

This script traces the flow without executing it.
"""

print("""
================================================================================
WORKDAY FULL CONTEXT MODE - Data Flow
================================================================================

1. ENTRY POINT: app/api/routes.py
   ↓
   extract_api_details(strategy="api", job=job_data, site_type="WORKDAY_API", ...)
   
2. DETAIL EXTRACTOR: app/services/detail_extractor.py
   ↓
   extract_job_details() → detects strategy="api" and site_type="WORKDAY_API"
   ↓
   _workday_html_detail(job, base_url, client, result)
   ↓
   Fetches HTML from Workday public URL
   ↓
   extract_job_details(html, force_ai=True, site_type="WORKDAY_API")
   [NOTE: site_type is now passed to orchestrator]
   
3. ORCHESTRATOR: app/job_detail_engine/orchestrator.py
   ↓
   extract_job_details(html, force_ai=True, site_type="WORKDAY_API")
   ↓
   Parses HTML (JSON-LD → HTML basic)
   ↓
   Checks: should_call_ai = force_ai or conf < 4  → TRUE
   ↓
   Checks: if site_type == "WORKDAY_API":
   ↓ YES
   Calls: extract_with_ai_workday_full(clean_text, result)
   [NOTE: Routes to Workday-specific function]
   
4. CLEANER: app/job_detail_engine/utils/cleaner.py
   ↓
   clean_html(html)  [called by orchestrator]
   ↓
   Default: truncate=True (4800 char limit)
   [NOTE: For Workday, this still runs, but truncation is less impactful
    since the AI function receives the full output]
   
5. AI EXTRACTOR (WORKDAY MODE): app/job_detail_engine/ai/extractor.py
   ↓
   extract_with_ai_workday_full(text, known_fields)
   ↓
   ✓ Prints: "[DEBUG] Workday FULL context mode enabled"
   ✓ Prints: "[DEBUG] Description length: {len(text)}"
   ↓
   ✗ SKIPS: filter_content_for_ai()  [NO CHUNKING, NO SCORING]
   ↓
   ✓ Calls: _build_ai_input_workday_full(text, known_fields)
   ↓
   ✓ Sends FULL text to AI with prompt:
     "INPUT (FULL Workday job posting - NO FILTERING APPLIED): {full_text}"
   ↓
   ✓ Uses: _WORKDAY_SYSTEM_PROMPT
     "Extract required_skills comprehensively from the FULL description"
     "Extract every skill, qualification, and requirement you can find"
   ↓
   ✓ Returns: AI extraction with comprehensive skills
   
6. RESULT:
   ↓
   required_skills: ["skill1", "skill2", "skill3", ...]  [COMPREHENSIVE]
   ↓
   Merged back through orchestrator → detail_extractor → routes.py
   ↓
   Returned to user with full skill set

================================================================================
COMPARISON: Standard Mode vs Workday Full Context Mode
================================================================================

STANDARD MODE (GREENHOUSE_API, SIMPLE_API, DOM):
────────────────────────────────────────────────
HTML → clean_html() [4800 chars max]
     → filter_content_for_ai() [splits into chunks]
     → Score each chunk [+1 for relevant, -2 for irrelevant]
     → Keep top 4 chunks [max 3600 chars total]
     → AI extraction [limited context]
     → required_skills [may miss skills in filtered chunks]

WORKDAY FULL CONTEXT MODE (TEST MODE):
─────────────────────────────────────
HTML → clean_html() [4800 chars max]
     → [BYPASSES filter_content_for_ai()]
     → extract_with_ai_workday_full() [receives all 4800 chars]
     → AI extraction [full context]
     → required_skills [comprehensive - all skills from full description]

================================================================================
KEY DIFFERENCES
================================================================================

1. Content Filtering:
   - Standard: filter_content_for_ai() chunks and scores
   - Workday: NO filtering, sends everything

2. Character Limit:
   - Standard: 3600 chars after filtering (top 4 chunks)
   - Workday: 4800 chars (full output from clean_html)

3. AI System Prompt:
   - Standard: "Extract ONLY these fields... Total output must stay under 600 tokens"
   - Workday: "Extract ALL available information... Extract every skill, qualification"

4. AI Input Label:
   - Standard: "INPUT (filtered to relevant job content):"
   - Workday: "INPUT (FULL Workday job posting - NO FILTERING APPLIED):"

5. Debug Logging:
   - Standard: Standard token usage logging
   - Workday: PLUS "[DEBUG] Workday FULL context mode enabled" and description length

6. Timeout:
   - Standard: 90 seconds
   - Workday: 180 seconds (handles larger input)

================================================================================
EXPECTED OUTCOME
================================================================================

If aggressive filtering was causing missing required_skill_set:

✓ Workday jobs should now have comprehensive skill lists
✓ Skills from ALL sections of the job posting will be extracted
✓ No skills missed due to chunk scoring filtering out relevant sections
✓ Token usage will be higher (more input tokens) but results should be better

If filtering was NOT the issue:

✗ required_skill_set will still be empty
✗ The issue lies elsewhere (AI prompt, parsing logic, data availability)
✗ This test mode helps diagnose the root cause

================================================================================
""")
