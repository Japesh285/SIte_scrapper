"""Test raw JSON saving functionality."""
import json
from pathlib import Path
from app.services.raw_json_saver import save_raw_json, save_scrape_result, RAW_JSON_DIR


def test_save_raw_json():
    """Test saving raw JSON data."""
    test_data = {
        "test": "data",
        "jobs": [
            {"title": "Software Engineer", "location": "Remote", "url": "https://example.com/job1"}
        ]
    }
    
    result = save_raw_json(test_data, "test.com", "test_data")
    assert result is not None, "Failed to save raw JSON"
    
    # Verify file exists and contains correct data
    file_path = Path(result)
    assert file_path.exists(), "File was not created"
    
    with open(file_path, "r") as f:
        saved_data = json.load(f)
    
    assert saved_data == test_data, "Saved data doesn't match original"
    print(f"✓ Raw JSON saved successfully to: {result}")
    
    # Clean up
    file_path.unlink()
    print("✓ Test passed!")


def test_save_scrape_result():
    """Test saving complete scrape result."""
    jobs = [
        {"title": "Engineer", "location": "NYC", "url": "https://example.com/1"},
        {"title": "Designer", "location": "LA", "url": "https://example.com/2"},
    ]
    
    metadata = {"url": "https://example.com/careers", "confidence": 0.95}
    
    result = save_scrape_result(jobs, "example.com", "GREENHOUSE_API", metadata)
    assert result is not None, "Failed to save scrape result"
    
    file_path = Path(result)
    assert file_path.exists(), "Scrape result file was not created"
    
    with open(file_path, "r") as f:
        saved_data = json.load(f)
    
    assert saved_data["domain"] == "example.com"
    assert saved_data["site_type"] == "GREENHOUSE_API"
    assert saved_data["jobs_count"] == 2
    assert len(saved_data["jobs"]) == 2
    assert "metadata" in saved_data
    print(f"✓ Scrape result saved successfully to: {result}")
    
    # Clean up
    file_path.unlink()
    print("✓ Test passed!")


if __name__ == "__main__":
    print("Testing raw JSON save functionality...")
    test_save_raw_json()
    print("\nTesting scrape result save functionality...")
    test_save_scrape_result()
