"""Split the latest Ashby output into batches of at most 10 jobs.

Source: <workspace>/discovery/ashby/outputs/YYYY-MM-DD_HHMM_ashby_jobs.json
        (the newest file matching this pattern, not limited to today; batches still use today's date)
Schema: {date, job_count, jobs: [ {company, position, jd, apply_url, source_key}, ... ]}
Output: <workspace>/screening/input_batches/ashby/YYYY-MM-DD_ashby_batch_NNN.json
"""
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repository root
import batch_utils  # noqa: E402
from common import paths  # noqa: E402

PLATFORM = "ashby"
SOURCE_DIR = paths.data_dir("discovery", "ashby", "outputs")
OUTPUT_DIR = paths.data_dir("screening", "input_batches", PLATFORM)
JOB_KEY = "source_key"
SOURCE_GLOB = f"????-??-??_????_{PLATFORM}_jobs.json"  # YYYY-MM-DD_HHMM sorts chronologically


def find_latest_source():
    candidates = sorted(SOURCE_DIR.glob(SOURCE_GLOB))
    if not candidates:
        sys.exit(f"No {PLATFORM} output matching {SOURCE_GLOB} in {SOURCE_DIR}")
    return candidates[-1]


def main():
    today = date.today().isoformat()
    source = find_latest_source()
    if not source.name.startswith(today):
        print(f"note: latest {PLATFORM} source is not from today ({today}): {source.name}")
    data = json.loads(source.read_text(encoding="utf-8"))
    batch_utils.write_and_verify(data["jobs"], PLATFORM, today, source, OUTPUT_DIR, JOB_KEY)


if __name__ == "__main__":
    main()
