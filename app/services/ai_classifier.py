import json
import httpx
from app.core.config import OPENAI_API_KEY
from app.core.logger import logger

SYSTEM_PROMPT = """You are selecting the best scraping strategy.

Choose ONE:
WORKDAY_API, GREENHOUSE_API, DYNAMIC_API, SIMPLE_API, INTERACTIVE_DOM, DOM_LOAD_MORE, DOM_INFINITE_SCROLL, DOM_BROWSER, UNKNOWN

Rules:
- Prefer matched = true
- Prefer higher jobs_found
- Prefer api_usable = true
- Priority:
  WORKDAY_API > GREENHOUSE_API > DYNAMIC_API > SIMPLE_API > INTERACTIVE_DOM > DOM_LOAD_MORE > DOM_INFINITE_SCROLL > DOM_BROWSER
- If none match → UNKNOWN
- Do not guess

Return JSON:
{ "type": "...", "confidence": 0-1 }"""

ALLOWED_TYPES = {
    "WORKDAY_API",
    "GREENHOUSE_API",
    "DYNAMIC_API",
    "SIMPLE_API",
    "INTERACTIVE_DOM",
    "DOM_LOAD_MORE",
    "DOM_INFINITE_SCROLL",
    "DOM_BROWSER",
    "UNKNOWN",
}


async def classify_site(data: dict) -> dict:
    """Use OpenAI to classify the best scraping strategy."""

    if not OPENAI_API_KEY:
        logger.warning("No OPENAI_API_KEY set, falling back to heuristic classification")
        return _heuristic_classify(data)

    user_prompt = json.dumps(data, indent=2)

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPENAI_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "gpt-4.1-mini",
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": 0,
                    "response_format": {"type": "json_object"},
                },
            )
            resp.raise_for_status()
            result = resp.json()
            content = result["choices"][0]["message"]["content"].strip()

            # Parse JSON from response
            if content.startswith("```"):
                content = content.strip("`").split("\n", 1)[-1].rsplit("```", 1)[0]

            parsed = json.loads(content)
            result_type = parsed.get("type", "UNKNOWN")
            confidence = parsed.get("confidence", 0.0)
            if result_type not in ALLOWED_TYPES:
                raise ValueError(f"Unsupported classification type: {result_type}")
            return {
                "type": result_type,
                "confidence": max(0.0, min(float(confidence), 1.0)),
            }

    except Exception as e:
        logger.error(f"OpenAI classification failed: {e}")
        return _heuristic_classify(data)


def _heuristic_classify(data: dict) -> dict:
    """Fallback heuristic classification without AI.

    Priority:
        WORKDAY_API > GREENHOUSE_API > DYNAMIC_API > SIMPLE_API > INTERACTIVE_DOM > DOM
    """
    tests = data.get("tests", {})
    browser_probe = data.get("browser_probe", {})

    dynamic_api = tests.get("dynamic_api", {})

    # ── INTERACTIVE_DOM from browser probe ──────────────────────────
    interactive_dom = tests.get("interactive_dom", {})
    interactive_dom_matched = interactive_dom.get("matched", False)

    ranked_tests = [
        ("WORKDAY_API", tests.get("workday", {})),
        ("GREENHOUSE_API", tests.get("greenhouse", {})),
        ("DYNAMIC_API", dynamic_api),
        ("SIMPLE_API", tests.get("simple_api", {})),
        ("INTERACTIVE_DOM", interactive_dom),
        ("DOM_LOAD_MORE", tests.get("dom_load_more", {})),
        ("DOM_INFINITE_SCROLL", tests.get("dom_infinite_scroll", {})),
        ("DOM_BROWSER", tests.get("dom_browser", {})),
    ]

    # For DYNAMIC_API: support both formats
    #   New async: {"api_usable": bool, "score": int, ...}
    #   Legacy probe: {"best_score": int, "api_usable": bool, ...}
    dynamic_api_score = dynamic_api.get("score", dynamic_api.get("best_score", 0))
    dynamic_api_usable = dynamic_api.get("api_usable", False)

    def _is_viable(site_type: str, result: dict) -> bool:
        if site_type == "DYNAMIC_API":
            return dynamic_api_usable and dynamic_api_score >= 5
        if site_type == "INTERACTIVE_DOM":
            return interactive_dom_matched
        return result.get("matched") and result.get("api_usable")

    viable_tests = [
        (site_type, result)
        for site_type, result in ranked_tests
        if _is_viable(site_type, result)
    ]

    if viable_tests:
        priority = {
            "WORKDAY_API": 7,
            "GREENHOUSE_API": 6,
            "DYNAMIC_API": 5,
            "SIMPLE_API": 4,
            "INTERACTIVE_DOM": 3,
            "DOM_LOAD_MORE": 2,
            "DOM_INFINITE_SCROLL": 1,
            "DOM_BROWSER": 0,
        }
        site_type, result = max(
            viable_tests,
            key=lambda item: (
                item[1].get("confidence", 0),
                item[1].get("jobs_found", 0),
                priority.get(item[0], 0),
            ),
        )
        jobs_found = max(result.get("jobs_found", 0), 1)
        confidence = min(0.99, 0.55 + min(jobs_found, 25) * 0.015)
        return {"type": site_type, "confidence": round(confidence, 2)}

    return {"type": "UNKNOWN", "confidence": 0.0}
