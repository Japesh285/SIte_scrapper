"""
Full pipeline test — runs orchestrate_scrape() against every URL in Bank Job Data.xlsx
and writes a running markdown report to Full_Pipeline_Test_Report.md.

Usage:
    env/bin/python -u run_full_pipeline_test.py
"""
from __future__ import annotations

import asyncio
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).parent))
sys.stdout.reconfigure(line_buffering=True)

from app.core.config import DATABASE_URL
from app.services.orchestrator import orchestrate_scrape
from app.core.site_utils import get_domain

XLSX_PATH = "/home/japesh/Dev/scrapper/SIte_scrapper/Bank Job Data.xlsx"
URL_COLUMN = "Exact India Jobs Link"
REPORT_PATH = "/home/japesh/Dev/scrapper/SIte_scrapper/Full_Pipeline_Test_Report.md"

engine = create_async_engine(DATABASE_URL)
Session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def load_urls() -> list[str]:
    df = pd.read_excel(XLSX_PATH)
    urls, seen = [], set()
    for val in df[URL_COLUMN].dropna():
        url = str(val).strip()
        if url and url not in seen and url.lower() != url_column_lower:
            seen.add(url)
            urls.append(url)
    return urls


url_column_lower = URL_COLUMN.lower()


def _init_report(total: int) -> None:
    with open(REPORT_PATH, "w") as f:
        f.write(f"# Full Pipeline Test Report\n\n")
        f.write(f"**Run started:** {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n")
        f.write(f"**Total URLs:** {total}\n\n")
        f.write("---\n\n")
        f.write("## Results\n\n")
        f.write("| # | Domain | ATS Type | Jobs | Time | Status | Note |\n")
        f.write("|---|---|---|---|---|---|---|\n")


def _append_row(idx: int, domain: str, ats: str, jobs: int, elapsed: float,
                status: str, note: str) -> None:
    status_icon = "✓" if jobs > 0 else ("⏱" if status == "TIMEOUT" else "✗")
    with open(REPORT_PATH, "a") as f:
        f.write(f"| {idx} | `{domain}` | {ats} | {jobs} | {elapsed:.1f}s | {status_icon} {status} | {note} |\n")


def _write_summary(results: list[dict]) -> None:
    from collections import Counter, defaultdict

    counts = Counter(r["type"] for r in results)
    total_jobs = sum(r["jobs"] for r in results)
    worked = [r for r in results if r["jobs"] > 0]
    failed = [r for r in results if r["jobs"] == 0 and r["type"] not in ("TIMEOUT", "ERROR")]
    timeouts = [r for r in results if r["type"] == "TIMEOUT"]

    with open(REPORT_PATH, "a") as f:
        f.write("\n---\n\n")
        f.write("## Summary\n\n")
        f.write(f"**Run completed:** {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n")
        f.write(f"**Total jobs found:** {total_jobs}\n\n")

        f.write("### By ATS Type\n\n")
        f.write("| ATS Type | Sites | Jobs |\n")
        f.write("|---|---|---|\n")
        for t, c in sorted(counts.items(), key=lambda x: -sum(r["jobs"] for r in results if r["type"] == x[0])):
            tj = sum(r["jobs"] for r in results if r["type"] == t)
            f.write(f"| {t} | {c} | {tj} |\n")

        f.write("\n### Working Sites\n\n")
        for r in sorted(worked, key=lambda x: -x["jobs"]):
            f.write(f"- `{r['domain']}` — **{r['type']}** — {r['jobs']} jobs\n")

        f.write("\n### Failed / 0 Jobs\n\n")
        by_type: dict[str, list] = defaultdict(list)
        for r in failed:
            by_type[r["type"]].append(r)

        for ats_type, rs in sorted(by_type.items()):
            f.write(f"\n**{ats_type}** ({len(rs)} sites):\n")
            for r in rs:
                note = f" — {r['note']}" if r['note'] else ""
                f.write(f"- `{r['domain']}`{note}\n")

        if timeouts:
            f.write("\n### Timeouts\n\n")
            for r in timeouts:
                f.write(f"- `{r['domain']}` — {r['elapsed']:.0f}s\n")


async def run_one(url: str) -> dict:
    async with Session() as session:
        result = await orchestrate_scrape(url, session)
    return result


async def run_all(urls: list[str], start_idx: int = 1) -> list[dict]:
    results = []
    for i, url in enumerate(urls, start_idx):
        domain = get_domain(url) or url
        t0 = time.time()
        try:
            result = await asyncio.wait_for(run_one(url), timeout=120)
            ats = result.get("type", "UNKNOWN")
            jobs = result.get("jobs_found", 0)
            status = result.get("status", "unknown")
            note = ""
        except asyncio.TimeoutError:
            ats, jobs, status, note = "TIMEOUT", 0, "TIMEOUT", "hard_cap_120s"
        except Exception as exc:
            ats, jobs, status, note = "ERROR", 0, "ERROR", str(exc)[:80]

        elapsed = time.time() - t0
        print(f"{i:3d}  [{ats:<20}] {domain:<42} jobs={jobs:>5} t={elapsed:.1f}s{' ' + note if note else ''}")
        _append_row(i, domain, ats, jobs, elapsed, status, note)

        row = {"idx": i, "url": url, "domain": domain, "type": ats,
               "jobs": jobs, "elapsed": elapsed, "status": status, "note": note}
        results.append(row)

    return results


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-from", type=int, default=1,
                        help="1-based URL index to start from (for resuming after crash)")
    args = parser.parse_args()

    urls = load_urls()
    print(f"Loaded {len(urls)} URLs from {XLSX_PATH}")
    print(f"Report: {REPORT_PATH}")
    if args.start_from > 1:
        print(f"Resuming from URL #{args.start_from}")
    print()

    if args.start_from <= 1:
        _init_report(len(urls))
    # When resuming, just append rows to the existing report

    subset = urls[args.start_from - 1:]
    results = asyncio.run(run_all(subset, start_idx=args.start_from))

    print()
    print("=== SUMMARY ===")
    from collections import Counter
    counts = Counter(r["type"] for r in results)
    for t, c in sorted(counts.items(), key=lambda x: -x[1]):
        tj = sum(r["jobs"] for r in results if r["type"] == t)
        print(f"  {t:<22} {c:>3} sites   {tj:>7} jobs")
    print(f"\n  Total: {sum(r['jobs'] for r in results)} jobs")

    _write_summary(results)
    print(f"\nReport written to: {REPORT_PATH}")
