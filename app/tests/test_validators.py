"""Tests for job extraction validation helpers."""

from app.job_detail_engine.scoring.validators import (
    is_meaningful_string,
    clean_text,
    is_meaningful_description,
    has_valid_list_items,
)

def test_is_meaningful_string():
    # Valid
    assert is_meaningful_string("Software Engineer") is True
    assert is_meaningful_string("Remote") is True
    
    # Invalid types
    assert is_meaningful_string(None) is False
    assert is_meaningful_string(123) is False
    assert is_meaningful_string(["test"]) is False
    
    # Empty or whitespace
    assert is_meaningful_string("") is False
    assert is_meaningful_string("   ") is False
    assert is_meaningful_string("\n\t") is False
    
    # Placeholders
    assert is_meaningful_string("N/A") is False
    assert is_meaningful_string("n/a") is False
    assert is_meaningful_string("None") is False
    assert is_meaningful_string("Unknown") is False
    assert is_meaningful_string("not disclosed") is False

def test_clean_text():
    assert clean_text(None) == ""
    assert clean_text("  hello   world  ") == "hello world"
    assert clean_text("<p>Hello <b>World</b></p>") == "Hello World"
    assert clean_text("Line 1<br>Line 2") == "Line 1 Line 2"
    assert clean_text({"some": "dict"}) == "{'some': 'dict'}"
    assert clean_text(123) == "123"

def test_is_meaningful_description():
    # Valid description (> 20 words, not overly repetitive)
    valid_desc = "We are looking for a software engineer to join our fast paced team. You will be building cutting edge features."
    valid_desc += " " + "Additional text to make it definitely more than 20 words for the sake of passing the test suite cleanly."
    assert is_meaningful_description(valid_desc) is True
    
    # Short description
    short_desc = "We need a software engineer. Period."
    assert is_meaningful_description(short_desc) is False
    
    # Repetitive garbage
    garbage = "hello " * 30
    assert is_meaningful_description(garbage) is False
    
    # HTML garbage that looks long but is empty after cleaning
    html_garbage = "<br>" * 50
    assert is_meaningful_description(html_garbage) is False

def test_has_valid_list_items():
    assert has_valid_list_items(["Python", "Java"]) is True
    assert has_valid_list_items(["", "Python"]) is True  # at least one valid
    
    assert has_valid_list_items([]) is False
    assert has_valid_list_items([""]) is False
    assert has_valid_list_items([None, "  "]) is False
    assert has_valid_list_items(["N/A"]) is False
    assert has_valid_list_items("not a list") is False
