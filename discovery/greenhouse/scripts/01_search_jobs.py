#!/usr/bin/env python3
"""Prepare or finalize Greenhouse discovery without launching title-screen workers."""

from __future__ import annotations

import argparse
import sys
import hashlib
import importlib.util
import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))  # repository root
from common import paths  # noqa: E402

DATA_ROOT = paths.data_dir("discovery", "greenhouse")  # runtime state, logs, outputs
CORE_PATH = PROJECT_ROOT / "01_filter_greenhouse_boards.py"
SPEC = importlib.util.spec_from_file_location("greenhouse_board_filter", CORE_PATH)
CORE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(CORE)

DEFAULT_REGISTRY = DATA_ROOT / "outputs/greenhouse_boards_relevant_merged.json"
DEFAULT_LOG = DATA_ROOT / "logs/01_search_jobs.log"
TORONTO_TZ = ZoneInfo("America/Toronto")
MAX_TEST_BOARDS = 20
PRODUCTION_HANDOFF = DATA_ROOT / ".runtime/01_search_handoff.json"
TEST_HANDOFF = DATA_ROOT / ".runtime/01_search_handoff_test.json"
PRODUCTION_SEEN_STATE = DATA_ROOT / ".runtime/greenhouse_seen_jobs.json"
TEST_SEEN_STATE = DATA_ROOT / ".runtime/greenhouse_seen_jobs_test.json"
PRODUCTION_PREPARE = DATA_ROOT / ".runtime/01_search_prepare.json"
TEST_PREPARE = DATA_ROOT / ".runtime/01_search_prepare_test.json"
PRODUCTION_SCREENING = DATA_ROOT / ".runtime/01_title_screen_input.json"
TEST_SCREENING = DATA_ROOT / ".runtime/01_title_screen_input_test.json"
PRODUCTION_RESULTS = DATA_ROOT / ".runtime/01_title_screen_results.json"
TEST_RESULTS = DATA_ROOT / ".runtime/01_title_screen_results_test.json"

CURATED_TEST_TOKENS = (
    "adyen", "7shifts", "faire", "capintel", "internationalbusinessuniversity",
    "angusreid", "gomotive", "zestyai", "airnorth", "aspectbiosystems",
    "michelscorporation", "textnowinc", "ensemble", "mobsquad", "coinme",
    "dept", "fanaticsfbg", "telixus", "aperaaiinc", "jackmortonworldwide",
)
GTA_PLACES = (
    "toronto", "greater toronto area", "gta", "north york", "scarborough",
    "etobicoke", "mississauga", "brampton", "markham", "vaughan",
    "richmond hill", "oakville", "burlington", "milton", "pickering", "ajax",
    "whitby", "oshawa",
)


def _contains_phrase(text: str, phrase: str) -> bool:
    return bool(re.search(r"(?<![a-z0-9])" + re.escape(phrase) + r"(?![a-z0-9])", text))


def match_target_location(location: str) -> Tuple[bool, Optional[str]]:
    """Accept GTA locations or locations explicitly remote-enabled for Canada."""
    normalized = CORE.normalize_location(location)
    if not normalized:
        return False, None
    us_regions = "|".join(
        [re.escape(value) for value in CORE.US_STATE_NAMES] + sorted(CORE.US_STATE_CODES)
    )
    for place in GTA_PLACES:
        if not _contains_phrase(normalized, place):
            continue
        us_place = re.search(
            re.escape(place) + r"\s*,\s*(?:" + us_regions + r")(?:\b|$)", normalized
        )
        if not us_place:
            return True, "gta"
    has_canada = (
        _contains_phrase(normalized, "canada")
        or _contains_phrase(normalized, "canadian")
        or bool(re.search(r"(?:^|[,;/|])\s*CAN(?=\s*(?:$|[,;/|]|\bOR\b|\bAND\b))", location))
    )
    has_remote = bool(re.search(
        r"(?<![a-z0-9])(remote|work from home|home-based|virtual)(?![a-z0-9])", normalized
    ))
    if has_canada and has_remote:
        return True, "canada_remote"
    return False, None


