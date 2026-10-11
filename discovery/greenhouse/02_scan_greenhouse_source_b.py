#!/usr/bin/env python3
"""Scan only the open-ats-feed Greenhouse tokens absent from Source A."""

from __future__ import annotations

import argparse
import sys
import csv
import importlib.util
import io
import json
import logging
import random
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


ROOT = Path(__file__).resolve().parent
CORE_PATH = ROOT / "01_filter_greenhouse_boards.py"

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repository root
from common import paths  # noqa: E402

DATA_ROOT = paths.data_dir("discovery", "greenhouse")  # runtime state, logs, outputs
SPEC = importlib.util.spec_from_file_location("greenhouse_board_filter", CORE_PATH)
CORE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(CORE)

SOURCE_B_NAME = "open-ats-feed"
SOURCE_B_URL = "https://github.com/veresk06/open-ats-feed"
SOURCE_B_DATA_URL = (
    "https://raw.githubusercontent.com/veresk06/open-ats-feed/main/docs/data/greenhouse.csv"
)
SOURCE_A_AUDIT = DATA_ROOT / ("outputs/greenhouse_board_scan_audit.json")
SOURCE_A_RELEVANT = DATA_ROOT / ("outputs/greenhouse_boards_relevant.json")
DEFAULT_CHECKPOINT = DATA_ROOT / (".runtime/greenhouse_source_b_checkpoint.json")
DEFAULT_AUDIT = DATA_ROOT / ("outputs/greenhouse_source_b_audit.json")
DEFAULT_COMPARISON = DATA_ROOT / ("outputs/greenhouse_source_comparison.json")
DEFAULT_MERGED = DATA_ROOT / ("outputs/greenhouse_boards_relevant_merged.json")
DEFAULT_LOG = DATA_ROOT / ("logs/02_scan_greenhouse_source_b.log")


def normalize_token(value: str) -> str:
    return (value or "").strip().strip("/").lower()


def read_json(path: Path) -> Dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise RuntimeError(f"expected JSON object in {path}")
    return value


def load_source_a(audit_path: Path) -> Tuple[set, Dict[str, Dict[str, Any]]]:
    document = read_json(audit_path)
    rows = document.get("boards")
    if not isinstance(rows, list):
        raise RuntimeError(f"Source A audit has no boards list: {audit_path}")
    by_token: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        token = normalize_token(row.get("board_token", ""))
        if token:
            by_token[token] = row
    if not by_token:
        raise RuntimeError("Source A audit contains no board tokens")
    return set(by_token), by_token


def load_source_b(timeout: float) -> Tuple[set, Dict[str, Dict[str, str]]]:
    status, payload, _ = CORE.http_get(SOURCE_B_DATA_URL, timeout)
    if status != 200:
        raise RuntimeError(f"Source B CSV returned HTTP {status}")
    reader = csv.DictReader(io.StringIO(payload.decode("utf-8-sig")))
    required = {"provider", "token", "open_postings", "board_url", "api_url"}
    missing = required - set(reader.fieldnames or [])
    if missing:
        raise RuntimeError(f"Source B CSV missing fields {sorted(missing)}; actual={reader.fieldnames}")
    by_token: Dict[str, Dict[str, str]] = {}
    for row in reader:
        if (row.get("provider") or "").strip().lower() != "greenhouse":
            continue
        token = normalize_token(row.get("token", ""))
        if not token:
            continue
        by_token[token] = {
            "board_token": token,
            "company": token,
            "source_open_roles": (row.get("open_postings") or "").strip(),
            "source_board_url": (row.get("board_url") or "").strip(),
        }
    if not by_token:
        raise RuntimeError("Source B CSV contains no Greenhouse tokens")
    return set(by_token), by_token


