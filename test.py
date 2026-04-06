import requests

url = "https://medtronic.wd1.myworkdayjobs.com/en-US/MedtronicCareers/job/AI-Data-Science-Engineer-II_R61966-1"

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive"
}

session = requests.Session()
res = session.get(url, headers=headers, allow_redirects=True)

print("Final URL:", res.url)
print("Status:", res.status_code)

with open("workday_page.html", "w", encoding="utf-8") as f:
    f.write(res.text)