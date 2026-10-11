#!/usr/bin/env python3
"""Stage 3: Resume Quality Review for the jobs listed in a 02 handoff manifest.

v1_ready.json -> validate manifest / job.json / tracker -> CHECKPOINT or REVIEW
-> per job, serially, through scoring.ReviewEngine with the selected provider:
     READY -> INPUT_PREPARED -> SUBMISSION_INTENT_RECORDED (fsync) -> SUBMITTED
     -> RESULT_DETECTED -> RESULT_VALIDATED -> job.json + tracker -> COMMITTED
-> feedback_ready.json for stage 4 (jobs with a valid quality_feedback only).

Providers (--provider, default $LJP_SCORER or "mock"):
    mock              offline keyword coverage (scoring/mock.py); CI and demos
    manual            request files for a human reviewer (--inbox), collected on a later run
    module:factory    any ResumeScorer implementation on PYTHONPATH (e.g. a private adapter)

Fail closed: a job whose submission cannot be proven or whose report fails validation is
MANUAL_RECOVERY_REQUIRED and is never resubmitted automatically. A provider that is out of
quota / unavailable stops the run (exit 3) and writes <handoff>/<batch>/review_blocked.json;
stage 4 refuses that batch until stage 3 is rerun after the operator acted.

No Claude worker: stage 3 makes 0 model calls. Per-job journals:
<workspace>/resume/.runtime/review_journal/<batch>/<job_folder>.json. Operational events:
<workspace>/resume/.runtime/telemetry/review_events.jsonl (counters and states only).

Tracker columns owned by this stage: review_status, hard_missing, soft_missing, last_error,
updated_at.

Usage:
    python3 03_review_resumes.py                       # newest <batch>/v1_ready.json, mock provider
    python3 03_review_resumes.py --input /abs/.../v1_ready.json --provider manual --inbox /abs/inbox
    python3 03_review_resumes.py --limit 1 | --dry-run
    python3 03_review_resumes.py --batch B --resolve-not-submitted FOLDER --note "checked provider"
    python3 03_review_resumes.py --batch B --adopt-report FOLDER report.json --note "from provider UI"

Exit codes: 0 ok | 1 some job not done (see FAILED lines) | 2 usage | 3 provider unavailable.

Progress protocol (line-buffered):
    [DONE|FAILED|SKIPPED] Company — Position [| detail]
    PROGRESS REVIEW i/N | DONE d FAILED f SKIPPED s
    DONE exit=<code>                                  (always last)
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))


def _load_stage2():
    """Reuse 02's batch naming, Resume V1 validator and helpers (and 01 via 02)."""
    spec = importlib.util.spec_from_file_location("generate_v1", SCRIPT_DIR / "02_generate_v1.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # needed by @dataclass in 02
    spec.loader.exec_module(module)
    return module


p02 = _load_stage2()
p01 = p02.p01

from scoring import engine as review  # noqa: E402  (repository root is on sys.path via 02)
from scoring.models import QualityReport, ResumeDocument, ReviewJob  # noqa: E402
from scoring.providers import load_provider  # noqa: E402

ROOT = p02.ROOT
DEFAULT_JOBS_ROOT = p02.DEFAULT_JOBS_ROOT
HANDOFF_ROOT = p02.HANDOFF_ROOT
HANDOFF_NAME = p02.HANDOFF_NAME
FEEDBACK_NAME = "feedback_ready.json"
REVIEW_BLOCKED_MARKER = "review_blocked.json"  # in <handoff>/<batch>/; 04 refuses the batch
JOURNAL_ROOT = p02.DATA_ROOT / ".runtime" / "review_journal"
EVENTS_PATH = p02.DATA_ROOT / ".runtime" / "telemetry" / "review_events.jsonl"

REQUIRED_FIELDS = ("company", "position", "jd", "apply_url", "source_key", "resume_v1")
OWNED_COLUMNS = ("review_status", "hard_missing", "soft_missing", "last_error", "updated_at")
DONE_OUTCOMES = (review.COMMITTED, review.ALREADY_COMMITTED)

validate_feedback = review.validate_feedback


class PipelineError(Exception):
    """Fail-closed error for the whole run (before any provider call)."""


class JobError(Exception):
    """Failure of a single job."""


# ---------------------------------------------------------------- manifest

def find_latest_manifest() -> Path:
    if not HANDOFF_ROOT.is_dir():
        raise PipelineError(f"handoff root not found: {HANDOFF_ROOT}")
    found = [(key, d / HANDOFF_NAME) for d in HANDOFF_ROOT.iterdir()
             if d.is_dir() and (key := p02.batch_sort_key(d.name)) is not None
             and (d / HANDOFF_NAME).is_file()]
    if not found:
        raise PipelineError(f"no <batch>/{HANDOFF_NAME} under {HANDOFF_ROOT}")
    return max(found, key=lambda f: f[0])[1]


def _nonempty_str(v) -> bool:
    return isinstance(v, str) and bool(v.strip())


def load_manifest(path: Path, jobs_root: Path) -> tuple[str, list[dict]]:
    """Validate the manifest; return (batch, entries). Read-only."""
    if not path.is_file():
        raise PipelineError(f"manifest not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise PipelineError(f"manifest unreadable: {e}") from e

    if not isinstance(data, dict):
        raise PipelineError("manifest root must be a JSON object")
    missing = [k for k in ("batch", "generated_at", "job_count", "jobs") if k not in data]
    if missing:
        raise PipelineError(f"manifest missing: {', '.join(missing)}")
    batch, count, jobs = data["batch"], data["job_count"], data["jobs"]
    if not isinstance(batch, str) or p02.batch_sort_key(batch) is None:
        raise PipelineError(f"manifest batch is not a valid batch name: {batch!r}")
    if not _nonempty_str(data["generated_at"]):
        raise PipelineError("manifest generated_at must be a non-empty string")
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        raise PipelineError("manifest job_count must be an integer >= 0")
    if not isinstance(jobs, list):
        raise PipelineError("manifest jobs must be a list")
    if count != len(jobs):
        raise PipelineError(f"manifest job_count ({count}) != len(jobs) ({len(jobs)})")

    errors, folders, paths = [], set(), set()
    batch_dir = (jobs_root / batch).resolve()
    for i, item in enumerate(jobs):
        where = f"jobs[{i}]"
        if not isinstance(item, dict):
            errors.append(f"{where} is not an object")
            continue
        folder, job_json = item.get("job_folder"), item.get("job_json")
        if not _nonempty_str(folder) or not _nonempty_str(job_json):
            errors.append(f"{where}: job_folder and job_json must be non-empty strings")
            continue
        if folder in (".", "..") or "/" in folder or "\\" in folder:
            errors.append(f"{where}: job_folder is not a plain folder name: {folder!r}")
            continue
        if not os.path.isabs(job_json):
            errors.append(f"{where}: job_json is not an absolute path")
            continue
        expected = (batch_dir / folder / "job.json").resolve()
        actual = Path(job_json).resolve()
        if actual != expected:
            errors.append(f"{where}: job_json must be {expected}, got {actual}")
            continue
        if not actual.is_file():
            errors.append(f"{where}: job.json does not exist: {actual}")
            continue
        if folder in folders or str(actual) in paths:
            errors.append(f"{where}: duplicate job_folder/job_json {folder}")
            continue
        folders.add(folder)
        paths.add(str(actual))
    if errors:
        raise PipelineError("manifest validation failed:\n  " + "\n  ".join(errors))
    return batch, jobs


# ------------------------------------------------------------- job state

@dataclass
class Job:
    folder: str
    path: Path
    data: dict
    has_valid_result: bool

    @property
    def label(self) -> str:
        return f"{self.data['company']} — {self.data['position']}"


def load_jobs(entries: list[dict]) -> list[Job]:
    """Re-read every manifest job.json before any review (state verification)."""
    jobs, errors = [], []
    for item in entries:
        folder, path = item["job_folder"], Path(item["job_json"])
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
            errors.append(f"{folder}: job.json unreadable ({e})")
            continue
        if not isinstance(data, dict):
            errors.append(f"{folder}: job.json root is not an object")
            continue
        bad = [f for f in REQUIRED_FIELDS if not _nonempty_str(data.get(f))]
        if bad:
            errors.append(f"{folder}: missing/empty {', '.join(bad)}")
            continue
        err = p02.validate_resume(data["resume_v1"], data["position"])
        if err:
            errors.append(f"{folder}: resume_v1 no longer valid ({err})")
            continue
        valid = "quality_feedback" in data and validate_feedback(data["quality_feedback"]) is None
        jobs.append(Job(folder, path, data, valid))
    if errors:
        raise PipelineError("manifest/job state inconsistent:\n  " + "\n  ".join(errors))
    return jobs


# ------------------------------------------------------------------ tracker

def check_tracker(tracker: Path, batch: str, jobs: list[Job]) -> None:
    if not tracker.is_file():
        raise PipelineError(f"tracker not found: {tracker}")
    try:
        rows = p01.read_tracker(tracker)
    except p01.PrepareError as e:
        raise PipelineError(str(e)) from e
    by_folder: dict[str, list[dict]] = {}
    for r in rows:
        if r["batch"] == batch:
            by_folder.setdefault(r["job_folder"], []).append(r)
    problems = []
    for j in jobs:
        matches = by_folder.get(j.folder, [])
        if len(matches) != 1:
            problems.append(f"{j.folder}: {len(matches)} tracker rows")
        elif matches[0]["v1_status"] != "DONE":
            problems.append(f"{j.folder}: v1_status is {matches[0]['v1_status']!r}, not DONE")
    if problems:
        raise PipelineError("tracker preflight failed:\n  " + "\n  ".join(problems))


def update_tracker(tracker: Path, batch: str, folder: str, **fields) -> None:
    """Read-modify-write this stage's columns on one row (03 is serial)."""
    assert set(fields) <= set(OWNED_COLUMNS), fields
    rows = p01.read_tracker(tracker)
    matches = [r for r in rows if r["batch"] == batch and r["job_folder"] == folder]
    if len(matches) != 1:
        raise JobError(f"tracker has {len(matches)} rows for {batch}/{folder}")
    matches[0].update(fields, updated_at=p02.now_str())
    p01.write_tracker_atomic(tracker, rows)


def missing_counts(feedback: dict) -> dict:
    return {"hard_missing": str(len(feedback["hard_skills"]["missing"])),
            "soft_missing": str(len(feedback["soft_skills"]["missing"]))}


def sync_checkpoint(tracker: Path, batch: str, job: Job) -> None:
    target = {"review_status": "DONE", **missing_counts(job.data["quality_feedback"])}
    row = next(r for r in p01.read_tracker(tracker)
               if r["batch"] == batch and r["job_folder"] == job.folder)
    if any(row[k] != v for k, v in target.items()):
        update_tracker(tracker, batch, job.folder, last_error="", **target)


# ------------------------------------------------------------- parsing (04)
# 04 parses its Claude worker envelopes with these (03 itself has no worker).

def parse_response(stdout: str) -> dict:
    """Parse a `--output-format json` envelope (used by 04)."""
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError:
        raise JobError("CLI output is not JSON")
    return _result_object(envelope)


def _result_object(envelope) -> dict:
    if (not isinstance(envelope, dict) or envelope.get("is_error")
            or envelope.get("subtype") != "success"):
        detail = envelope.get("subtype") if isinstance(envelope, dict) else "?"
        raise JobError(f"worker reported error ({detail})")
    text = (envelope.get("result") or "").strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.S)
    if fenced:
        text = fenced.group(1)
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        raise JobError("invalid JSON in worker result")
    if not isinstance(obj, dict):
        raise JobError("worker result is not a JSON object")
    return obj


# ------------------------------------------------------------------- sink

class JobJsonSink:
    """scoring.ResultSink for this stage: quality_feedback + quality_review into job.json, then
    the tracker row. Idempotent: committing the same report twice writes the same content."""

    def __init__(self, tracker: Path, batch: str, jobs: dict[str, Job]):
        self.tracker, self.batch, self.jobs = tracker, batch, jobs

    def _read(self, job: Job) -> dict:
        try:
            current = json.loads(job.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
            raise JobError(f"cannot re-read job.json: {e}") from e
        if not isinstance(current, dict):
            raise JobError("job.json root is no longer an object")
        return current

    def committed_fingerprint(self, job_id: str) -> str | None:
        current = self._read(self.jobs[job_id])
        meta, fb = current.get("quality_review"), current.get("quality_feedback")
        if isinstance(meta, dict) and fb is not None and validate_feedback(fb) is None:
            return meta.get("report_fingerprint")
        return None

    def commit(self, job_id: str, report: QualityReport) -> None:
        job = self.jobs[job_id]
        current = self._read(job)
        changed = [f for f in REQUIRED_FIELDS if current.get(f) != job.data[f]]
        if changed:
            raise JobError(f"job.json changed during review: {', '.join(changed)}")
        feedback = report.to_feedback()
        current["quality_feedback"] = feedback
        current["quality_review"] = {"provider": report.provider, "report_id": report.report_id,
                                     "score": report.score, "generated_at": report.generated_at,
                                     "report_fingerprint": report.fingerprint}
        p01.write_json_atomic(job.path, current)
        job.data = current
        update_tracker(self.tracker, self.batch, job.folder, review_status="DONE", last_error="",
                       **missing_counts(feedback))


# ------------------------------------------------------------------ handoff

def feedback_path(batch: str) -> Path:
    return HANDOFF_ROOT / batch / FEEDBACK_NAME


def build_feedback_ready(batch: str, entries: list[dict], tracker: Path) -> dict:
    """Snapshot of manifest jobs ready for Final Resume. Read-only; keeps manifest order."""
    status = {r["job_folder"]: r["review_status"]
              for r in p01.read_tracker(tracker) if r["batch"] == batch}
    jobs = []
    for item in entries:
        path = Path(item["job_json"])
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if (isinstance(data, dict)
                and status.get(item["job_folder"]) == "DONE"
                and all(_nonempty_str(data.get(f)) for f in REQUIRED_FIELDS)
                and p02.validate_resume(data["resume_v1"], data["position"]) is None
                and "quality_feedback" in data
                and validate_feedback(data["quality_feedback"]) is None):
            jobs.append({"job_folder": item["job_folder"], "job_json": str(path)})
    return {"batch": batch, "generated_at": p02.now_str(), "job_count": len(jobs), "jobs": jobs}


def write_feedback_ready(path: Path, manifest: dict) -> None:
    jobs = manifest["jobs"]
    assert manifest["job_count"] == len(jobs)
    assert len({j["job_folder"] for j in jobs}) == len({j["job_json"] for j in jobs}) == len(jobs)
    path.parent.mkdir(parents=True, exist_ok=True)
    p01.write_json_atomic(path, manifest)


def append_event(event: dict) -> None:
    """One JSONL line per job run: counters and states only (no resume, JD or report text)."""
    try:
        EVENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        line = (json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        fd = os.open(EVENTS_PATH, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
    except OSError as e:
        print(f"[REVIEW] warning: event not recorded ({e})", file=sys.stderr)


# --------------------------------------------------------------------- main

TRACKER_STATUS = {review.SAFE_TO_RETRY: "RETRY", review.PENDING_RESULT: "PENDING_RESULT",
                  review.MANUAL_RECOVERY_REQUIRED: "MANUAL_RECOVERY", review.FAILED: "FAILED",
                  review.PROVIDER_UNAVAILABLE: "PROVIDER_UNAVAILABLE"}


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Resume Quality Review for the jobs in a 02 handoff manifest.")
    ap.add_argument("--input", help=f"handoff manifest (default: newest <batch>/{HANDOFF_NAME})")
    ap.add_argument("--jobs-root", default=str(DEFAULT_JOBS_ROOT),
                    help="jobs root the manifest paths must live under")
    ap.add_argument("--provider", default=os.environ.get("LJP_SCORER", "mock"),
                    help="mock | manual | module:factory (default: $LJP_SCORER or mock)")
    ap.add_argument("--inbox", help="manual provider: request / report folder")
    ap.add_argument("--limit", type=int, help="review only the first N jobs without a result")
    ap.add_argument("--max-polls", type=int, default=60, help="collect() attempts per job and run")
    ap.add_argument("--poll-interval", type=float, default=2.0, help="seconds between collect() attempts")
    ap.add_argument("--dry-run", action="store_true", help="validate and plan only; no provider calls, no writes")
    ap.add_argument("--batch", help="operator actions: the batch of the job folder")
    ap.add_argument("--resolve-not-submitted", metavar="FOLDER",
                    help="operator: confirmed with the provider that nothing was submitted")
    ap.add_argument("--adopt-report", nargs=2, metavar=("FOLDER", "REPORT_JSON"),
                    help="operator: adopt a report obtained by hand")
    ap.add_argument("--note", default="", help="operator note recorded in the journal")
    return ap


def operator_action(args) -> int:
    if not args.batch or not args.note:
        print("FAILED: operator actions need --batch and --note")
        return 2
    folder = args.resolve_not_submitted or args.adopt_report[0]
    path = JOURNAL_ROOT / args.batch / f"{review._safe_name(folder)}.json"
    try:
        if args.resolve_not_submitted:
            review.resolve_not_submitted(path, args.note)
        else:
            report = QualityReport(**json.loads(Path(args.adopt_report[1]).read_text(encoding="utf-8")))
            review.adopt_report(path, report, args.note)
    except Exception as e:  # noqa: BLE001
        print(f"FAILED: {type(e).__name__}: {e}")
        return 1
    print(f"[OPERATOR] {folder}: recorded; rerun stage 3 to continue")
    return 0


def run(args) -> int:
    jobs_root = Path(args.jobs_root).expanduser().resolve()
    manifest = Path(args.input).expanduser().resolve() if args.input else find_latest_manifest()
    batch, entries = load_manifest(manifest, jobs_root)
    jobs = load_jobs(entries)
    tracker = jobs_root / p01.TRACKER_NAME
    check_tracker(tracker, batch, jobs)
    todo = [j for j in jobs if not j.has_valid_result]
    if args.limit:
        todo = todo[:args.limit]
    print(f"[REVIEW] batch {batch}: {len(jobs)} jobs, {len(jobs) - len([j for j in jobs if not j.has_valid_result])} "
          f"with a result, {len(todo)} to review, provider {args.provider}")
    if args.dry_run:
        for j in todo:
            print(f"[PLAN] {j.label}")
        return 0

    marker = HANDOFF_ROOT / batch / REVIEW_BLOCKED_MARKER
    if marker.exists():
        print(f"[REVIEW] {marker.name} found: the operator reran stage 3, retrying the provider")
        marker.unlink()
    for j in jobs:
        if j.has_valid_result:
            sync_checkpoint(tracker, batch, j)

    scorer = load_provider(args.provider, inbox=Path(args.inbox).expanduser() if args.inbox else None)
    sink = JobJsonSink(tracker, batch, {j.folder: j for j in jobs})
    engine = review.ReviewEngine(scorer, JOURNAL_ROOT / batch, sink,
                                 poll_interval=args.poll_interval, max_polls=args.max_polls)
    done = failed = 0
    skipped = len(jobs) - len(todo)
    exit_code = 0
    for i, j in enumerate(todo, 1):
        t0 = time.monotonic()
        r = engine.review(ReviewJob(j.folder, ResumeDocument(j.data["resume_v1"]), j.data["jd"], j.label))
        append_event({"schema": 1, "ts": time.time(), "batch": batch, "job_folder": j.folder,
                      "provider": getattr(scorer, "name", "?"), "outcome": r.outcome, "state": r.state,
                      "reconciled": r.reconciled, "duration_ms": round((time.monotonic() - t0) * 1000),
                      **{f"{k}_calls": v for k, v in r.calls.items()}})
        if r.outcome in DONE_OUTCOMES:
            done += 1
            print(f"[DONE] {j.label} | score {r.report.score if r.report else '?'}")
        else:
            failed += 1
            try:
                update_tracker(tracker, batch, j.folder, review_status=TRACKER_STATUS[r.outcome],
                               last_error=r.detail[:300])
            except (JobError, OSError) as e:
                print(f"[REVIEW] warning: tracker not updated for {j.folder}: {e}")
            print(f"[FAILED] {j.label} | {r.outcome}: {r.detail}")
        print(f"PROGRESS REVIEW {i}/{len(todo)} | DONE {done} FAILED {failed} SKIPPED {skipped}")
        if r.outcome == review.PROVIDER_UNAVAILABLE:
            p01.write_json_atomic(marker, {"batch": batch, "reason": r.detail, "at": p02.now_str()})
            print(f"FAILED: provider unavailable ({r.detail}); stopping. Operator action required, then rerun 03.")
            exit_code = 3
            break
        if r.outcome not in DONE_OUTCOMES:
            exit_code = 1
    write_feedback_ready(feedback_path(batch), build_feedback_ready(batch, entries, tracker))
    return exit_code


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    if args.limit is not None and args.limit < 1:
        ap.error("--limit must be >= 1")
    try:
        code = operator_action(args) if (args.resolve_not_submitted or args.adopt_report) else run(args)
    except (PipelineError, p01.PrepareError, ValueError, TypeError, ImportError) as e:
        print(f"FAILED: {e}")
        code = 1
    print(f"DONE exit={code}")
    return code


if __name__ == "__main__":
    sys.exit(main())