def load_source_b_checkpoint(path: Path) -> Dict[str, Dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        document = read_json(path)
        if document.get("filter_version") != CORE.FILTER_VERSION:
            logging.info("CHECKPOINT invalidated: filter version changed")
            return {}
        results = document.get("results", {})
        return results if isinstance(results, dict) else {}
    except (OSError, json.JSONDecodeError):
        logging.exception("Could not read Source B checkpoint %s", path)
        return {}


def save_source_b_checkpoint(path: Path, results: Dict[str, Dict[str, Any]]) -> None:
    CORE.atomic_write_json(path, {
        "updated_at": CORE.utc_now(),
        "source": SOURCE_B_NAME,
        "source_url": SOURCE_B_URL,
        "source_data_url": SOURCE_B_DATA_URL,
        "filter_version": CORE.FILTER_VERSION,
        "results": results,
    })


def source_sets(source_a: set, source_b: set) -> Dict[str, set]:
    return {
        "overlap": source_a & source_b,
        "only_a": source_a - source_b,
        "only_b": source_b - source_a,
        "union": source_a | source_b,
    }


def select_only_b(tokens: Sequence[str], limit: Optional[int]) -> List[str]:
    selected = sorted(tokens)
    return selected[:limit] if limit is not None else selected


def scan_delta(
    selected: Sequence[str],
    source_b_rows: Dict[str, Dict[str, str]],
    checkpoint_path: Path,
    workers: int,
    timeout: float,
    max_attempts: int,
    backoff_base: float,
    checkpoint_every: int,
    progress_every: int,
) -> Tuple[List[Dict[str, Any]], int, int]:
    checkpoint = load_source_b_checkpoint(checkpoint_path)
    results: Dict[str, Dict[str, Any]] = {}
    pending: List[str] = []
    prior_attempts: Dict[str, int] = {}
    for token in selected:
        cached = checkpoint.get(token)
        if cached and cached.get("status") in CORE.SUCCESSFUL_CHECKPOINT_STATUSES:
            results[token] = cached
        else:
            pending.append(token)
            prior_attempts[token] = int((cached or {}).get("request_attempts", 0))

    logging.info("RESUME reused=%d pending=%d", len(results), len(pending))
    completed_since_save = 0
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(
                CORE.scan_board, source_b_rows[token], timeout, max_attempts, backoff_base
            ): token
            for token in pending
        }
        for future in as_completed(futures):
            token = futures[future]
            result = future.result()
            result["request_attempts"] = int(result.get("request_attempts", 0)) + prior_attempts[token]
            results[token] = result
            checkpoint[token] = result
            completed_since_save += 1
            if completed_since_save >= checkpoint_every:
                save_source_b_checkpoint(checkpoint_path, checkpoint)
                completed_since_save = 0
            done = len(results)
            if done % progress_every == 0 or done == len(selected):
                stats = CORE.summarize(results.values())
                failed = stats["REQUEST_FAILED"] + stats["RATE_LIMITED"]
                logging.info(
                    "PROGRESS boards=%d/%d live=%d relevant=%d failed=%d",
                    done, len(selected), stats["LIVE"], stats["relevant"], failed,
                )
    save_source_b_checkpoint(checkpoint_path, checkpoint)
    ordered = [results[token] for token in selected]
    total_attempts = sum(int(row.get("request_attempts", 0)) for row in ordered)
    first_attempts = sum(int(row.get("request_attempts", 0)) > 0 for row in ordered)
    retries = max(0, total_attempts - first_attempts)
    return ordered, first_attempts, retries


def build_merged_registry(
    source_a_relevant_path: Path,
    source_b_tokens: set,
    new_relevant: Sequence[Dict[str, Any]],
    generated_at: str,
) -> Dict[str, Any]:
    source_a = read_json(source_a_relevant_path)
    existing = source_a.get("boards")
    if not isinstance(existing, list):
        raise RuntimeError(f"Source A relevant registry has no boards list: {source_a_relevant_path}")
    merged: Dict[str, Dict[str, Any]] = {}
    for original in existing:
        token = normalize_token(original.get("board_token", ""))
        if not token:
            continue
        row = dict(original)
        row["board_token"] = token
        row["sources"] = ["kalebconfer"] + ([SOURCE_B_NAME] if token in source_b_tokens else [])
        merged[token] = row
    for original in new_relevant:
        token = normalize_token(original.get("board_token", ""))
        if token in merged:
            if SOURCE_B_NAME not in merged[token]["sources"]:
                merged[token]["sources"].append(SOURCE_B_NAME)
            continue
        row = dict(original)
        row["sources"] = [SOURCE_B_NAME]
        merged[token] = row
    return {
        "generated_at": generated_at,
        "sources": [
            {"name": "kalebconfer", "url": CORE.SOURCE_PAGE},
            {"name": SOURCE_B_NAME, "url": SOURCE_B_URL, "data_url": SOURCE_B_DATA_URL},
        ],
        "existing_relevant_boards": len(existing),
        "new_source_b_relevant_boards": len(new_relevant),
        "merged_unique_relevant_boards": len(merged),
        "boards": [merged[token] for token in sorted(merged)],
    }


