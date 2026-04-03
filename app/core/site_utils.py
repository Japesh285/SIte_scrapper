from urllib.parse import urljoin, urlparse


def normalize_site_url(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if value.startswith(("http://", "https://")):
        return value
    return f"https://{value}"


def get_domain(value: str) -> str:
    normalized = normalize_site_url(value)
    parsed = urlparse(normalized)
    return parsed.netloc.lower()


def get_origin(value: str) -> str:
    normalized = normalize_site_url(value)
    parsed = urlparse(normalized)
    return f"{parsed.scheme}://{parsed.netloc}"


def absolutize_url(base_url: str, candidate: str) -> str:
    return urljoin(normalize_site_url(base_url), candidate)
