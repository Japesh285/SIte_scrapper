"""HTML cleaning utilities — strip noise and produce readable plain text."""

import re

from bs4 import BeautifulSoup

from app.core.logger import logger

# Section keywords that indicate irrelevant blocks
_IRRELEVANT_SECTIONS = [
    "about us", "who we are", "our company", "company description",
    "equal employment", "eeo", "equal opportunity",
    "diversity", "inclusion",
    "privacy", "cookie", "terms of use", "terms and conditions",
    "disclaimer", "legal notice", "accessibility",
    "contact us", "careers page", "back to top",
    "share this job", "job alerts", "apply now",
]

_MAX_TEXT_CHARS = 4800  # ~1200 tokens


def clean_html(html: str, truncate: bool = True) -> str:
    """Remove script/style tags, strip HTML, normalise whitespace.

    Parameters
    ----------
    html : str
        Raw HTML content to clean.
    truncate : bool
        When True (default), truncates text to _MAX_TEXT_CHARS (4800).
        When False, returns full cleaned text without truncation.
        Set to False for WORKDAY_API full context mode.

    Returns clean plain text suitable for AI or regex parsing.
    """
    if not html or not html.strip():
        return ""

    try:
        soup = BeautifulSoup(html, "html.parser")

        # Remove noisy elements
        for tag in soup(["script", "style", "noscript", "iframe", "nav", "footer", "header"]):
            tag.decompose()

        # Remove blocks containing irrelevant keywords
        for elem in soup.find_all(True):
            text = elem.get_text(strip=True).lower()
            if any(kw in text for kw in _IRRELEVANT_SECTIONS):
                # Only remove if the element itself is a section container
                if elem.name in ("div", "section", "article", "aside", "p", "ul", "ol"):
                    elem.decompose()

        # Get text with minimal structure
        text = soup.get_text(separator="\n", strip=True)
    except Exception as exc:
        logger.warning("[Cleaner] BeautifulSoup fallback failed: %s", exc)
        # Bare-bones regex strip
        text = re.sub(r"<[^>]+>", "\n", html)

    # Normalise whitespace — keep line breaks for section detection
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.strip()

    # Truncate to target <1200 tokens (~4800 chars) ONLY if truncate=True
    if truncate and len(text) > _MAX_TEXT_CHARS:
        text = text[:_MAX_TEXT_CHARS]
        # Trim to last complete line
        last_nl = text.rfind("\n")
        if last_nl > _MAX_TEXT_CHARS * 0.8:
            text = text[:last_nl]

    return text
