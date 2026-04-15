from playwright.sync_api import sync_playwright
from pathlib import Path
import time

INPUT_FILE = "job_urls.txt"
OUTPUT_DIR = Path("job_html")
OUTPUT_DIR.mkdir(exist_ok=True)


def run():
    # read urls
    with open(INPUT_FILE, "r") as f:
        urls = [line.strip() for line in f if line.strip()]

    print(f"Total URLs: {len(urls)}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page = browser.new_page()

        for idx, url in enumerate(urls, start=1):
            print(f"\n[{idx}/{len(urls)}] Visiting: {url}")

            try:
                page.goto(url, timeout=60000)

                # wait for page load
                page.wait_for_load_state("domcontentloaded")
                page.wait_for_timeout(3000)

                # get full HTML
                html = page.content()

                # safe filename
                file_name = f"job_{idx}.html"
                file_path = OUTPUT_DIR / file_name

                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(html)

                print(f"Saved: {file_name}")

                # small delay (anti-bot safety)
                time.sleep(2)

            except Exception as e:
                print(f"Error on {url}: {e}")

        browser.close()

    print("\n✅ All job pages saved.")


if __name__ == "__main__":
    run()