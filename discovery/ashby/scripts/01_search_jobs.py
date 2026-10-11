#!/usr/bin/env python3
"""Stage 01: discover, title-screen, and emit Ashby jobs for Job Match."""

from __future__ import annotations

import argparse
import sys
import hashlib
import importlib.util
import json
import logging
import os
import re
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlparse
from zoneinfo import ZoneInfo


PROJECT_ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repository root
from common import paths  # noqa: E402

DATA_ROOT = paths.data_dir("discovery", "ashby")  # runtime state, logs, outputs
STAGE00_PATH = PROJECT_ROOT / "00_initialize_board_pool.py"
SPEC = importlib.util.spec_from_file_location("ashby_stage00", STAGE00_PATH)
STAGE00 = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(STAGE00)

DEFAULT_POOL = PROJECT_ROOT / "config/ashby_board_pool.json"
DEFAULT_LOG = DATA_ROOT / "logs/01_search_jobs.log"
PRODUCTION_SEEN = DATA_ROOT / ".runtime/ashby_seen_jobs.json"
TEST_SEEN = DATA_ROOT / ".runtime/ashby_seen_jobs_test.json"
PRODUCTION_CHECKPOINT = DATA_ROOT / ".runtime/01_board_fetch_checkpoint.json"
TEST_CHECKPOINT = DATA_ROOT / ".runtime/01_board_fetch_checkpoint_test.json"
PRODUCTION_PREPARE = DATA_ROOT / ".runtime/01_search_prepare.json"
TEST_PREPARE = DATA_ROOT / ".runtime/01_search_prepare_test.json"
PRODUCTION_SCREENING = DATA_ROOT / ".runtime/01_title_screen_input.json"
TEST_SCREENING = DATA_ROOT / ".runtime/01_title_screen_input_test.json"
PRODUCTION_RESULTS = DATA_ROOT / ".runtime/01_title_screen_results.json"
TEST_RESULTS = DATA_ROOT / ".runtime/01_title_screen_results_test.json"
PRODUCTION_HANDOFF = DATA_ROOT / ".runtime/01_search_handoff.json"
TEST_HANDOFF = DATA_ROOT / ".runtime/01_search_handoff_test.json"
TORONTO_TZ = ZoneInfo("America/Toronto")
MAX_TEST_BOARDS = 20
MAX_TEST_TITLE_JOBS = 10
SEEN_KEY = re.compile(r"ashby:[^:]+:[^:]+")
BACKEND_JOB_KEYS = {"company", "position", "jd", "apply_url", "source_key"}
BACKEND_TOP_LEVEL_KEYS = {"date", "job_count", "jobs"}
TERMINAL_FETCH_ERRORS = {"HTTP_404"}
EMPTY_STATE_TIMESTAMP = "1970-01-01T00:00:00+00:00"


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    except BaseException:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass
        raise


