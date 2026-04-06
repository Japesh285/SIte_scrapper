"""Content filtering utility — reduce AI token usage by sending only relevant job content.

Implements:
1. Keyword-based section extraction (keep job-relevant sections, remove junk)
2. Chunk-based filtering (micro-RAG) — split text, score chunks, keep top-N
3. Fallback to first 1500 chars if no relevant content found
"""

import re

from app.core.logger import logger

# ── Keyword Lists ──────────────────────────────────────────────────

RELEVANT_KEYWORDS = [
    "responsibilities",
    "requirements",
    "qualifications",
    "skills",
    "experience",
    "what you will do",
    "what you'll do",
    "what we are looking for",
    "what we're looking for",
    "job summary",
    "job description",
    "about the role",
    "position summary",
    "role description",
    "duties",
    "key responsibilities",
    "main responsibilities",
    "essential duties",
    "required qualifications",
    "minimum qualifications",
    "preferred qualifications",
    "required skills",
    "preferred skills",
    "technical skills",
    "professional experience",
    "work experience",
    "years of experience",
    "education",
    "certifications",
    "competencies",
    "capabilities",
]

IRRELEVANT_KEYWORDS = [
    "about us",
    "who we are",
    "our company",
    "company description",
    "equal employment",
    "eeo",
    "eeo policy",
    "equal opportunity",
    "diversity",
    "inclusion",
    "benefits",
    "perks",
    "compensation and benefits",
    "why work with us",
    "why join us",
    "privacy",
    "cookie",
    "terms of use",
    "contact us",
    "careers page",
    "back to top",
    "apply now",
    "share this job",
    "job alerts",
    "disclaimer",
    "legal notice",
    "accessibility",
]

# ── Public API ─────────────────────────────────────────────────────


def filter_content_for_ai(
    text: str,
    *,
    chunk_size: int = 800,
    max_chunks: int = 5,
    max_total_chars: int = 4500,
) -> dict:
    """Filter raw page text to send only relevant job content to AI.

    Parameters
    ----------
    text : str
        Cleaned plain-text version of the job page.
    chunk_size : int
        Target size for each chunk (characters).  Default 800.
    max_chunks : int
        Maximum number of chunks to return.  Default 5.
    max_total_chars : int
        Hard cap on total characters returned.  Default 4500.

    Returns
    -------
    dict with keys:
        - title: extracted or inferred job title (str)
        - job_summary: first relevant section or description (str)
        - relevant_chunks: list of filtered text chunks (list[str])
        - original_text_length: len(text) (int)
        - filtered_text_length: len(combined chunks) (int)
        - number_of_chunks_selected: len(relevant_chunks) (int)
    """
    original_length = len(text)

    if not text or not text.strip():
        logger.warning("[ContentFilter] Empty text received")
        return {
            "title": "",
            "job_summary": "",
            "relevant_chunks": [],
            "original_text_length": 0,
            "filtered_text_length": 0,
            "number_of_chunks_selected": 0,
        }

    # ── Step 1: Extract job summary / description ──────────────────
    job_summary = _extract_job_summary(text)

    # ── Step 2: Split into chunks and score ────────────────────────
    chunks = _split_into_chunks(text, chunk_size)
    scored_chunks = _score_chunks(chunks)

    # ── Step 3: Keep only relevant chunks ──────────────────────────
    relevant = _keep_relevant_chunks(scored_chunks, max_chunks)

    # ── Step 4: Fallback if nothing relevant ───────────────────────
    if not relevant:
        logger.info("[ContentFilter] No relevant chunks found — using fallback")
        relevant = [text[:1500]]

    # ── Step 5: Trim and deduplicate ───────────────────────────────
    relevant = _trim_and_dedup(relevant, max_total_chars)

    filtered_length = sum(len(c) for c in relevant)

    logger.info(
        "[ContentFilter] original_text_length=%d, filtered_text_length=%d, "
        "number_of_chunks_selected=%d",
        original_length,
        filtered_length,
        len(relevant),
    )

    return {
        "title": "",
        "job_summary": job_summary,
        "relevant_chunks": relevant,
        "original_text_length": original_length,
        "filtered_text_length": filtered_length,
        "number_of_chunks_selected": len(relevant),
    }


# ── Internal Helpers ───────────────────────────────────────────────


