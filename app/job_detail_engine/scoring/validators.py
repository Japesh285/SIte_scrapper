"""Validation helpers for job extraction scoring."""

import re
from typing import Any
from bs4 import BeautifulSoup

def is_meaningful_string(value: Any) -> bool:
    """Check if value is a meaningful string (not empty, not a placeholder)."""
    if not isinstance(value, str):
        return False
    
    cleaned = value.strip().lower()
    if not cleaned:
        return False
        
    invalid_placeholders = {
        "n/a", "none", "null", "unknown", "not disclosed", "unspecified", "tbd"
    }
    if cleaned in invalid_placeholders:
        return False
        
    return True

def clean_text(value: Any) -> str:
    """Safely convert to string, strip HTML tags, normalize whitespace, and trim."""
    if value is None:
        return ""
        
    # Convert non-string to string safely
    if isinstance(value, (dict, list)):
        # For complex types, we might just want to convert them to string representation
        # However, for job parsing, this usually indicates a malformed extraction.
        # But we'll safely convert it.
        text = str(value)
    else:
        text = str(value)
    
    # Strip HTML tags if any
    if "<" in text and ">" in text:
        try:
            # fast parse
            soup = BeautifulSoup(text, "html.parser")
            text = soup.get_text(separator=" ")
        except Exception:
            # fallback
            text = re.sub(r"<[^>]+>", " ", text)
            
    # Normalize whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text

def is_meaningful_description(value: Any) -> bool:
    """Check if description has a minimum meaningful word count and isn't garbage."""
    text = clean_text(value)
    if not text:
        return False
        
    words = text.split()
    
    # Require at least 20 words for a description to be considered meaningful
    if len(words) < 20:
        return False
        
    # Reject repetitive garbage
    # e.g., if less than 30% of words are unique in a text > 20 words
    unique_words = set(w.lower() for w in words)
    if len(unique_words) < len(words) * 0.3:
        return False
        
    return True

def has_valid_list_items(value: Any) -> bool:
    """Ensure value is a list with at least one meaningful string item."""
    if not isinstance(value, list):
        return False
        
    for item in value:
        if is_meaningful_string(item):
            return True
            
    return False
