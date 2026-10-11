#!/usr/bin/env python3
"""Stage 5: queue finished resumes in application_list.json for the browser apply sessions.

pipeline_tracker.csv (final_status == DONE, pdf_pages == 1) + resume.pdf on disk
-> one new `status: TODO` row per job, numbered after the current max `n`.

Never changes existing rows. A job is skipped when its job_folder is already listed, or when
the same normalized company + position is already listed (the same job from another source,
e.g. jobs.ca vs LinkedIn). `ats` is only a guess from the apply_url host; the apply session
corrects it. Before writing, the old list is copied to
.runtime/application_list.json.bak_<YYYYMMDD_HHMMSS>; if the list changed on disk while this
ran (an apply session saved it), nothing is written.

Usage:
    python3 05_enqueue_applications.py                      # newest batch in the tracker
    python3 05_enqueue_applications.py --batch 2026-10-07_0008 [--batch ...]
    python3 05_enqueue_applications.py --dry-run

Progress protocol:
    [ADD|SKIP|NOT_READY] Company — Position [| detail]
    DONE exit=<code>                                  (always last)
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent  # resume/


def _load_stage1():
    spec = importlib.util.spec_from_file_location("prepare_jobs", SCRIPT_DIR / "01_prepare_jobs.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


p01 = _load_stage1()

DEFAULT_JOBS_ROOT = p01.DEFAULT_OUTPUT_ROOT
DEFAULT_APPLICATION_LIST = p01.DEFAULT_APPLICATION_LIST
BACKUP_DIR = p01.DATA_ROOT / ".runtime"

# (host suffix or URL fragment, ats label). First match wins; unknown hosts -> None.
ATS_RULES = (
    ("linkedin.com/jobs/view", "LinkedIn Easy Apply"),
    ("myworkdayjobs.com", "Workday"),
    ("taleo.net", "Taleo"),
    ("greenhouse.io", "Greenhouse"),
    ("grnh.se", "Greenhouse"),
    ("lever.co", "Lever"),
    ("ashbyhq.com", "Ashby"),
    ("workable.com", "Workable"),
    ("applytojob.com", "JazzHR"),
    ("icims.com", "iCIMS"),
    ("successfactors", "SAP SuccessFactors"),
    ("avature.net", "Avature"),
    ("bamboohr.com", "BambooHR"),
    ("ziprecruiter.com", "ZipRecruiter"),
    ("oraclecloud.com", "Oracle HCM"),
    ("inforcloudsuite.com", "Infor CloudSuite HCM"),
    ("zohorecruit", "Zoho Recruit"),
    ("ultipro.", "UKG Pro Recruiting (UltiPro)"),
    ("rippling.com", "Rippling"),
    ("smartrecruiters.com", "SmartRecruiters"),
)


class EnqueueError(Exception):
    """Any failure that should abort the run with exit code 1."""


# ------------------------------------------------------------------ matching
# Shared with 01 so its pre-skip and this check agree.
norm_text = p01.norm_text
job_key = p01.job_key


def guess_ats(apply_url: str) -> str | None:
    u = urlparse(apply_url or "")
    target = f"{u.netloc.lower()}{u.path}"
    return next((label for frag, label in ATS_RULES if frag in target), None)


# ------------------------------------------------------------------ planning

def load_list(path: Path) -> tuple[bytes, dict]:
    if not path.is_file():
        raise EnqueueError(f"application list not found: {path}")
    raw = path.read_bytes()
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise EnqueueError(f"application list is not valid UTF-8 JSON: {e}") from e
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
        raise EnqueueError("application list root must be an object with a 'jobs' list")
    return raw, data


def plan(rows: list[dict], batches: list[str], app: dict, jobs_root: Path,
         log=print) -> list[dict]:
    listed_folders = {j.get("job_folder") for j in app["jobs"]}
    listed_keys = {job_key(j.get("company", ""), j.get("position", "")): j for j in app["jobs"]}
    next_n = max((j.get("n", 0) for j in app["jobs"]), default=0) + 1
    new = []
    for row in rows:
        if row["batch"] not in batches:
            continue
        label = f"{row['company']} — {row['position']}"
        folder = jobs_root / row["batch"] / row["job_folder"]
        pdf = folder / "resume.pdf"
        if str(folder) in listed_folders:
            log(f"[SKIP] {label} | already listed")
            continue
        if row["final_status"] != "DONE" or row["pdf_pages"] != "1":
            log(f"[NOT_READY] {label} | final_status={row['final_status'] or '-'} "
                f"pdf_pages={row['pdf_pages'] or '-'}")
            continue
        if not pdf.is_file():
            log(f"[NOT_READY] {label} | tracker DONE but resume.pdf missing")
            continue
        key = job_key(row["company"], row["position"])
        if key in listed_keys:
            hit = listed_keys[key]
            log(f"[SKIP] {label} | same job as #{hit.get('n')} {hit.get('status')}")
            continue
        entry = {
            "n": next_n, "batch": row["batch"], "job_folder": str(folder),
            "company": row["company"], "position": row["position"], "apply_url": row["apply_url"],
            "resume_pdf": str(pdf), "ats": guess_ats(row["apply_url"]),
            "status": "TODO", "applied_at": None, "notes": "",
        }
        new.append(entry)
        listed_folders.add(str(folder))
        listed_keys[key] = entry
        next_n += 1
        log(f"[ADD] {label} | #{entry['n']} ats={entry['ats']}")
    return new


# ------------------------------------------------------------------- writing

def write_list(path: Path, original: bytes, data: dict, backup_dir: Path, now: datetime) -> Path:
    if path.read_bytes() != original:
        raise EnqueueError(f"{path.name} changed on disk while planning (apply session?); "
                           "nothing written, run again")
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / f"{path.name}.bak_{now:%Y%m%d_%H%M%S}"
    shutil.copy2(path, backup)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)  # same layout as the hand-edited file
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    return backup


def run(args: argparse.Namespace, log=print) -> int:
    jobs_root = Path(args.jobs_root).expanduser().resolve()
    tracker = jobs_root / p01.TRACKER_NAME
    try:
        rows = p01.read_tracker(tracker)
    except p01.PrepareError as e:
        raise EnqueueError(str(e)) from e
    if not rows:
        raise EnqueueError(f"tracker is empty or missing: {tracker}")
    known = sorted({r["batch"] for r in rows})
    batches = args.batch or [known[-1]]
    unknown = [b for b in batches if b not in known]
    if unknown:
        raise EnqueueError(f"batch not in tracker: {', '.join(unknown)}")

    app_path = Path(args.application_list).expanduser().resolve()
    original, app = load_list(app_path)
    log(f"Batches: {', '.join(batches)}")
    new = plan(rows, batches, app, jobs_root, log=log)
    log(f"\nTo add: {len(new)}")
    if args.dry_run or not new:
        log("DRY RUN (nothing written)" if args.dry_run else "Nothing to add")
        return 0
    app["jobs"].extend(new)
    backup = write_list(app_path, original, app, Path(args.backup_dir), datetime.now())
    log(f"Wrote {app_path} (#{new[0]['n']}–#{new[-1]['n']})")
    log(f"Backup: {backup}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Queue finished resumes in application_list.json.")
    ap.add_argument("--batch", action="append",
                    help="batch name (repeatable; default: newest batch in the tracker)")
    ap.add_argument("--jobs-root", default=str(DEFAULT_JOBS_ROOT))
    ap.add_argument("--application-list", default=str(DEFAULT_APPLICATION_LIST))
    ap.add_argument("--backup-dir", default=str(BACKUP_DIR))
    ap.add_argument("--dry-run", action="store_true", help="show planned rows without writing")
    args = ap.parse_args()
    try:
        code = run(args)
    except EnqueueError as e:
        print(f"FAILED: {e}")
        code = 1
    except Exception as e:  # unexpected: still fail loudly
        print(f"FAILED: unexpected error: {type(e).__name__}: {e}")
        code = 1
    print(f"\nDONE exit={code}")
    return code


if __name__ == "__main__":
    sys.exit(main())
