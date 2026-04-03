import json
import os
from pathlib import Path
from datetime import datetime
from app.core.logger import logger

# Directory to store raw JSON responses
RAW_JSON_DIR = Path("raw_json")


def save_raw_json(data: dict | list, domain: str, source: str = "scraper") -> str | None:
    """
    Save raw JSON data to a file organized by domain and timestamp.
    
    Args:
        data: The JSON-serializable data to save
        domain: The domain name (used for folder organization)
        source: The source/type of data (e.g., 'scraper', 'api_response')
    
    Returns:
        The file path where data was saved, or None if saving failed
    """
    try:
        # Create domain-specific subdirectory
        domain_dir = RAW_JSON_DIR / domain
        domain_dir.mkdir(parents=True, exist_ok=True)
        
        # Generate filename with timestamp
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{source}_{timestamp}.json"
        file_path = domain_dir / filename
        
        # Write JSON file with indentation for readability
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False, default=str)
        
        logger.info(f"Saved raw JSON to {file_path}")
        return str(file_path)
        
    except Exception as e:
        logger.error(f"Failed to save raw JSON for {domain}: {e}")
        return None


def save_scrape_result(jobs: list[dict], domain: str, site_type: str, metadata: dict = None) -> str | None:
    """
    Save the complete scrape result including jobs and metadata.
    
    Args:
        jobs: List of job dictionaries
        domain: The domain that was scraped
        site_type: The classified site type (WORKDAY_API, GREENHOUSE_API, etc.)
        metadata: Additional metadata about the scrape
    
    Returns:
        The file path where data was saved, or None if saving failed
    """
    result = {
        "domain": domain,
        "site_type": site_type,
        "timestamp": datetime.now().isoformat(),
        "jobs_count": len(jobs),
        "jobs": jobs,
    }
    
    if metadata:
        result["metadata"] = metadata
    
    return save_raw_json(result, domain, source="scrape_result")


def save_api_response(data: dict | list, domain: str, api_type: str) -> str | None:
    """
    Save API response data separately.
    
    Args:
        data: The API response data
        domain: The domain
        api_type: The type of API (e.g., 'greenhouse_api', 'workday_api')
    
    Returns:
        The file path where data was saved, or None if saving failed
    """
    return save_raw_json(data, domain, source=f"{api_type}_response")
