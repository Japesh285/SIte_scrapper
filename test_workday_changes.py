"""
Test script to verify Workday full context mode changes.
This validates the code structure without requiring external dependencies.
"""

import ast
import sys

def check_function_exists(filepath, function_name):
    """Check if a function exists in a Python file."""
    with open(filepath, 'r') as f:
        tree = ast.parse(f.read())
    
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == function_name:
            return True
        if isinstance(node, ast.FunctionDef) and node.name == function_name:
            return True
    return False

def check_function_signature(filepath, function_name, expected_params):
    """Check if a function has the expected parameters."""
    with open(filepath, 'r') as f:
        tree = ast.parse(f.read())
    
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == function_name:
            params = [arg.arg for arg in node.args.args]
            print(f"  ✓ {function_name} found with params: {params}")
            for param in expected_params:
                if param in params:
                    print(f"    ✓ Has '{param}' parameter")
                else:
                    print(f"    ✗ Missing '{param}' parameter")
                    return False
            return True
    print(f"  ✗ {function_name} not found")
    return False

def check_string_in_file(filepath, search_string):
    """Check if a string exists in a file."""
    with open(filepath, 'r') as f:
        content = f.read()
        return search_string in content

print("=" * 80)
print("TESTING: Workday Full Context Mode Changes")
print("=" * 80)

# Test 1: Check orchestrator.py has site_type parameter
print("\n1. Checking orchestrator.py...")
filepath = "app/job_detail_engine/orchestrator.py"
assert check_function_signature(filepath, "extract_job_details", ["html", "force_ai", "site_type"])
assert check_string_in_file(filepath, 'site_type == "WORKDAY_API"')
assert check_string_in_file(filepath, "extract_with_ai_workday_full")
print("  ✓ orchestrator.py has WORKDAY_API routing logic")

# Test 2: Check extractor.py has new Workday function
print("\n2. Checking extractor.py...")
filepath = "app/job_detail_engine/ai/extractor.py"
assert check_function_exists(filepath, "extract_with_ai_workday_full")
assert check_function_exists(filepath, "_build_ai_input_workday_full")
assert check_string_in_file(filepath, "[DEBUG] Workday FULL context mode enabled")
assert check_string_in_file(filepath, "[DEBUG] Description length:")
assert check_string_in_file(filepath, "NO FILTERING APPLIED")
assert check_string_in_file(filepath, "_WORKDAY_SYSTEM_PROMPT")
print("  ✓ extractor.py has Workday full context functions")
print("  ✓ extractor.py has debug logging")

# Test 3: Check cleaner.py has truncate parameter
print("\n3. Checking cleaner.py...")
filepath = "app/job_detail_engine/utils/cleaner.py"
assert check_function_signature(filepath, "clean_html", ["html", "truncate"])
assert check_string_in_file(filepath, "if truncate and len(text) > _MAX_TEXT_CHARS:")
print("  ✓ cleaner.py has truncate parameter")

# Test 4: Check detail_extractor.py passes site_type
print("\n4. Checking detail_extractor.py...")
filepath = "app/services/detail_extractor.py"
assert check_string_in_file(filepath, 'site_type="WORKDAY_API"')
print("  ✓ detail_extractor.py passes site_type to orchestrator")

# Test 5: Verify filter_content_for_ai is NOT called for Workday
print("\n5. Verifying filtering bypass for Workday...")
filepath = "app/job_detail_engine/ai/extractor.py"
with open(filepath, 'r') as f:
    content = f.read()
    # Find the extract_with_ai_workday_full function
    assert "filter_content_for_ai" not in content.split("async def extract_with_ai_workday_full")[1].split("async def ")[0]
    print("  ✓ extract_with_ai_workday_full does NOT call filter_content_for_ai")

print("\n" + "=" * 80)
print("✅ ALL TESTS PASSED!")
print("=" * 80)
print("\nSummary of changes:")
print("1. orchestrator.py: Added site_type param, routes WORKDAY_API to full context mode")
print("2. extractor.py: Added extract_with_ai_workday_full() with NO filtering")
print("3. cleaner.py: Added truncate parameter to skip 4800 char limit")
print("4. detail_extractor.py: Passes site_type='WORKDAY_API' to orchestrator")
print("5. Debug logging: Prints description length and mode enabled")
print("\nExpected behavior:")
print("- Workday jobs: FULL description sent to AI (no chunking, no filtering)")
print("- Other jobs (GREENHOUSE_API, SIMPLE_API, DOM): Existing pipeline unchanged")
print("- required_skill_set should now be populated for Workday jobs")