def configure_logging(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(path, encoding="utf-8"), logging.StreamHandler()],
    )


def run(args: argparse.Namespace) -> int:
    configure_logging(args.log)
    source_a_tokens, _ = load_source_a(args.source_a_audit)
    source_b_tokens, source_b_rows = load_source_b(args.timeout)
    sets = source_sets(source_a_tokens, source_b_tokens)
    only_b = sorted(sets["only_b"])
    selected = select_only_b(only_b, args.limit)

    logging.info(
        "SOURCE_COMPARE source_a=%d source_b=%d overlap=%d only_a=%d only_b=%d union=%d",
        len(source_a_tokens), len(source_b_tokens), len(sets["overlap"]),
        len(sets["only_a"]), len(only_b), len(sets["union"]),
    )
    logging.info("REQUEST_PLAN selected_only_b=%d overlap_requested=0", len(selected))

    rows, first_attempts, retries = scan_delta(
        selected, source_b_rows, args.checkpoint, args.workers, args.timeout,
        args.max_attempts, args.backoff_base, args.checkpoint_every, args.progress_every,
    )
    stats = CORE.summarize(rows)
    new_relevant = [row for row in rows if row.get("geographic_match")]

    if args.no_output:
        logging.info(
            "TEST_DONE selected=%d first_attempts=%d retries=%d relevant=%d",
            len(selected), first_attempts, retries, len(new_relevant),
        )
        return 0
    if len(selected) != len(only_b):
        raise RuntimeError("refusing to write final outputs from a limited scan")

    generated_at = CORE.utc_now()
    comparison = {
        "generated_at": generated_at,
        "source_a": {"name": "kalebconfer directory", "token_count": len(source_a_tokens)},
        "source_b": {
            "name": SOURCE_B_NAME, "url": SOURCE_B_URL,
            "data_url": SOURCE_B_DATA_URL, "token_count": len(source_b_tokens),
        },
        "overlap": len(sets["overlap"]),
        "only_a": len(sets["only_a"]),
        "only_b": len(only_b),
        "union": len(sets["union"]),
        "new_tokens_planned": len(only_b),
        "new_tokens_requested": first_attempts,
        "first_attempt_api_requests": first_attempts,
        "retry_requests": retries,
        "total_api_request_attempts": first_attempts + retries,
        "overlap_tokens_requested": 0,
        "new_live": stats["LIVE"],
        "new_empty": stats["EMPTY"],
        "new_invalid": stats["INVALID"],
        "new_failed": stats["REQUEST_FAILED"],
        "new_rate_limited": stats["RATE_LIMITED"],
        "new_geographic_relevant": len(new_relevant),
    }
    audit = {
        "generated_at": generated_at,
        "source": SOURCE_B_NAME,
        "source_url": SOURCE_B_URL,
        "source_data_url": SOURCE_B_DATA_URL,
        "only_b_token_count": len(only_b),
        "summary": comparison,
        "boards": rows,
    }
    merged = build_merged_registry(
        args.source_a_relevant, source_b_tokens, new_relevant, generated_at
    )
    CORE.atomic_write_json(args.audit_output, audit)
    CORE.atomic_write_json(args.comparison_output, comparison)
    CORE.atomic_write_json(args.merged_output, merged)
    logging.info(
        "DONE planned=%d first_attempts=%d retries=%d new_relevant=%d merged=%d",
        len(only_b), first_attempts, retries, len(new_relevant),
        merged["merged_unique_relevant_boards"],
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-a-audit", type=Path, default=SOURCE_A_AUDIT)
    parser.add_argument("--source-a-relevant", type=Path, default=SOURCE_A_RELEVANT)
    parser.add_argument("--checkpoint", type=Path, default=DEFAULT_CHECKPOINT)
    parser.add_argument("--audit-output", type=Path, default=DEFAULT_AUDIT)
    parser.add_argument("--comparison-output", type=Path, default=DEFAULT_COMPARISON)
    parser.add_argument("--merged-output", type=Path, default=DEFAULT_MERGED)
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG)
    parser.add_argument("--workers", type=int, default=20)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--backoff-base", type=float, default=1.0)
    parser.add_argument("--checkpoint-every", type=int, default=25)
    parser.add_argument("--progress-every", type=int, default=100)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--no-output", action="store_true")
    return parser


if __name__ == "__main__":
    raise SystemExit(run(build_parser().parse_args()))