def classify_updated_at(
    value: Any, now: Optional[datetime] = None, recent_days: int = 7
) -> Dict[str, Any]:
    now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if value is None or not isinstance(value, str) or not value.strip():
        return {"status": "missing", "updated_age_days": None, "updated_within_7d": False}
    raw = value.strip()
    try:
        parsed = datetime.fromisoformat(raw[:-1] + "+00:00" if raw.endswith("Z") else raw)
        if parsed.tzinfo is None:
            raise ValueError("timestamp has no timezone")
        parsed_utc = parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return {"status": "invalid", "updated_age_days": None, "updated_within_7d": False}
    age_days = (now_utc - parsed_utc).total_seconds() / 86400
    return {
        "status": "valid", "updated_age_days": round(age_days, 3),
        "updated_within_7d": parsed_utc >= now_utc - timedelta(days=recent_days),
    }


def load_registry(path: Path) -> List[str]:
    with path.open(encoding="utf-8") as handle:
        document = json.load(handle)
    boards = document.get("boards") if isinstance(document, dict) else None
    if not isinstance(boards, list):
        raise RuntimeError(f"registry has no boards list: {path}")
    tokens: List[str] = []
    seen = set()
    for row in boards:
        token = (row.get("board_token") or "").strip().strip("/").lower()
        if token and token not in seen:
            seen.add(token)
            tokens.append(token)
    if not tokens:
        raise RuntimeError("registry contains no board tokens")
    return tokens


def select_test_boards(
    registry_tokens: Sequence[str], limit: int, requested_tokens: Sequence[str]
) -> List[str]:
    registry_set = set(registry_tokens)
    if requested_tokens:
        normalized = [token.strip().strip("/").lower() for token in requested_tokens]
        missing = [token for token in normalized if token not in registry_set]
        if missing:
            raise RuntimeError(f"requested tokens are not in the merged registry: {missing}")
        selected = list(dict.fromkeys(normalized))
        if len(selected) > limit:
            raise RuntimeError("--board-tokens exceeds --limit-boards")
        return selected
    selected = [token for token in CURATED_TEST_TOKENS if token in registry_set]
    if len(selected) < limit:
        selected.extend(token for token in registry_tokens if token not in set(selected))
    return selected[:limit]


def validate_seen_state(state: Dict[str, Any]) -> None:
    if not isinstance(state, dict) or set(state) != {"version", "updated_at", "seen"}:
        raise RuntimeError("invalid Greenhouse seen state schema")
    if state["version"] != 1 or not isinstance(state["seen"], dict):
        raise RuntimeError("invalid Greenhouse seen state")
    for key, record in state["seen"].items():
        if not re.fullmatch(r"greenhouse:[^:]+:[^:]+", key):
            raise RuntimeError(f"invalid seen key: {key}")
        if not isinstance(record, dict) or set(record) != {"first_seen_at"}:
            raise RuntimeError(f"invalid seen record: {key}")
        if not isinstance(record["first_seen_at"], str) or not record["first_seen_at"]:
            raise RuntimeError(f"invalid first_seen_at: {key}")


def load_seen_state(path: Path, now_iso: str) -> Dict[str, Any]:
    if not path.exists():
        return {"version": 1, "updated_at": now_iso, "seen": {}}
    with path.open(encoding="utf-8") as handle:
        state = json.load(handle)
    validate_seen_state(state)
    return state


def save_seen_state(path: Path, state: Dict[str, Any]) -> None:
    validate_seen_state(state)
    CORE.atomic_write_json(path, state)
    with path.open(encoding="utf-8") as handle:
        validate_seen_state(json.load(handle))


def seen_state_fingerprint(state: Dict[str, Any]) -> str:
    payload = json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def fetch_board(token: str, timeout: float, max_attempts: int, backoff_base: float) -> Dict[str, Any]:
    fetched = CORE.fetch_board_jobs(token, timeout, max_attempts, backoff_base)
    result = {
        "board_token": token, "status": fetched["status"],
        "request_attempts": fetched["request_attempts"], "jobs": fetched.get("jobs", []),
    }
    if "error" in fetched:
        result["error"] = fetched["error"]
    return result


