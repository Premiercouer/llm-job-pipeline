#!/usr/bin/env python3
"""Stage 1: split a Job Match output JSON into per-job workspaces.

Reads one upstream file (`YYYY-MM-DD_HHMM_jobs.json`), validates it, and
creates a new batch folder under the jobs root containing one folder per job,
each holding a single `job.json` with the five downstream fields.
Also appends one row per created job to `<jobs root>/pipeline_tracker.csv`.

Jobs whose normalized company + position is already in application_list.json
(any status), or repeated earlier in the same input, are skipped before any
folder is created, so 02-04 never spend Resume V1 / quality-review work on them.
05 uses the same job_key() for its own duplicate check.

Usage:
    python3 01_prepare_jobs.py                       # newest upstream JSON
    python3 01_prepare_jobs.py --input /abs/file.json
    python3 01_prepare_jobs.py --dry-run
    python3 01_prepare_jobs.py --include-listed      # do not skip listed jobs
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import sys
import unicodedata
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repository root
from common import candidate, paths, worker_dir  # noqa: E402

DATA_ROOT = paths.data_dir("resume")  # job folders, tracker, handoffs, telemetry, queue
DEFAULT_INPUT_DIR = paths.data_dir("screening", "output")  # screening handoff (*_jobs.json)
DEFAULT_OUTPUT_ROOT = DATA_ROOT / "jobs"
DEFAULT_APPLICATION_LIST = DATA_ROOT / "application_list.json"

INPUT_NAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}_\d{4})_jobs\.json$")
JOB_FIELDS = ("company", "position", "jd", "apply_url", "source_key")
MAX_NAME_PART = 60  # keep folder names well under filesystem limits
COMPANY_SUFFIX_RE = re.compile(r"\b(inc|incorporated|ltd|limited|llc|llp|ulc|plc|corp|corporation)$")

TRACKER_NAME = "pipeline_tracker.csv"
TRACKER_ENCODING = "utf-8-sig"  # UTF-8 + BOM so Excel shows Unicode correctly
TRACKER_COLUMNS = (
    "batch", "job_folder", "company", "position", "apply_url",
    "v1_status", "review_status", "hard_missing", "soft_missing",
    "final_status", "pdf_pages", "apply_status", "applied_at",
    "last_error", "notes", "updated_at",
)


class PrepareError(Exception):
    """Any failure that should abort the run with exit code 1."""


# ---------------------------------------------------------------- discovery

def find_latest_input(input_dir: Path) -> Path:
    if not input_dir.is_dir():
        raise PrepareError(f"input directory not found: {input_dir}")
    candidates = []
    for p in input_dir.iterdir():
        m = INPUT_NAME_RE.match(p.name)
        if not m or not p.is_file():
            continue
        try:
            stamp = datetime.strptime(m.group(1), "%Y-%m-%d_%H%M")
        except ValueError:
            continue  # e.g. 2026-13-40_9999_jobs.json
        candidates.append((stamp, p))
    if not candidates:
        raise PrepareError(
            f"no files matching YYYY-MM-DD_HHMM_jobs.json in {input_dir}")
    candidates.sort(key=lambda c: c[0])
    return candidates[-1][1]


# --------------------------------------------------------------- validation

def load_and_validate(path: Path) -> list[dict]:
    if not path.is_file():
        raise PrepareError(f"input file not found: {path}")
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise PrepareError(f"input is not valid UTF-8 JSON: {e}") from e

    if not isinstance(data, dict):
        raise PrepareError("root must be a JSON object")
    if "jobs" not in data:
        raise PrepareError("missing 'jobs'")
    jobs = data["jobs"]
    if not isinstance(jobs, list):
        raise PrepareError("'jobs' must be a list")
    if "job_count" not in data:
        raise PrepareError("missing 'job_count'")
    count = data["job_count"]
    if not isinstance(count, int) or isinstance(count, bool):
        raise PrepareError("'job_count' must be an integer")
    if count != len(jobs):
        raise PrepareError(
            f"job_count ({count}) does not match len(jobs) ({len(jobs)})")

    errors = []
    for i, job in enumerate(jobs):
        if not isinstance(job, dict):
            errors.append(f"jobs[{i}] is not an object")
            continue
        for field in JOB_FIELDS:
            if field not in job:
                errors.append(f"jobs[{i}] missing '{field}'")
            elif not isinstance(job[field], str) or not job[field].strip():
                errors.append(f"jobs[{i}].{field} must be a non-empty string")
    if errors:
        raise PrepareError("validation failed:\n  " + "\n  ".join(errors))
    return jobs


# ---------------------------------------------------------- duplicate check
# Same normalization as linkedin/scripts/ctl.py job_key(); keep the two in step.
# 05_enqueue_applications.py imports these.

def norm_text(text: str) -> str:
    s = unicodedata.normalize("NFKD", text or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).casefold()
    s = s.replace("&", " and ").replace("'", "").replace("’", "")
    return " ".join(re.sub(r"[^\w]+", " ", s).split())


def job_key(company: str, position: str) -> tuple[str, str]:
    c = norm_text(company)
    while True:
        stripped = COMPANY_SUFFIX_RE.sub("", c).strip()
        if stripped == c or not stripped:
            break
        c = stripped
    return c, norm_text(position)


def load_listed_keys(path: Path) -> dict[tuple[str, str], dict]:
    """job_key -> application_list row, for every listed job (any status)."""
    if not path.is_file():
        raise PrepareError(f"application list not found: {path} "
                           "(use --include-listed to skip this check)")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise PrepareError(f"application list is not valid UTF-8 JSON: {e}") from e
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
        raise PrepareError("application list root must be an object with a 'jobs' list")
    return {job_key(j.get("company", ""), j.get("position", "")): j
            for j in data["jobs"] if isinstance(j, dict)}


def drop_listed(jobs: list[dict], listed: dict) -> tuple[list[dict], list[str]]:
    """Remove jobs already in the application list or repeated in the input."""
    kept, skipped, seen = [], [], {}
    for i, job in enumerate(jobs):
        key = job_key(job["company"], job["position"])
        label = f"{job['company']} — {job['position']}"
        if key in listed:
            hit = listed[key]
            skipped.append(f"{label} | same job as #{hit.get('n')} {hit.get('status')}")
        elif key in seen:
            skipped.append(f"{label} | same job as jobs[{seen[key]}] in this input")
        else:
            seen[key] = i
            kept.append(job)
    return kept, skipped


# ------------------------------------------------------------------ naming

def sanitize_name_part(text: str, fallback: str) -> str:
    s = text.strip()
    s = re.sub(r"[^\w.\-]+", "_", s)   # spaces, / \ : * ? " < > | ( ) , & ...
    s = re.sub(r"_+", "_", s)
    s = s[:MAX_NAME_PART].strip("._-")
    return s or fallback


def short_id(source_key: str) -> str:
    return hashlib.sha256(source_key.encode("utf-8")).hexdigest()[:8]


def job_folder_name(job: dict) -> str:
    return "__".join((
        sanitize_name_part(job["position"], "Position"),
        sanitize_name_part(job["company"], "Company"),
        short_id(job["source_key"]),
    ))


def plan_job_folders(jobs: list[dict]) -> list[str]:
    names = [job_folder_name(job) for job in jobs]
    # macOS filesystems are usually case-insensitive, so compare casefolded.
    seen: dict[str, int] = {}
    collisions = []
    for i, name in enumerate(names):
        key = name.casefold()
        if key in seen:
            collisions.append(f"jobs[{seen[key]}] and jobs[{i}] -> {name}")
        else:
            seen[key] = i
    if collisions:
        raise PrepareError(
            "folder name collision:\n  " + "\n  ".join(collisions))
    return names


def next_batch_name(output_root: Path, now: datetime) -> str:
    base = now.strftime("%Y-%m-%d_%H%M")
    name, n = base, 1
    while (output_root / name).exists():
        n += 1
        name = f"{base}_{n:02d}"
    return name


# ----------------------------------------------------------------- writing

def write_json_atomic(path: Path, obj: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def create_batch_dir(output_root: Path, now: datetime) -> Path:
    output_root.mkdir(parents=True, exist_ok=True)
    base = now.strftime("%Y-%m-%d_%H%M")
    for n in range(1, 100):
        name = base if n == 1 else f"{base}_{n:02d}"
        path = output_root / name
        try:
            path.mkdir()  # atomic: fails if it already exists, never overwrites
            return path
        except FileExistsError:
            continue
    raise PrepareError(f"too many batches for {base} in {output_root}")


def write_batch(batch_dir: Path, jobs: list[dict], names: list[str]) -> None:
    for i, (job, name) in enumerate(zip(jobs, names)):
        stage = "mkdir"
        try:
            job_dir = batch_dir / name
            job_dir.mkdir()
            stage = "write job.json"
            write_json_atomic(job_dir / "job.json",
                              {field: job[field] for field in JOB_FIELDS})
        except OSError as e:
            raise PrepareError(
                f"{stage} failed for jobs[{i}] ({name}): {e}") from e


# ----------------------------------------------------------------- tracker
# Reusable by later stages: read_tracker -> modify rows -> write_tracker_atomic.

def validate_tracker_header(header: list[str] | None, tracker: Path) -> None:
    if header is None or tuple(header) != TRACKER_COLUMNS:
        raise PrepareError(
            f"tracker header does not match expected schema: {tracker}\n"
            f"  expected: {','.join(TRACKER_COLUMNS)}\n"
            f"  found:    {','.join(header or [])}")


def read_tracker(tracker: Path) -> list[dict]:
    """Return existing rows ([] if the file is absent); fail on bad schema."""
    if not tracker.exists():
        return []
    if not tracker.is_file():
        raise PrepareError(f"tracker path is not a file: {tracker}")
    try:
        with tracker.open("r", encoding=TRACKER_ENCODING, newline="") as f:
            reader = csv.DictReader(f)
            validate_tracker_header(reader.fieldnames, tracker)
            rows = []
            for row in reader:
                if None in row:  # more cells than columns
                    raise PrepareError(
                        f"tracker line {reader.line_num} has extra cells: "
                        f"{tracker}")
                rows.append({k: v or "" for k, v in row.items()})
            return rows
    except (OSError, UnicodeDecodeError, csv.Error) as e:
        raise PrepareError(f"cannot read tracker {tracker}: {e}") from e


def write_tracker_atomic(tracker: Path, rows: list[dict]) -> None:
    """Write header + all rows to a temp file, then replace the tracker."""
    tmp = tracker.with_name(tracker.name + ".tmp")
    created = False
    try:
        with tmp.open("w", encoding=TRACKER_ENCODING, newline="") as f:
            created = True
            writer = csv.DictWriter(f, fieldnames=TRACKER_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, tracker)
    except BaseException:
        if created:  # never remove a .tmp this call did not write
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
        raise


def build_initial_tracker_rows(batch: str, jobs: list[dict],
                               names: list[str]) -> list[dict]:
    updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows = []
    for job, name in zip(jobs, names):
        row = dict.fromkeys(TRACKER_COLUMNS, "")
        row.update(
            batch=batch, job_folder=name, company=job["company"],
            position=job["position"], apply_url=job["apply_url"],
            v1_status="PENDING", review_status="PENDING",
            final_status="PENDING", apply_status="NOT_APPLIED",
            updated_at=updated_at,
        )
        rows.append(row)
    return rows


def append_tracker_rows(tracker: Path, new_rows: list[dict]) -> None:
    """Append rows as one transaction; fail on duplicate batch+job_folder."""
    existing = read_tracker(tracker)
    seen = {(r["batch"], r["job_folder"]) for r in existing}
    dupes = [f"{r['batch']}/{r['job_folder']}" for r in new_rows
             if (r["batch"], r["job_folder"]) in seen]
    if dupes:
        raise PrepareError(
            "tracker already has rows for:\n  " + "\n  ".join(dupes))
    try:
        write_tracker_atomic(tracker, existing + new_rows)
    except OSError as e:
        raise PrepareError(f"tracker write failed ({tracker}): {e}") from e


# -------------------------------------------------------------------- main

def run(args: argparse.Namespace) -> None:
    input_path = (Path(args.input).expanduser().resolve() if args.input
                  else find_latest_input(Path(args.input_dir)))
    jobs = load_and_validate(input_path)
    total = len(jobs)
    skipped: list[str] = []
    if not args.include_listed:
        listed = load_listed_keys(Path(args.application_list).expanduser().resolve())
        jobs, skipped = drop_listed(jobs, listed)
    names = plan_job_folders(jobs)
    output_root = Path(args.output_root).expanduser().resolve()
    tracker = output_root / TRACKER_NAME
    read_tracker(tracker)  # validate schema before creating anything
    now = datetime.now()

    if args.dry_run:
        batch_name = next_batch_name(output_root, now)
        print("Prepare Jobs DRY RUN (nothing written)\n")
        print(f"Input: {input_path}")
        print(f"Jobs: {total} | skipped {len(skipped)} | to prepare {len(jobs)}")
        print_skipped(skipped)
        print(f"Output: {output_root / batch_name}")
        print(f"Tracker: {tracker}")
        print(f"Rows to add: {len(names)}\n")
        print("Planned job folders:")
        for name in names:
            print(f"- {name}/job.json")
        return

    if not jobs:
        print("Prepare Jobs: nothing to prepare\n")
        print(f"Input: {input_path}")
        print(f"Jobs: {total} | skipped {len(skipped)} | to prepare 0")
        print_skipped(skipped)
        print("No batch folder or tracker rows created.")
        return

    batch_dir = create_batch_dir(output_root, now)
    try:
        write_batch(batch_dir, jobs, names)
        rows = build_initial_tracker_rows(batch_dir.name, jobs, names)
        append_tracker_rows(tracker, rows)
    except BaseException:
        # Only remove the batch folder this run created.
        shutil.rmtree(batch_dir, ignore_errors=True)
        raise

    print("Prepare Jobs complete\n")
    print(f"Input: {input_path}")
    print(f"Jobs: {total} | skipped {len(skipped)} | prepared {len(jobs)}")
    print_skipped(skipped)
    print(f"Output: {batch_dir}")
    print(f"Tracker: {tracker}\n")
    print("Created:")
    print(f"- {len(names)} job folders")
    print(f"- {len(names)} job.json files")
    print(f"- {len(rows)} tracker rows")


def print_skipped(skipped: list[str]) -> None:
    for line in skipped:
        print(f"[SKIP] {line}")
    if skipped:
        print()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Split a Job Match output JSON into per-job workspaces.")
    parser.add_argument("--input", help="explicit upstream JSON file "
                        "(default: newest YYYY-MM-DD_HHMM_jobs.json)")
    parser.add_argument("--input-dir", default=str(DEFAULT_INPUT_DIR),
                        help="directory scanned when --input is omitted")
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT),
                        help="root folder for batch workspaces")
    parser.add_argument("--application-list", default=str(DEFAULT_APPLICATION_LIST),
                        help="jobs already listed here (any status) are skipped")
    parser.add_argument("--include-listed", action="store_true",
                        help="do not skip jobs already in the application list")
    parser.add_argument("--dry-run", action="store_true",
                        help="validate and show planned folders without writing")
    args = parser.parse_args()

    try:
        run(args)
    except PrepareError as e:
        print(f"FAILED: {e}")
        print("DONE exit=1")
        return 1
    except Exception as e:  # unexpected: still fail loudly
        print(f"FAILED: unexpected error: {type(e).__name__}: {e}")
        print("DONE exit=1")
        return 1
    print("\nDONE exit=0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