def read_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def fingerprint(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def load_pool(path: Path) -> List[Dict[str, str]]:
    document = read_json(path)
    boards = document.get("boards") if isinstance(document, dict) else None
    if not isinstance(boards, list) or document.get("board_count") != len(boards):
        raise RuntimeError(f"invalid Ashby pool: {path}")
    result: List[Dict[str, str]] = []
    seen = set()
    for row in boards:
        company = row.get("company") if isinstance(row, dict) else None
        slug = row.get("board_slug") if isinstance(row, dict) else None
        if not isinstance(company, str) or not company.strip() or not isinstance(slug, str) or not slug.strip():
            raise RuntimeError("pool contains an invalid board")
        identity = slug.strip().casefold()
        if identity in seen:
            raise RuntimeError(f"duplicate pool board: {slug}")
        seen.add(identity)
        result.append({"company": company.strip(), "board_slug": slug.strip()})
    if not result:
        raise RuntimeError("pool contains no boards")
    return result


def select_boards(
    boards: Sequence[Dict[str, str]], limit: Optional[int], requested: Sequence[str]
) -> List[Dict[str, str]]:
    if limit is None:
        if requested:
            raise RuntimeError("--board-slugs requires --limit-boards")
        return list(boards)
    if not 1 <= limit <= min(MAX_TEST_BOARDS, len(boards)):
        raise RuntimeError(f"--limit-boards must be between 1 and {MAX_TEST_BOARDS}")
    by_slug = {row["board_slug"].casefold(): row for row in boards}
    if requested:
        normalized = list(dict.fromkeys(value.strip().casefold() for value in requested if value.strip()))
        missing = [value for value in normalized if value not in by_slug]
        if missing:
            raise RuntimeError(f"requested boards are not in Pool: {missing}")
        if len(normalized) != limit:
            raise RuntimeError("unique --board-slugs count must equal --limit-boards")
        return [by_slug[value] for value in normalized]
    return list(boards[:limit])


def validate_seen_state(state: Any) -> None:
    if not isinstance(state, dict) or set(state) != {"version", "updated_at", "seen"}:
        raise RuntimeError("invalid Ashby seen state schema")
    if state["version"] != 1 or not isinstance(state["seen"], dict):
        raise RuntimeError("invalid Ashby seen state")
    for key, record in state["seen"].items():
        if not isinstance(key, str) or not SEEN_KEY.fullmatch(key):
            raise RuntimeError(f"invalid seen identity: {key!r}")
        if not isinstance(record, dict) or set(record) != {"first_seen_at"}:
            raise RuntimeError(f"invalid seen record: {key}")
        if not isinstance(record["first_seen_at"], str) or not record["first_seen_at"]:
            raise RuntimeError(f"invalid first_seen_at: {key}")


def load_seen_state(path: Path, now_iso: str) -> Dict[str, Any]:
    if not path.exists():
        # A stable empty fingerprint is required for prepare checkpoint resume
        # before the first successful finalize creates the state file.
        return {"version": 1, "updated_at": EMPTY_STATE_TIMESTAMP, "seen": {}}
    state = read_json(path)
    validate_seen_state(state)
    return state


def save_seen_state(path: Path, state: Dict[str, Any]) -> None:
    validate_seen_state(state)
    atomic_write_json(path, state)
    validate_seen_state(read_json(path))


class FetchCheckpoint:
    """Persist terminal board responses for one unfinished prepare/finalize transaction."""

    def __init__(self, path: Path, signature: str, reset: bool = False):
        self.path = path
        self.signature = signature
        self.lock = threading.Lock()
        self.results: Dict[str, Dict[str, Any]] = {}
        self.status = "PREPARING"
        if path.exists() and not reset:
            document = read_json(path)
            if document.get("signature") == signature and document.get("status") != "FINALIZED":
                rows = document.get("results")
                if isinstance(rows, dict):
                    self.results = {key: value for key, value in rows.items() if isinstance(value, dict)}
                    self.status = document.get("status", "PREPARING")

    @staticmethod
    def terminal(row: Dict[str, Any]) -> bool:
        return row.get("ok") is True or row.get("error_type") in TERMINAL_FETCH_ERRORS

    def completed(self, slug: str) -> bool:
        row = self.results.get(slug.casefold())
        return bool(row and self.terminal(row))

    def _save(self) -> None:
        atomic_write_json(self.path, {
            "version": 1, "updated_at": STAGE00.utc_now(), "signature": self.signature,
            "status": self.status, "results": self.results,
        })

    def put(self, slug: str, result: Dict[str, Any]) -> None:
        with self.lock:
            self.results[slug.casefold()] = result
            self._save()

    def set_status(self, status: str) -> None:
        with self.lock:
            self.status = status
            self._save()


def fetch_board(board: Dict[str, str], timeout: float, retries: int, backoff: float) -> Dict[str, Any]:
    result = STAGE00.fetch_board(board["board_slug"], timeout, retries, backoff)
    return {"company": board["company"], "board_slug": board["board_slug"], **result}


def parse_published(value: Any, now: datetime, days: int = 7) -> Dict[str, Any]:
    if not isinstance(value, str) or not value.strip():
        return {"status": "missing", "age_days": None, "within_window": False}
    raw = value.strip()
    try:
        parsed = datetime.fromisoformat(raw[:-1] + "+00:00" if raw.endswith("Z") else raw)
        if parsed.tzinfo is None:
            raise ValueError("timezone missing")
        parsed = parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return {"status": "invalid", "age_days": None, "within_window": False}
    age = (now.astimezone(timezone.utc) - parsed).total_seconds() / 86400
    return {
        "status": "valid", "age_days": round(age, 3),
        "within_window": parsed >= now.astimezone(timezone.utc) - timedelta(days=days),
        "date": parsed.astimezone(TORONTO_TZ).date().isoformat(),
    }


def valid_http_url(value: Any) -> bool:
    return isinstance(value, str) and urlparse(value.strip()).scheme in {"http", "https"}


def build_candidate(
    board: Dict[str, Any], job: Any, matcher: Any, now: datetime
) -> Tuple[Optional[Dict[str, Any]], str, Optional[str]]:
    if not isinstance(job, dict):
        return None, "invalid_job", "job is not an object"
    if job.get("isListed") is not True:
        return None, "is_listed_reject", None
    location = STAGE00.evaluate_job_location(job, matcher)
    if not location.get("matched"):
        return None, "location_reject", None
    published = parse_published(job.get("publishedAt"), now)
    if not published["within_window"]:
        return None, "freshness_reject", None
    job_id, title = job.get("id"), job.get("title")
    apply_url, jd = job.get("applyUrl"), job.get("descriptionPlain")
    if not isinstance(job_id, str) or not job_id.strip():
        return None, "invalid_job", "missing id"
    if not isinstance(title, str) or not title.strip():
        return None, "invalid_job", "missing title"
    if not valid_http_url(apply_url):
        return None, "invalid_job", "missing or invalid applyUrl"
    if not isinstance(jd, str) or not jd.strip():
        return None, "invalid_job", "missing or empty descriptionPlain"
    identity = f"ashby:{board['board_slug']}:{job_id.strip()}"
    return {
        "source_key": identity, "platform": "ashby", "company": board["company"],
        "board_slug": board["board_slug"], "job_id": job_id.strip(),
        "position": title.strip(), "location": job.get("location"),
        "location_match_reason": location.get("reason"),
        "location_match_category": location.get("category"),
        "published_at": job.get("publishedAt"), "published_age_days": published["age_days"],
        "date": published["date"], "apply_url": apply_url.strip(), "jd": jd,
    }, "candidate", None


def discover(
    board_results: Sequence[Dict[str, Any]], state: Dict[str, Any], matcher: Any,
    now: datetime, max_title_jobs: Optional[int],
) -> Tuple[List[Dict[str, Any]], Dict[str, str], List[Dict[str, Any]], Dict[str, int]]:
    candidates: List[Dict[str, Any]] = []
    pending_seen: Dict[str, str] = {}
    failures: List[Dict[str, Any]] = []
    now_iso = now.isoformat()
    counts = {
        "total_jobs": 0, "already_seen": 0, "identity_duplicates": 0,
        "is_listed_rejected": 0, "location_rejected": 0, "freshness_rejected": 0,
        "invalid_jobs": 0, "title_screen_input": 0, "title_screen_deferred": 0,
    }
    encountered = set()
    for board in board_results:
        if board.get("ok") is not True:
            continue
        for index, job in enumerate(board.get("jobs", [])):
            counts["total_jobs"] += 1
            job_id = job.get("id") if isinstance(job, dict) else None
            identity = f"ashby:{board['board_slug']}:{job_id}" if isinstance(job_id, str) and job_id else None
            if identity and identity in encountered:
                counts["identity_duplicates"] += 1
                continue
            if identity:
                encountered.add(identity)
            if identity and identity in state["seen"]:
                counts["already_seen"] += 1
                continue
            candidate, outcome, error = build_candidate(board, job, matcher, now)
            if outcome == "is_listed_reject":
                counts["is_listed_rejected"] += 1
            elif outcome == "location_reject":
                counts["location_rejected"] += 1
            elif outcome == "freshness_reject":
                counts["freshness_rejected"] += 1
            elif outcome == "invalid_job":
                counts["invalid_jobs"] += 1
                failures.append({
                    "board_slug": board["board_slug"], "job_index": index,
                    "source_key": identity, "status": "JOB_VALIDATION_FAILED", "error": error,
                })
                continue
            else:
                assert candidate is not None and identity is not None
                if max_title_jobs is not None and len(candidates) >= max_title_jobs:
                    counts["title_screen_deferred"] += 1
                    continue
                candidates.append(candidate)
                counts["title_screen_input"] += 1
            if identity:
                pending_seen[identity] = now_iso
    return candidates, pending_seen, failures, counts


def validate_title_results(document: Any, expected: Sequence[str]) -> Tuple[List[str], List[str]]:
    if not isinstance(document, dict) or set(document) != {"accepted_ids", "rejected_ids"}:
        raise RuntimeError("title results must contain exactly accepted_ids and rejected_ids")
    accepted, rejected = document["accepted_ids"], document["rejected_ids"]
    if not isinstance(accepted, list) or not isinstance(rejected, list):
        raise RuntimeError("accepted_ids and rejected_ids must be lists")
    if any(not isinstance(value, str) for value in accepted + rejected):
        raise RuntimeError("title result IDs must be strings")
    if len(set(accepted)) != len(accepted) or len(set(rejected)) != len(rejected):
        raise RuntimeError("title result IDs must not repeat")
    accepted_set, rejected_set, expected_set = set(accepted), set(rejected), set(expected)
    if accepted_set & rejected_set:
        raise RuntimeError("accepted_ids and rejected_ids overlap")
    if accepted_set | rejected_set != expected_set:
        missing = expected_set - (accepted_set | rejected_set)
        unknown = (accepted_set | rejected_set) - expected_set
        raise RuntimeError(f"title partition mismatch; missing={sorted(missing)[:5]} unknown={sorted(unknown)[:5]}")
    return accepted, rejected


def backend_job(candidate: Dict[str, Any]) -> Dict[str, str]:
    result = {
        "company": candidate["company"], "position": candidate["position"],
        "jd": candidate["jd"], "apply_url": candidate["apply_url"],
        "source_key": candidate["apply_url"],
    }
    validate_backend_job(result)
    return result


def validate_backend_job(job: Any) -> None:
    if not isinstance(job, dict) or set(job) != BACKEND_JOB_KEYS:
        raise RuntimeError("backend job must have the exact five-field Job Match schema")
    for key in ("company", "position", "jd"):
        if not isinstance(job[key], str) or not job[key].strip():
            raise RuntimeError(f"backend {key} must be a non-empty string")
    if not valid_http_url(job["apply_url"]) or job["source_key"] != job["apply_url"]:
        raise RuntimeError("backend apply_url/source_key is invalid")


def validate_backend_document(document: Any) -> None:
    if not isinstance(document, dict) or set(document) != BACKEND_TOP_LEVEL_KEYS:
        raise RuntimeError("backend document has an invalid top-level schema")
    if not isinstance(document["date"], str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", document["date"]):
        raise RuntimeError("backend date must be YYYY-MM-DD")
    if not isinstance(document["jobs"], list) or document["job_count"] != len(document["jobs"]):
        raise RuntimeError("backend job_count mismatch")
    keys = set()
    for job in document["jobs"]:
        validate_backend_job(job)
        if job["source_key"] in keys:
            raise RuntimeError("duplicate backend source_key")
        keys.add(job["source_key"])


def mode_paths(args: argparse.Namespace, test: bool) -> Tuple[Path, Path, Path, Path, Path]:
    if test:
        return args.test_seen, args.test_checkpoint, args.test_prepare, args.test_screening, args.test_handoff
    return args.production_seen, args.production_checkpoint, args.production_prepare, args.production_screening, args.production_handoff


def choose_output_paths(output_dir: Path, now: datetime, test: bool) -> Tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = now.astimezone(TORONTO_TZ).strftime("%Y-%m-%d_%H%M_ashby_jobs")
    if test:
        stem += "_test"
    backend, audit = output_dir / f"{stem}.json", output_dir / f"{stem}_audit.json"
    suffix = 2
    while backend.exists() or audit.exists():
        backend, audit = output_dir / f"{stem}_{suffix}.json", output_dir / f"{stem}_audit_{suffix}.json"
        suffix += 1
    return backend, audit


def prepare(args: argparse.Namespace) -> int:
    started = time.monotonic()
    now = datetime.now(timezone.utc)
    pool = load_pool(args.pool)
    selected = select_boards(pool, args.limit_boards, args.board_slugs)
    test = args.limit_boards is not None
    if args.max_title_jobs is not None and not test:
        raise RuntimeError("--max-title-jobs is test-only")
    if args.max_title_jobs is not None and not 0 <= args.max_title_jobs <= MAX_TEST_TITLE_JOBS:
        raise RuntimeError(f"--max-title-jobs must be 0..{MAX_TEST_TITLE_JOBS}")
    seen_path, checkpoint_path, prepare_path, screening_path, _ = mode_paths(args, test)
    state = load_seen_state(seen_path, now.isoformat())
    state_fp = fingerprint(state)
    signature = fingerprint({
        "pool": str(args.pool.resolve()), "pool_fingerprint": fingerprint(read_json(args.pool)),
        "boards": [row["board_slug"] for row in selected], "seen_fingerprint": state_fp,
    })
    checkpoint = FetchCheckpoint(checkpoint_path, signature, args.reset_checkpoint)
    resumed = sum(checkpoint.completed(row["board_slug"]) for row in selected)
    pending = [row for row in selected if not checkpoint.completed(row["board_slug"])]
    pending_slugs = {row["board_slug"].casefold() for row in pending}
    logging.info("RUN phase=prepare test=%s boards=%d resumed=%d pending=%d", test, len(selected), resumed, len(pending))
    fetch_started = time.monotonic()
    with ThreadPoolExecutor(max_workers=min(args.workers, len(pending)) if pending else 1) as executor:
        futures = {executor.submit(fetch_board, row, args.timeout, args.max_retries, args.backoff_base): row for row in pending}
        for future in as_completed(futures):
            row = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                result = {
                    "company": row["company"], "board_slug": row["board_slug"], "ok": False,
                    "http_status": None, "error_type": "UNEXPECTED_ERROR",
                    "error": f"{type(exc).__name__}: {exc}", "request_attempts": 0,
                    "request_seconds": 0.0,
                }
            checkpoint.put(row["board_slug"], result)
            logging.info("BOARD slug=%s ok=%s status=%s jobs=%d attempts=%d", row["board_slug"], result.get("ok"), result.get("http_status"), len(result.get("jobs", [])), result.get("request_attempts", 0))
    fetch_seconds = time.monotonic() - fetch_started
    results = [checkpoint.results[row["board_slug"].casefold()] for row in selected]
    matcher = STAGE00.load_greenhouse_matcher(args.greenhouse_rule)
    candidates, pending_seen, job_failures, counts = discover(results, state, matcher, now, args.max_title_jobs)
    board_failures = [{
        "board_slug": row["board_slug"], "http_status": row.get("http_status"),
        "error_type": row.get("error_type"), "error": row.get("error"),
        "request_attempts": row.get("request_attempts", 0),
    } for row in results if row.get("ok") is not True]
    recoverable_board_failures = [
        row for row in board_failures if row.get("error_type") not in TERMINAL_FETCH_ERRORS
    ]
    stats = {
        "boards_requested": len(selected),
        "boards_succeeded": sum(row.get("ok") is True for row in results),
        "boards_failed": len(board_failures), **counts,
        "api_request_count": sum(
            row.get("request_attempts", 0)
            for row in results if row["board_slug"].casefold() in pending_slugs
        ),
        "api_transaction_request_count": sum(row.get("request_attempts", 0) for row in results),
        "api_request_seconds": round(sum(
            row.get("request_seconds", 0.0)
            for row in results if row["board_slug"].casefold() in pending_slugs
        ), 3),
        "api_transaction_request_seconds": round(sum(row.get("request_seconds", 0.0) for row in results), 3),
        "boards_resumed": resumed,
    }
    # request_count is the transaction total; resumed responses retain their original attempt counts.
    prepared = {
        "version": 1, "stage": "ashby_daily_discovery_prepare", "status": "TITLE_SCREEN_READY",
        "generated_at": now.isoformat(), "test_run": test, "pool_path": str(args.pool.resolve()),
        "pool_boards": len(pool), "selected_board_slugs": [row["board_slug"] for row in selected],
        "seen_state_path": str(seen_path.resolve()), "seen_state_fingerprint": state_fp,
        "checkpoint_signature": signature, "stats": stats, "board_failures": board_failures,
        "recoverable_board_failures": recoverable_board_failures,
        "job_failures": job_failures, "pending_seen_records": pending_seen,
        "candidates": candidates, "timing": {"board_fetch_seconds": round(fetch_seconds, 3)},
    }
    screening = {
        "generated_at": now.isoformat(), "test_run": test,
        "title_screen_total": len(candidates),
        "screening_jobs": [{"source_key": row["source_key"], "position": row["position"]} for row in candidates],
    }
    atomic_write_json(prepare_path, prepared)
    atomic_write_json(screening_path, screening)
    checkpoint.set_status("TITLE_SCREEN_READY")
    print(json.dumps({
        "phase": "prepare", "prepare_file": str(prepare_path.resolve()),
        "screening_file": str(screening_path.resolve()), "stats": stats,
        "job_failures": len(job_failures), "elapsed_seconds": round(time.monotonic() - started, 3),
    }, ensure_ascii=False))
    return 0


def finalize(args: argparse.Namespace) -> int:
    started = time.monotonic()
    test = args.limit_boards is not None
    seen_path, checkpoint_path, prepare_path, _, handoff_path = mode_paths(args, test)
    results_path = args.title_results or (args.test_results if test else args.production_results)
    prepared = read_json(prepare_path)
    if prepared.get("status") != "TITLE_SCREEN_READY" or prepared.get("test_run") is not test:
        raise RuntimeError("prepare artifact is absent or incompatible")
    recoverable_board_failures = prepared.get("recoverable_board_failures")
    if not isinstance(recoverable_board_failures, list):
        raise RuntimeError("prepare artifact lacks recoverable failure state")
    if recoverable_board_failures:
        slugs = [row.get("board_slug") for row in recoverable_board_failures]
        raise RuntimeError(
            f"recoverable board failures remain; rerun prepare before finalize: {slugs[:10]}"
        )
    candidates = prepared.get("candidates")
    pending_seen = prepared.get("pending_seen_records")
    if not isinstance(candidates, list) or not isinstance(pending_seen, dict):
        raise RuntimeError("prepare artifact is incomplete")
    expected = [row.get("source_key") for row in candidates]
    if any(not isinstance(value, str) for value in expected) or len(expected) != len(set(expected)):
        raise RuntimeError("prepare artifact has invalid candidate identities")
    accepted, rejected = validate_title_results(read_json(results_path), expected)
    accepted_set = set(accepted)
    backend_jobs: List[Dict[str, str]] = []
    duplicate_outputs: List[Dict[str, str]] = []
    output_keys = set()
    for row in candidates:
        if row["source_key"] not in accepted_set:
            continue
        job = backend_job(row)
        if job["source_key"] in output_keys:
            duplicate_outputs.append({"internal_source_key": row["source_key"], "source_key": job["source_key"]})
            continue
        output_keys.add(job["source_key"])
        backend_jobs.append(job)
    now = datetime.now(timezone.utc)
    backend = {"date": now.astimezone(TORONTO_TZ).date().isoformat(), "job_count": len(backend_jobs), "jobs": backend_jobs}
    validate_backend_document(backend)
    backend_path, audit_path = choose_output_paths(args.output_dir, now, test)
    atomic_write_json(backend_path, backend)
    validate_backend_document(read_json(backend_path))

    current_state = load_seen_state(seen_path, prepared["generated_at"])
    if fingerprint(current_state) != prepared["seen_state_fingerprint"]:
        raise RuntimeError("seen state changed after prepare; refusing stale finalize")
    for key, first_seen_at in pending_seen.items():
        if not isinstance(key, str) or not SEEN_KEY.fullmatch(key) or not isinstance(first_seen_at, str):
            raise RuntimeError(f"invalid pending seen record: {key!r}")
        current_state["seen"][key] = {"first_seen_at": first_seen_at}
    current_state["updated_at"] = now.isoformat()
    save_seen_state(seen_path, current_state)

    stats = {
        **prepared["stats"], "title_accepted": len(accepted), "title_rejected": len(rejected),
        "title_failed": 0, "final_jobs": len(backend_jobs),
        "ai_call_count": args.ai_invocations,
        "ai_cost_usd": args.ai_cost_usd,
    }
    audit = {
        "version": 1, "stage": "ashby_daily_discovery", "generated_at": now.isoformat(),
        "test_run": test, "output_file": str(backend_path.resolve()), "seen_state_path": str(seen_path.resolve()),
        "stats": stats, "board_failures": prepared["board_failures"],
        "job_failures": prepared["job_failures"], "duplicate_backend_source_keys": duplicate_outputs,
        "accepted_internal_ids": accepted, "rejected_internal_ids": rejected,
        "timing": {**prepared["timing"], "stage_wall_seconds": round((now - datetime.fromisoformat(prepared["generated_at"])).total_seconds(), 3), "finalize_seconds": round(time.monotonic() - started, 3)},
    }
    atomic_write_json(audit_path, audit)
    atomic_write_json(handoff_path, {
        "generated_at": now.isoformat(), "stage": "ashby_daily_discovery",
        "status": "READY", "test_run": test, "output_file": str(backend_path.resolve()),
        "job_count": len(backend_jobs),
    })
    checkpoint = FetchCheckpoint(checkpoint_path, prepared["checkpoint_signature"])
    checkpoint.set_status("FINALIZED")
    print(json.dumps({
        "phase": "finalize", "output": str(backend_path.resolve()),
        "audit": str(audit_path.resolve()), "handoff": str(handoff_path.resolve()),
        "seen_state": str(seen_path.resolve()), "stats": stats,
    }, ensure_ascii=False))
    return 0


def configure_logging(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=[logging.FileHandler(path, encoding="utf-8"), logging.StreamHandler()])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("prepare", "finalize"), default="prepare")
    parser.add_argument("--pool", type=Path, default=DEFAULT_POOL)
    parser.add_argument("--output-dir", type=Path, default=DATA_ROOT / "outputs")
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG)
    parser.add_argument("--workers", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument("--backoff-base", type=float, default=1.0)
    parser.add_argument("--limit-boards", type=int)
    parser.add_argument("--board-slugs", nargs="*", default=[])
    parser.add_argument("--max-title-jobs", type=int)
    parser.add_argument("--reset-checkpoint", action="store_true")
    parser.add_argument("--greenhouse-rule", type=Path, default=STAGE00.DEFAULT_GREENHOUSE_RULE)
    parser.add_argument("--title-results", type=Path)
    parser.add_argument("--ai-invocations", type=int, default=0)
    parser.add_argument("--ai-cost-usd", type=float)
    parser.add_argument("--production-seen", type=Path, default=PRODUCTION_SEEN)
    parser.add_argument("--test-seen", type=Path, default=TEST_SEEN)
    parser.add_argument("--production-checkpoint", type=Path, default=PRODUCTION_CHECKPOINT)
    parser.add_argument("--test-checkpoint", type=Path, default=TEST_CHECKPOINT)
    parser.add_argument("--production-prepare", type=Path, default=PRODUCTION_PREPARE)
    parser.add_argument("--test-prepare", type=Path, default=TEST_PREPARE)
    parser.add_argument("--production-screening", type=Path, default=PRODUCTION_SCREENING)
    parser.add_argument("--test-screening", type=Path, default=TEST_SCREENING)
    parser.add_argument("--production-results", type=Path, default=PRODUCTION_RESULTS)
    parser.add_argument("--test-results", type=Path, default=TEST_RESULTS)
    parser.add_argument("--production-handoff", type=Path, default=PRODUCTION_HANDOFF)
    parser.add_argument("--test-handoff", type=Path, default=TEST_HANDOFF)
    return parser


def run(args: argparse.Namespace) -> int:
    configure_logging(args.log)
    if args.workers < 1 or args.timeout <= 0 or args.max_retries < 0 or args.backoff_base < 0:
        raise RuntimeError("workers/timeout must be positive; retries/backoff non-negative")
    if args.ai_invocations < 0 or (args.ai_cost_usd is not None and args.ai_cost_usd < 0):
        raise RuntimeError("AI metrics must be non-negative")
    return prepare(args) if args.phase == "prepare" else finalize(args)


if __name__ == "__main__":
    raise SystemExit(run(build_parser().parse_args()))