def build_cheap_candidate(token: str, job: Any, now: datetime) -> Tuple[Optional[Dict[str, Any]], str]:
    if not isinstance(job, dict):
        return None, "invalid_job"
    job_id = job.get("id")
    title = job.get("title")
    location_obj = job.get("location")
    location = location_obj.get("name") if isinstance(location_obj, dict) else None
    if job_id is None or not isinstance(title, str) or not title.strip() or not isinstance(location, str):
        return None, "invalid_job"
    location_ok, location_reason = match_target_location(location)
    if not location_ok:
        return None, "location_reject"
    updated = classify_updated_at(job.get("updated_at"), now=now)
    if not updated["updated_within_7d"]:
        return None, "freshness_reject"
    return {
        "source_key": f"greenhouse:{token}:{job_id}", "platform": "greenhouse",
        "board_token": token, "job_id": job_id, "internal_job_id": job.get("internal_job_id"),
        "position": title.strip(), "location": location.strip(),
        "location_match_reason": location_reason, "updated_at": job.get("updated_at"),
        "updated_age_days": updated["updated_age_days"], "updated_within_7d": True,
        "updated_at_status": updated["status"], "absolute_url": job.get("absolute_url"),
    }, "candidate"


def collect_unseen_cheap_candidates(
    board_results: Sequence[Dict[str, Any]], state: Dict[str, Any], now: datetime
) -> Tuple[List[Dict[str, Any]], Dict[str, str], Dict[str, int]]:
    """Collect unseen IDs in memory; this function never persists state."""
    now_iso = now.astimezone(timezone.utc).isoformat()
    candidates: List[Dict[str, Any]] = []
    newly_seen: Dict[str, str] = {}
    counts = {
        "jobs_seen": 0, "already_seen": 0, "newly_seen": 0, "invalid_jobs": 0,
        "location_rejected": 0, "freshness_rejected": 0, "cheap_filter_pass": 0,
    }
    for row in board_results:
        token = row["board_token"]
        for job in row.get("jobs", []):
            counts["jobs_seen"] += 1
            if not isinstance(job, dict) or job.get("id") is None:
                counts["invalid_jobs"] += 1
                continue
            source_key = f"greenhouse:{token}:{job['id']}"
            if source_key in state["seen"] or source_key in newly_seen:
                counts["already_seen"] += 1
                continue
            newly_seen[source_key] = now_iso
            counts["newly_seen"] += 1
            candidate, outcome = build_cheap_candidate(token, job, now)
            if outcome == "invalid_job":
                counts["invalid_jobs"] += 1
            elif outcome == "location_reject":
                counts["location_rejected"] += 1
            elif outcome == "freshness_reject":
                counts["freshness_rejected"] += 1
            else:
                counts["cheap_filter_pass"] += 1
                assert candidate is not None
                candidates.append(candidate)
    return candidates, newly_seen, counts


def validate_screening_results(document: Any, expected_ids: Sequence[str]) -> Tuple[List[str], List[str]]:
    if not isinstance(document, dict) or set(document) != {"accepted_ids", "rejected_ids"}:
        raise RuntimeError("title results must contain exactly accepted_ids and rejected_ids")
    accepted, rejected = document["accepted_ids"], document["rejected_ids"]
    if not isinstance(accepted, list) or not isinstance(rejected, list):
        raise RuntimeError("accepted_ids and rejected_ids must be lists")
    if any(not isinstance(value, str) for value in accepted + rejected):
        raise RuntimeError("all title result IDs must be strings")
    if len(accepted) != len(set(accepted)) or len(rejected) != len(set(rejected)):
        raise RuntimeError("title results contain duplicate IDs")
    accepted_set, rejected_set, expected_set = set(accepted), set(rejected), set(expected_ids)
    if accepted_set & rejected_set:
        raise RuntimeError("accepted_ids and rejected_ids overlap")
    unknown = (accepted_set | rejected_set) - expected_set
    missing = expected_set - (accepted_set | rejected_set)
    if unknown:
        raise RuntimeError(f"title results contain unknown IDs: {sorted(unknown)[:5]}")
    if missing:
        raise RuntimeError(f"title results omit IDs: {sorted(missing)[:5]}")
    return accepted, rejected


def validate_candidate_document(document: Dict[str, Any]) -> None:
    jobs = document.get("jobs")
    if not isinstance(jobs, list) or document.get("candidate_count") != len(jobs):
        raise RuntimeError("candidate_count does not equal len(jobs)")
    required = {
        "source_key", "platform", "board_token", "job_id", "position", "location",
        "updated_at", "updated_within_7d", "absolute_url",
    }
    identities = set()
    for index, job in enumerate(jobs):
        if not isinstance(job, dict) or not required.issubset(job):
            raise RuntimeError(f"candidate {index} is missing required fields")
        expected = f"greenhouse:{job['board_token']}:{job['job_id']}"
        if job["source_key"] != expected or expected in identities:
            raise RuntimeError(f"invalid or duplicate candidate identity at index {index}")
        if job["updated_within_7d"] is not True:
            raise RuntimeError(f"candidate {index} did not pass updated_at freshness")
        identities.add(expected)