def _extract_job_summary(text: str, max_chars: int = 1500) -> str:
    """Extract the job summary / description section from text."""
    summary_keywords = [
        "job summary",
        "job description",
        "about the role",
        "position summary",
        "role overview",
        "summary",
        "description",
    ]

    text_lower = text.lower()

    for kw in summary_keywords:
        idx = text_lower.find(kw)
        if idx != -1:
            start = idx + len(kw)
            newline_pos = text.find("\n", start)
            if newline_pos != -1:
                start = newline_pos + 1
            end = min(start + max_chars, len(text))
            section = text[start:end].strip()
            next_header = _find_next_header(section)
            if next_header != -1:
                section = section[:next_header].strip()
            if section:
                return section[:max_chars]

    fallback = text[:1000].strip()
    return fallback[:max_chars]


def _find_next_header(text: str) -> int:
    """Find the position of the next header-like pattern in text."""
    lines = text.split("\n")
    pos = 0
    for line in lines:
        line_stripped = line.strip()
        if len(line_stripped) > 3:
            is_all_caps = line_stripped.isupper() and len(line_stripped) < 60
            is_title_case = line_stripped.istitle() and len(line_stripped) < 60
            if is_all_caps or is_title_case:
                if not any(kw in line_stripped.lower() for kw in IRRELEVANT_KEYWORDS):
                    return pos
        pos += len(line) + 1
    return -1


def _split_into_chunks(text: str, chunk_size: int) -> list[str]:
    """Split text into chunks of approximately chunk_size characters."""
    if not text:
        return []

    paragraphs = re.split(r"\n\s*\n", text)
    paragraphs = [p.strip() for p in paragraphs if p.strip()]

    chunks = []
    current_chunk = ""

    for para in paragraphs:
        if len(current_chunk) + len(para) <= chunk_size:
            current_chunk += ("\n\n" if current_chunk else "") + para
        else:
            if current_chunk:
                chunks.append(current_chunk)
            if len(para) > chunk_size:
                sub_chunks = _split_paragraph(para, chunk_size)
                chunks.extend(sub_chunks[:-1])
                current_chunk = sub_chunks[-1] if sub_chunks else ""
            else:
                current_chunk = para

    if current_chunk:
        chunks.append(current_chunk)

    return chunks


def _split_paragraph(para: str, chunk_size: int) -> list[str]:
    """Split a large paragraph at sentence boundaries."""
    if len(para) <= chunk_size:
        return [para]

    sentences = re.split(r"(?<=[.!?])\s+", para)
    chunks = []
    current = ""

    for sentence in sentences:
        if len(current) + len(sentence) <= chunk_size:
            current += (" " if current else "") + sentence
        else:
            if current:
                chunks.append(current)
            if len(sentence) > chunk_size:
                for i in range(0, len(sentence), chunk_size):
                    chunks.append(sentence[i : i + chunk_size])
                current = ""
            else:
                current = sentence

    if current:
        chunks.append(current)

    return chunks


def _score_chunks(chunks: list[str]) -> list[tuple[int, str]]:
    """Score each chunk based on relevant/irrelevant keyword matches."""
    scored = []
    for chunk in chunks:
        chunk_lower = chunk.lower()
        score = 0

        for kw in RELEVANT_KEYWORDS:
            if kw in chunk_lower:
                score += 1

        for kw in IRRELEVANT_KEYWORDS:
            if kw in chunk_lower:
                score -= 2

        scored.append((score, chunk))

    return scored


def _keep_relevant_chunks(scored_chunks: list[tuple[int, str]], max_chunks: int) -> list[str]:
    """Keep only chunks with positive scores, sorted by score, limited to max_chunks."""
    positive = [(score, chunk) for score, chunk in scored_chunks if score > 0]

    if not positive:
        return []

    positive.sort(key=lambda x: x[0], reverse=True)
    return [chunk for _, chunk in positive[:max_chunks]]


def _trim_and_dedup(chunks: list[str], max_total_chars: int) -> list[str]:
    """Trim each chunk, remove duplicates, enforce total character limit."""
    if not chunks:
        return []

    trimmed = [c.strip() for c in chunks if c.strip()]

    seen = set()
    unique = []
    for chunk in trimmed:
        if chunk not in seen:
            seen.add(chunk)
            unique.append(chunk)

    unique = [c[:1500] for c in unique]

    result = []
    total = 0
    for chunk in unique:
        if total + len(chunk) <= max_total_chars:
            result.append(chunk)
            total += len(chunk)
        else:
            remaining = max_total_chars - total
            if remaining > 100:
                result.append(chunk[:remaining])
            break

    return result
