from urllib.parse import urlparse
from app.core.site_utils import get_origin, normalize_site_url
from app.detectors.workday import _build_workday_candidates, _extract_company_tokens

url = "https://ciena.wd5.myworkdayjobs.com/Careers?Location_Country=c4f78be1a8f14da0ab49ce1162348a5e"
html = "{}" # empty html
candidates = _build_workday_candidates(url, html, [])
print("Candidates:", candidates)