def choose_output_path(output_dir: Path, now: datetime, test_run: bool) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    label = "_test" if test_run else ""
    stem = now.astimezone(TORONTO_TZ).strftime("%Y-%m-%d_%H%M_greenhouse_candidates") + label
    path = output_dir / f"{stem}.json"
    suffix = 2
    while path.exists():
        path = output_dir / f"{stem}_{suffix}.json"
        suffix += 1
    return path


def configure_logging(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(path, encoding="utf-8"), logging.StreamHandler()],
    )


def resolve_mode_paths(args: argparse.Namespace, test_run: bool) -> Tuple[Path, Path, Path, Path]:
    if test_run:
        return args.test_seen_state, args.test_prepare, args.test_screening, args.test_handoff
    return args.production_seen_state, args.production_prepare, args.production_screening, args.production_handoff


def prepare(args: argparse.Namespace) -> int:
    started = time.monotonic()
    run_now = datetime.now(timezone.utc)
    registry_tokens = load_registry(args.registry)
    test_run = args.limit_boards is not None
    if args.board_tokens and not test_run:
        raise RuntimeError("--board-tokens requires --limit-boards")
    if test_run:
        if not 1 <= args.limit_boards <= min(MAX_TEST_BOARDS, len(registry_tokens)):
            raise RuntimeError(f"--limit-boards must be between 1 and {MAX_TEST_BOARDS}")
        selected = select_test_boards(registry_tokens, args.limit_boards, args.board_tokens)
        if len(selected) != args.limit_boards:
            raise RuntimeError("selected test board count does not equal --limit-boards")
    else:
        selected = list(registry_tokens)
    state_path, prepare_path, screening_path, _ = resolve_mode_paths(args, test_run)
    state = load_seen_state(state_path, run_now.isoformat())
    state_fingerprint = seen_state_fingerprint(state)
    logging.info(
        "RUN_MODE phase=prepare test_run=%s registry_boards=%d selected=%d seen_state=%s",
        test_run, len(registry_tokens), len(selected), state_path,
    )

    fetch_started = time.monotonic()
    board_results: List[Dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=min(args.workers, len(selected))) as executor:
        futures = {
            executor.submit(fetch_board, token, args.timeout, args.max_attempts, args.backoff_base): token
            for token in selected
        }
        for future in as_completed(futures):
            row = future.result()
            board_results.append(row)
            logging.info("BOARD board_token=%s status=%s jobs=%d", row["board_token"], row["status"], len(row.get("jobs", [])))
    board_fetch_seconds = time.monotonic() - fetch_started
    by_token = {row["board_token"]: row for row in board_results}
    ordered_results = [by_token[token] for token in selected]
    logging.info(
        "BOARDS_DONE boards=%d successful=%d failed=%d duration_seconds=%.3f",
        len(selected), sum(row["status"] in {"LIVE", "EMPTY"} for row in ordered_results),
        sum(row["status"] not in {"LIVE", "EMPTY"} for row in ordered_results), board_fetch_seconds,
    )

    discovery_started = time.monotonic()
    cheap_candidates, newly_seen, counts = collect_unseen_cheap_candidates(ordered_results, state, run_now)
    discovery_seconds = time.monotonic() - discovery_started
    failures = [
        {key: row[key] for key in ("board_token", "status", "error", "request_attempts") if key in row}
        for row in ordered_results if row["status"] not in {"LIVE", "EMPTY"}
    ]
    title_screen_total = len(cheap_candidates)
    logging.info(
        "DISCOVERY_DONE jobs_seen=%d already_seen=%d newly_seen=%d location_rejected=%d "
        "freshness_rejected=%d invalid_jobs=%d cheap_filter_pass=%d title_screen_total=%d duration_seconds=%.3f",
        counts["jobs_seen"], counts["already_seen"], counts["newly_seen"],
        counts["location_rejected"], counts["freshness_rejected"], counts["invalid_jobs"],
        counts["cheap_filter_pass"], title_screen_total, discovery_seconds,
    )
    screening_jobs = [
        {"source_key": candidate["source_key"], "position": candidate["position"]}
        for candidate in cheap_candidates
    ]
    prepare_document = {
        "version": 1, "stage": "greenhouse_discovery_prepare", "status": "TITLE_SCREEN_READY",
        "generated_at": run_now.isoformat(), "test_run": test_run,
        "registry_path": str(args.registry.resolve()), "seen_state_path": str(state_path.resolve()),
        "seen_state_fingerprint": state_fingerprint, "registry_boards": len(registry_tokens),
        "boards_selected": len(selected),
        "boards_successful": sum(row["status"] in {"LIVE", "EMPTY"} for row in ordered_results),
        "boards_failed": len(failures), **counts, "title_screen_total": title_screen_total,
        "selected_board_tokens": selected, "failures": failures,
        "pending_seen_records": newly_seen, "cheap_candidates": cheap_candidates,
        "timing": {"board_fetch_seconds": round(board_fetch_seconds, 3), "discovery_filter_seconds": round(discovery_seconds, 3)},
    }
    screening_document = {
        "generated_at": run_now.isoformat(), "test_run": test_run,
        "title_screen_total": title_screen_total, "screening_jobs": screening_jobs,
    }
    CORE.atomic_write_json(prepare_path, prepare_document)
    CORE.atomic_write_json(screening_path, screening_document)
    logging.info("TITLE_SCREEN_READY total=%d input=%s", title_screen_total, screening_path)
    print(json.dumps({
        "phase": "prepare", "prepare_file": str(prepare_path.resolve()),
        "screening_file": str(screening_path.resolve()), "title_screen_total": title_screen_total,
        "summary": counts,
        "timing": {**prepare_document["timing"], "prepare_total_seconds": round(time.monotonic() - started, 3)},
    }, ensure_ascii=False))
    return 0


def finalize(args: argparse.Namespace) -> int:
    started = time.monotonic()
    test_run = args.limit_boards is not None
    state_path, prepare_path, _, handoff_path = resolve_mode_paths(args, test_run)
    results_path = args.title_results or (args.test_results if test_run else args.production_results)
    with prepare_path.open(encoding="utf-8") as handle:
        prepared = json.load(handle)
    if prepared.get("status") != "TITLE_SCREEN_READY" or prepared.get("test_run") is not test_run:
        raise RuntimeError(f"prepare artifact is not compatible: {prepare_path}")
    candidates, newly_seen = prepared.get("cheap_candidates"), prepared.get("pending_seen_records")
    if not isinstance(candidates, list) or not isinstance(newly_seen, dict):
        raise RuntimeError("prepare artifact is missing candidates or newly_seen")
    expected_ids = [candidate.get("source_key") for candidate in candidates]
    if any(not isinstance(value, str) for value in expected_ids) or len(expected_ids) != len(set(expected_ids)):
        raise RuntimeError("prepare artifact contains invalid or duplicate candidate IDs")
    with results_path.open(encoding="utf-8") as handle:
        results_document = json.load(handle)
    accepted_ids, rejected_ids = validate_screening_results(results_document, expected_ids)
    accepted_set = set(accepted_ids)
    accepted = [candidate for candidate in candidates if candidate["source_key"] in accepted_set]
    logging.info("TITLE_SCREEN_DONE total=%d accepted=%d rejected=%d", len(expected_ids), len(accepted_ids), len(rejected_ids))

    now = datetime.now(timezone.utc)
    output = {
        "generated_at": now.isoformat(), "source": "greenhouse", "test_run": test_run,
        "registry_path": prepared["registry_path"], "seen_state_path": str(state_path.resolve()),
        "registry_boards": prepared["registry_boards"], "boards_selected": prepared["boards_selected"],
        "boards_successful": prepared["boards_successful"], "boards_failed": prepared["boards_failed"],
        **{key: prepared[key] for key in (
            "jobs_seen", "already_seen", "newly_seen", "invalid_jobs", "location_rejected",
            "freshness_rejected", "cheap_filter_pass",
        )},
        "title_screen_total": len(expected_ids), "title_screen_accepted": len(accepted_ids),
        "title_screen_rejected": len(rejected_ids), "candidate_count": len(accepted),
        "selected_board_tokens": prepared["selected_board_tokens"], "failures": prepared["failures"],
        "jobs": accepted,
    }
    validate_candidate_document(output)
    output_path = choose_output_path(args.output_dir, now, test_run)
    CORE.atomic_write_json(output_path, output)
    with output_path.open(encoding="utf-8") as handle:
        validate_candidate_document(json.load(handle))

    commit_started = time.monotonic()
    current_state = load_seen_state(state_path, prepared["generated_at"])
    if seen_state_fingerprint(current_state) != prepared["seen_state_fingerprint"]:
        raise RuntimeError("seen state changed after prepare; refusing stale finalize")
    for source_key, first_seen_at in newly_seen.items():
        if not re.fullmatch(r"greenhouse:[^:]+:[^:]+", source_key) or not isinstance(first_seen_at, str):
            raise RuntimeError(f"invalid pending seen record: {source_key}")
        current_state["seen"][source_key] = {"first_seen_at": first_seen_at}
    current_state["updated_at"] = now.isoformat()
    save_seen_state(state_path, current_state)
    commit_seconds = time.monotonic() - commit_started
    logging.info("SEEN_STATE_COMMIT added=%d total=%d duration_seconds=%.3f", len(newly_seen), len(current_state["seen"]), commit_seconds)
    CORE.atomic_write_json(handoff_path, {
        "generated_at": CORE.utc_now(), "stage": "greenhouse_discovery_title_screen",
        "status": "READY", "test_run": test_run,
        "output_file": str(output_path.resolve()), "candidate_count": len(accepted),
    })
    total_seconds = (datetime.now(timezone.utc) - datetime.fromisoformat(prepared["generated_at"])).total_seconds()
    summary = {key: output[key] for key in (
        "boards_selected", "boards_successful", "boards_failed", "jobs_seen", "already_seen",
        "newly_seen", "location_rejected", "freshness_rejected", "invalid_jobs",
        "cheap_filter_pass", "title_screen_total", "title_screen_accepted",
        "title_screen_rejected", "candidate_count",
    )}
    timing = {
        **prepared["timing"], "seen_state_commit_seconds": round(commit_seconds, 3),
        "finalize_seconds": round(time.monotonic() - started, 3), "stage01_wall_seconds": round(total_seconds, 3),
    }
    logging.info("DONE summary=%s timing=%s", summary, timing)
    print(json.dumps({
        "phase": "finalize", "output": str(output_path.resolve()),
        "handoff": str(handoff_path.resolve()), "seen_state": str(state_path.resolve()),
        "summary": summary, "timing": timing,
    }, ensure_ascii=False))
    return 0


def run(args: argparse.Namespace) -> int:
    configure_logging(args.log)
    if args.workers < 1:
        raise RuntimeError("--workers must be >= 1")
    return prepare(args) if args.phase == "prepare" else finalize(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("prepare", "finalize"), default="prepare")
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--output-dir", type=Path, default=DATA_ROOT / "outputs")
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG)
    parser.add_argument("--limit-boards", type=int)
    parser.add_argument("--board-tokens", nargs="*", default=[])
    parser.add_argument("--workers", type=int, default=20, help="parallel board API workers")
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--backoff-base", type=float, default=1.0)
    parser.add_argument("--title-results", type=Path)
    parser.add_argument("--production-seen-state", type=Path, default=PRODUCTION_SEEN_STATE)
    parser.add_argument("--test-seen-state", type=Path, default=TEST_SEEN_STATE)
    parser.add_argument("--production-prepare", type=Path, default=PRODUCTION_PREPARE)
    parser.add_argument("--test-prepare", type=Path, default=TEST_PREPARE)
    parser.add_argument("--production-screening", type=Path, default=PRODUCTION_SCREENING)
    parser.add_argument("--test-screening", type=Path, default=TEST_SCREENING)
    parser.add_argument("--production-results", type=Path, default=PRODUCTION_RESULTS)
    parser.add_argument("--test-results", type=Path, default=TEST_RESULTS)
    parser.add_argument("--production-handoff", type=Path, default=PRODUCTION_HANDOFF)
    parser.add_argument("--test-handoff", type=Path, default=TEST_HANDOFF)
    return parser


if __name__ == "__main__":
    raise SystemExit(run(build_parser().parse_args()))
