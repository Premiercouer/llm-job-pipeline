#!/usr/bin/env python3
"""Stage 00: initialize an Ashby board pool by target geography.

This is a one-time board discovery utility. It does not perform freshness or
title filtering, touch seen state, or invoke an AI worker.
"""

from __future__ import annotations

import argparse
import sys
import csv
import importlib.util
import io
import json
import os
import random
import re
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


PROJECT_ROOT = Path(__file__).resolve().parent

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repository root
from common import paths  # noqa: E402

DATA_ROOT = paths.data_dir("discovery", "ashby")  # runtime state, logs, outputs
SOURCE_URL = (
    "https://raw.githubusercontent.com/fyrosofttech/lastroundai-hiring-data/"
    "main/ats-directory/lastroundai-ats-company-directory-2026-08.csv"
)
API_TEMPLATE = "https://api.ashbyhq.com/posting-api/job-board/{}"
DEFAULT_MASTER = DATA_ROOT / "config/ashby_boards_master.json"
DEFAULT_POOL = PROJECT_ROOT / "config/ashby_board_pool.json"
DEFAULT_EXCLUDED = DATA_ROOT / "config/ashby_boards_excluded.json"
DEFAULT_ERRORS = DATA_ROOT / "config/ashby_boards_errors.json"
DEFAULT_CHECKPOINT = DATA_ROOT / ".runtime/ashby_board_pool_checkpoint.json"
DEFAULT_REASSESS_CHECKPOINT = DATA_ROOT / ".runtime/ashby_indeterminate_reassessment_checkpoint.json"
DEFAULT_LOG = DATA_ROOT / "logs/00_initialize_board_pool.log"
DEFAULT_GREENHOUSE_RULE = PROJECT_ROOT.parent / "greenhouse/scripts/01_search_jobs.py"
USER_AGENT = "ashby-board-pool-initializer/1.0"
RETRYABLE_HTTP = {429, 500, 502, 503, 504}
RESUMABLE_FINAL_STATUSES = {
    "IN_POOL",
    "EXCLUDED_NO_TARGET_LOCATION",
    "INDETERMINATE_LOCATION",
    "HTTP_404",
}
SMOKE_BOARDS = ("docebo", "facilityos", "lightspeedhq", "1Password", "ashby", "notion", "cursor")
REASSESS_SMOKE_BOARDS = ("0g", "a-team", "amplify", "notion", "cursor")
REMOTE_TERMS = re.compile(r"(?<![a-z0-9])(remote|work from home|home-based|virtual|anywhere|worldwide|global)(?![a-z0-9])", re.I)
GENERIC_REMOTE_SCOPE_TERMS = re.compile(
    r"(?<![a-z0-9])(anywhere|worldwide|global|north america|americas)(?![a-z0-9])", re.I
)
FOREIGN_SCOPE_TERMS = (
    "united states", "usa", "u.s.a", "u.s.", "us", "united kingdom", "uk", "u.k.",
    "england", "scotland", "wales", "india", "australia", "new zealand", "germany",
    "france", "spain", "italy", "netherlands", "singapore", "mexico", "brazil",
    "colombia", "argentina", "south africa", "japan", "china", "philippines",
    "ireland", "poland", "portugal", "romania", "ukraine", "israel", "sweden",
    "norway", "denmark", "finland", "switzerland", "austria", "belgium", "greece",
    "bulgaria", "hungary", "croatia", "estonia", "lithuania", "latvia", "czechia",
    "czech republic", "slovakia", "serbia", "turkey", "united arab emirates", "uae",
    "saudi arabia", "qatar", "egypt", "nigeria", "kenya", "ghana", "pakistan",
    "bangladesh", "indonesia", "malaysia", "thailand", "vietnam", "south korea",
    "taiwan", "hong kong", "chile", "peru", "uruguay", "costa rica",
)
TORONTO_TERM = re.compile(r"(?<![a-z0-9])toronto(?![a-z0-9])", re.I)
GTA_TERMS = (
    "greater toronto area", "gta", "north york", "scarborough", "etobicoke",
    "mississauga", "brampton", "markham", "vaughan", "richmond hill",
    "oakville", "burlington", "milton", "pickering", "ajax", "whitby", "oshawa",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def http_get(url: str, timeout: float) -> Tuple[int, bytes, Dict[str, str]]:
    request = Request(url, headers={"Accept": "application/json,text/csv,*/*", "User-Agent": USER_AGENT})
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.status, response.read(), dict(response.headers.items())
    except HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers.items()) if exc.headers else {}


def load_greenhouse_matcher(path: Path) -> Callable[[str], Tuple[bool, Optional[str]]]:
    if not path.is_file():
        raise RuntimeError(f"Greenhouse location rule not found: {path}")
    spec = importlib.util.spec_from_file_location("greenhouse_stage01_location_rule", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import Greenhouse location rule: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    matcher = getattr(module, "match_target_location", None)
    if not callable(matcher):
        raise RuntimeError(f"match_target_location is missing from {path}")
    return matcher


def import_master(source_url: str, output: Path, timeout: float) -> Dict[str, Any]:
    status, body, _ = http_get(source_url, timeout)
    if status != 200:
        raise RuntimeError(f"board directory returned HTTP {status}")
    reader = csv.DictReader(io.StringIO(body.decode("utf-8-sig")))
    required = {"ats_vendor", "company_name", "board_slug"}
    missing = required - set(reader.fieldnames or [])
    if missing:
        raise RuntimeError(f"board directory missing columns: {sorted(missing)}")

    boards: List[Dict[str, str]] = []
    seen = set()
    for row in reader:
        if (row.get("ats_vendor") or "").strip().casefold() != "ashby":
            continue
        slug = (row.get("board_slug") or "").strip().strip("/")
        company = (row.get("company_name") or "").strip()
        if not slug or not company:
            continue
        identity = slug.casefold()
        if identity in seen:
            continue
        seen.add(identity)
        boards.append({"company": company, "board_slug": slug, "source": source_url})
    boards.sort(key=lambda row: row["board_slug"].casefold())
    if not boards:
        raise RuntimeError("board directory produced no Ashby boards")
    document = {
        "generated_at": utc_now(),
        "source": source_url,
        "board_count": len(boards),
        "boards": boards,
    }
    atomic_write_json(output, document)
    return document


def read_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def load_master(path: Path) -> List[Dict[str, str]]:
    document = read_json(path)
    boards = document.get("boards") if isinstance(document, dict) else None
    if not isinstance(boards, list):
        raise RuntimeError(f"master has no boards array: {path}")
    result = []
    seen = set()
    for row in boards:
        if not isinstance(row, dict):
            raise RuntimeError(f"invalid master row: {row!r}")
        company = row.get("company")
        slug = row.get("board_slug")
        source = row.get("source")
        if not all(isinstance(value, str) and value.strip() for value in (company, slug, source)):
            raise RuntimeError(f"invalid master row: {row!r}")
        identity = slug.casefold()
        if identity not in seen:
            seen.add(identity)
            result.append({"company": company, "board_slug": slug, "source": source})
    if not result:
        raise RuntimeError("master contains no boards")
    return result


def retry_delay(headers: Dict[str, str], attempt: int, base: float) -> float:
    retry_after = next((value for key, value in headers.items() if key.casefold() == "retry-after"), None)
    if retry_after:
        try:
            return max(0.0, min(float(retry_after), 120.0))
        except ValueError:
            try:
                parsed = parsedate_to_datetime(retry_after)
                return max(0.0, min((parsed - datetime.now(parsed.tzinfo or timezone.utc)).total_seconds(), 120.0))
            except (TypeError, ValueError, OverflowError):
                pass
    return base * (2 ** (attempt - 1)) + random.uniform(0, max(0.05, base * 0.25))


def fetch_board(slug: str, timeout: float, max_retries: int, backoff_base: float) -> Dict[str, Any]:
    url = API_TEMPLATE.format(quote(slug, safe=""))
    started = time.monotonic()
    attempts = 0
    last_error: Optional[str] = None
    while attempts <= max_retries:
        attempts += 1
        try:
            status, body, headers = http_get(url, timeout)
        except (URLError, TimeoutError, OSError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempts <= max_retries:
                time.sleep(retry_delay({}, attempts, backoff_base))
                continue
            return {
                "ok": False, "http_status": None, "error_type": "TRANSPORT_ERROR",
                "error": last_error, "request_attempts": attempts,
                "request_seconds": round(time.monotonic() - started, 3), "request_url": url,
            }

        if status == 404:
            return {
                "ok": False, "http_status": 404, "error_type": "HTTP_404",
                "error": "board endpoint returned HTTP 404", "request_attempts": attempts,
                "request_seconds": round(time.monotonic() - started, 3), "request_url": url,
            }
        if status in RETRYABLE_HTTP and attempts <= max_retries:
            last_error = f"HTTP {status}"
            time.sleep(retry_delay(headers, attempts, backoff_base))
            continue
        if status != 200:
            return {
                "ok": False, "http_status": status, "error_type": f"HTTP_{status}",
                "error": f"board endpoint returned HTTP {status}", "request_attempts": attempts,
                "request_seconds": round(time.monotonic() - started, 3), "request_url": url,
            }
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return {
                "ok": False, "http_status": 200, "error_type": "INVALID_JSON",
                "error": f"{type(exc).__name__}: {exc}", "request_attempts": attempts,
                "request_seconds": round(time.monotonic() - started, 3), "request_url": url,
            }
        if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
            return {
                "ok": False, "http_status": 200, "error_type": "INVALID_SCHEMA",
                "error": "response is not an object with a jobs array", "request_attempts": attempts,
                "request_seconds": round(time.monotonic() - started, 3), "request_url": url,
            }
        return {
            "ok": True, "http_status": 200, "jobs": payload["jobs"], "request_attempts": attempts,
            "request_seconds": round(time.monotonic() - started, 3), "request_url": url,
        }
    raise AssertionError(f"unreachable fetch loop; last error={last_error}")


def string_value(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def address_text(value: Any) -> Tuple[str, List[str]]:
    problems: List[str] = []
    if value is None:
        return "", problems
    if not isinstance(value, dict):
        return "", ["address_not_object"]
    postal = value.get("postalAddress")
    if postal is None:
        return "", problems
    if not isinstance(postal, dict):
        return "", ["postalAddress_not_object"]
    parts = []
    for key in ("streetAddress", "addressLocality", "addressRegion", "postalCode", "addressCountry"):
        item = postal.get(key)
        if item is not None and not isinstance(item, str):
            problems.append(f"postalAddress.{key}_not_string")
        elif isinstance(item, str) and item.strip():
            parts.append(item.strip())
    return ", ".join(parts), problems


def phrase_present(text: str, phrase: str) -> bool:
    return bool(re.search(r"(?<![a-z0-9])" + re.escape(phrase) + r"(?![a-z0-9])", text.casefold()))


def address_country(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    postal = value.get("postalAddress")
    if not isinstance(postal, dict):
        return ""
    country = postal.get("addressCountry")
    return country.strip() if isinstance(country, str) else ""


def explicit_foreign_remote_scope(location: str, address: Any, remote_arrangement: bool) -> bool:
    """True only when the work-location text explicitly restricts remote work abroad.

    An office address alone is deliberately insufficient. The raw location text
    must itself contain a foreign country/scope. Generic multi-country scopes
    such as Americas and North America stay indeterminate.
    """
    if not remote_arrangement or not location.strip() or GENERIC_REMOTE_SCOPE_TERMS.search(location):
        return False
    normalized = location.casefold()
    if phrase_present(normalized, "canada") or phrase_present(normalized, "canadian"):
        return False
    terms = list(FOREIGN_SCOPE_TERMS)
    country = address_country(address)
    if country and country.casefold() not in {"canada", ""}:
        terms.append(country.casefold())
    return any(phrase_present(normalized, term) for term in terms)


def categorize_gta(text: str) -> str:
    if TORONTO_TERM.search(text):
        return "toronto"
    lowered = text.casefold()
    if any(re.search(r"(?<![a-z0-9])" + re.escape(place) + r"(?![a-z0-9])", lowered) for place in GTA_TERMS):
        return "gta"
    return "gta"


def evaluate_job_location(
    job: Any, matcher: Callable[[str], Tuple[bool, Optional[str]]]
) -> Dict[str, Any]:
    if not isinstance(job, dict):
        return {"listed": False, "matched": False, "indeterminate": True, "reason": "job_not_object"}
    if job.get("isListed") is not True:
        return {"listed": False, "matched": False, "indeterminate": False, "reason": "not_listed"}

    problems: List[str] = []
    location = string_value(job.get("location"))
    if job.get("location") is not None and not isinstance(job.get("location"), str):
        problems.append("location_not_string")
    primary_address, address_problems = address_text(job.get("address"))
    problems.extend(address_problems)
    raw_contexts: List[Dict[str, Any]] = [{
        "source": "primary", "location": location, "address": job.get("address"),
        "address_text": primary_address,
    }]

    secondary = job.get("secondaryLocations")
    if secondary is None:
        secondary = []
    if not isinstance(secondary, list):
        problems.append("secondaryLocations_not_array")
        secondary = []
    for index, item in enumerate(secondary):
        if not isinstance(item, dict):
            problems.append(f"secondaryLocations[{index}]_not_object")
            continue
        secondary_location = string_value(item.get("location"))
        if item.get("location") is not None and not isinstance(item.get("location"), str):
            problems.append(f"secondaryLocations[{index}].location_not_string")
        secondary_address, secondary_problems = address_text(item.get("address"))
        problems.extend(f"secondaryLocations[{index}].{problem}" for problem in secondary_problems)
        raw_contexts.append({
            "source": f"secondaryLocations[{index}]", "location": secondary_location,
            "address": item.get("address"), "address_text": secondary_address,
        })

    workplace = string_value(job.get("workplaceType"))
    is_remote = job.get("isRemote") is True
    workplace_remote = workplace.casefold() == "remote"
    evaluation_contexts: List[Dict[str, Any]] = []
    for context in raw_contexts:
        location_remote = bool(
            REMOTE_TERMS.search(context["location"]) or GENERIC_REMOTE_SCOPE_TERMS.search(context["location"])
        )
        remote_arrangement = is_remote or workplace_remote or location_remote
        match_parts = [context["location"], context["address_text"]]
        if remote_arrangement:
            match_parts.append("Remote")
        match_input = " | ".join(part for part in match_parts if part)
        if not match_input:
            continue
        evaluation_contexts.append({
            **context,
            "remote_signals": [
                signal for signal, enabled in (
                    ("location_text", location_remote), ("isRemote", is_remote),
                    ("workplaceType", workplace_remote),
                ) if enabled
            ],
            "match_input": match_input,
            "explicit_foreign_remote_scope": explicit_foreign_remote_scope(
                context["location"], context["address"], remote_arrangement
            ),
        })

    for context in evaluation_contexts:
        matched, reason = matcher(context["match_input"])
        if matched:
            category = "canada_remote" if reason == "canada_remote" else categorize_gta(context["match_input"])
            return {
                "listed": True, "matched": True, "indeterminate": False,
                "category": category, "reason": f"target_{category}",
                "evidence": context["match_input"], "evaluation_contexts": evaluation_contexts,
            }

    combined = " | ".join(context["match_input"] for context in evaluation_contexts)
    if problems:
        return {
            "listed": True, "matched": False, "indeterminate": True,
            "reason": "malformed_location_fields", "details": sorted(set(problems)),
            "evidence": combined, "evaluation_contexts": evaluation_contexts,
        }
    if not combined:
        return {
            "listed": True, "matched": False, "indeterminate": True,
            "reason": "missing_location_evidence", "evidence": "",
            "evaluation_contexts": evaluation_contexts,
        }
    remote_contexts = [context for context in evaluation_contexts if context["remote_signals"]]
    if remote_contexts and all(context["explicit_foreign_remote_scope"] for context in remote_contexts):
        return {
            "listed": True, "matched": False, "indeterminate": False,
            "reason": "definite_non_target_location", "details": ["explicit_non_canada_remote_scope"],
            "evidence": combined, "evaluation_contexts": evaluation_contexts,
        }
    if remote_contexts:
        return {
            "listed": True, "matched": False, "indeterminate": True,
            "reason": "remote_without_explicit_canada_evidence", "evidence": combined,
            "evaluation_contexts": evaluation_contexts,
        }
    return {
        "listed": True, "matched": False, "indeterminate": False,
        "reason": "definite_non_target_location", "evidence": combined,
        "evaluation_contexts": evaluation_contexts,
    }


def scan_board(
    board: Dict[str, str], matcher: Callable[[str], Tuple[bool, Optional[str]]],
    timeout: float, max_retries: int, backoff_base: float,
) -> Dict[str, Any]:
    checked_at = utc_now()
    fetched = fetch_board(board["board_slug"], timeout, max_retries, backoff_base)
    base: Dict[str, Any] = {
        "company": board["company"], "board_slug": board["board_slug"], "source": board["source"],
        "total_jobs": 0, "matching_location_jobs": 0, "checked_at": checked_at,
        "http_status": fetched.get("http_status"), "request_attempts": fetched["request_attempts"],
        "request_seconds": fetched["request_seconds"], "request_url": fetched["request_url"],
        "location_match_counts": {"toronto": 0, "gta": 0, "canada_remote": 0},
        "matched_job_samples": [], "indeterminate_location_jobs": 0,
        "indeterminate_samples": [], "indeterminate_jobs": [],
        "classification_reason_counts": {},
    }
    if not fetched["ok"]:
        base.update({
            "status": fetched["error_type"], "error_type": fetched["error_type"],
            "error": fetched["error"],
        })
        return base

    jobs = fetched["jobs"]
    base["total_jobs"] = len(jobs)
    for index, job in enumerate(jobs):
        evaluation = evaluate_job_location(job, matcher)
        reason = evaluation["reason"]
        base["classification_reason_counts"][reason] = base["classification_reason_counts"].get(reason, 0) + 1
        if evaluation["matched"]:
            base["matching_location_jobs"] += 1
            base["location_match_counts"][evaluation["category"]] += 1
            if len(base["matched_job_samples"]) < 10:
                base["matched_job_samples"].append({
                    "job_id": job.get("id") if isinstance(job, dict) else None,
                    "title": job.get("title") if isinstance(job, dict) else None,
                    "category": evaluation["category"], "reason": evaluation["reason"],
                    "evidence": evaluation["evidence"],
                })
        elif evaluation["indeterminate"]:
            base["indeterminate_location_jobs"] += 1
            diagnostic = {
                "job_index": index,
                "job_id": job.get("id") if isinstance(job, dict) else None,
                "title": job.get("title") if isinstance(job, dict) else None,
                "location": job.get("location") if isinstance(job, dict) else None,
                "secondaryLocations": job.get("secondaryLocations") if isinstance(job, dict) else None,
                "address": job.get("address") if isinstance(job, dict) else None,
                "isRemote": job.get("isRemote") if isinstance(job, dict) else None,
                "workplaceType": job.get("workplaceType") if isinstance(job, dict) else None,
                "evaluation_contexts": evaluation.get("evaluation_contexts", []),
                "evaluation_reason": evaluation["reason"],
                "details": evaluation.get("details", []),
            }
            base["indeterminate_jobs"].append(diagnostic)
            if len(base["indeterminate_samples"]) < 10:
                base["indeterminate_samples"].append(diagnostic)

    if base["matching_location_jobs"]:
        base["status"] = "IN_POOL"
    elif base["indeterminate_location_jobs"]:
        base.update({
            "status": "INDETERMINATE_LOCATION", "error_type": "INDETERMINATE_LOCATION",
            "error": "no definite target match and at least one listed job has ambiguous location evidence",
        })
    else:
        base["status"] = "EXCLUDED_NO_TARGET_LOCATION"
    return base


class Checkpoint:
    def __init__(self, path: Path, master_path: Path, reset: bool = False):
        self.path = path
        self.master_path = str(master_path.resolve())
        self.lock = threading.Lock()
        self.results: Dict[str, Dict[str, Any]] = {}
        if path.exists() and not reset:
            document = read_json(path)
            if isinstance(document, dict) and document.get("master_path") == self.master_path:
                rows = document.get("results", {})
                if isinstance(rows, dict):
                    self.results = {key: value for key, value in rows.items() if isinstance(value, dict)}

    def completed(self, slug: str) -> bool:
        row = self.results.get(slug.casefold())
        return bool(row and row.get("status") in RESUMABLE_FINAL_STATUSES)

    def put(self, result: Dict[str, Any]) -> None:
        with self.lock:
            self.results[result["board_slug"].casefold()] = result
            atomic_write_json(self.path, {
                "updated_at": utc_now(), "master_path": self.master_path, "results": self.results,
            })


def output_document(kind: str, rows: List[Dict[str, Any]], summary: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "generated_at": utc_now(), "kind": kind, "board_count": len(rows),
        "scan_summary": summary, "boards": rows,
    }


def build_summary(results: Sequence[Dict[str, Any]], started: float, resumed: int) -> Dict[str, Any]:
    successful = [row for row in results if row.get("http_status") == 200]
    pool = [row for row in results if row.get("status") == "IN_POOL"]
    excluded = [row for row in results if row.get("status") == "EXCLUDED_NO_TARGET_LOCATION"]
    errors = [row for row in results if row.get("status") not in {"IN_POOL", "EXCLUDED_NO_TARGET_LOCATION"}]
    error_types: Dict[str, int] = {}
    for row in errors:
        key = row.get("error_type") or row.get("status") or "UNKNOWN"
        error_types[key] = error_types.get(key, 0) + 1
    reason_counts: Dict[str, int] = {}
    for row in results:
        for reason, count in row.get("classification_reason_counts", {}).items():
            reason_counts[reason] = reason_counts.get(reason, 0) + count
    return {
        "candidate_boards": len(results), "api_success": len(successful),
        "api_failed": len(results) - len(successful), "error_classification": dict(sorted(error_types.items())),
        "pool_boards": len(pool), "excluded_boards": len(excluded), "error_boards": len(errors),
        "boards_with_toronto_match": sum(row["location_match_counts"]["toronto"] > 0 for row in pool),
        "boards_with_gta_match": sum(row["location_match_counts"]["gta"] > 0 for row in pool),
        "boards_with_canada_remote_match": sum(row["location_match_counts"]["canada_remote"] > 0 for row in pool),
        "pool_total_jobs": sum(row["total_jobs"] for row in pool),
        "pool_matching_location_jobs": sum(row["matching_location_jobs"] for row in pool),
        "resumed_boards": resumed, "execution_seconds": round(time.monotonic() - started, 3),
        "sum_request_seconds": round(sum(row.get("request_seconds", 0) for row in results), 3),
        "classification_reason_counts": dict(sorted(reason_counts.items())),
    }


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument("--backoff-base", type=float, default=1.0)
    parser.add_argument("--source-url", default=SOURCE_URL)
    parser.add_argument("--master", type=Path, default=DEFAULT_MASTER)
    parser.add_argument("--pool", type=Path, default=DEFAULT_POOL)
    parser.add_argument("--excluded", type=Path, default=DEFAULT_EXCLUDED)
    parser.add_argument("--errors", type=Path, default=DEFAULT_ERRORS)
    parser.add_argument("--baseline-pool", type=Path, default=DEFAULT_POOL)
    parser.add_argument("--baseline-excluded", type=Path, default=DEFAULT_EXCLUDED)
    parser.add_argument("--baseline-errors", type=Path, default=DEFAULT_ERRORS)
    parser.add_argument("--checkpoint", type=Path, default=DEFAULT_CHECKPOINT)
    parser.add_argument("--greenhouse-rule", type=Path, default=DEFAULT_GREENHOUSE_RULE)
    parser.add_argument("--refresh-master", action="store_true")
    parser.add_argument("--reset-checkpoint", action="store_true")
    parser.add_argument("--boards", help="comma-separated board slugs from master")
    parser.add_argument("--smoke-test", action="store_true", help="scan the seven previously successful probe boards")
    parser.add_argument(
        "--reassess-indeterminate", action="store_true",
        help="rescan only INDETERMINATE_LOCATION boards from the existing errors file and merge results",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    if args.workers < 1 or args.timeout <= 0 or args.max_retries < 0 or args.backoff_base < 0:
        raise SystemExit("workers/timeout must be positive; retries/backoff must be non-negative")
    started = time.monotonic()
    if args.refresh_master or not args.master.exists():
        master_document = import_master(args.source_url, args.master, args.timeout)
        print(f"MASTER boards={master_document['board_count']} path={args.master}", flush=True)
    boards = load_master(args.master)
    baseline_pool: List[Dict[str, Any]] = []
    baseline_excluded: List[Dict[str, Any]] = []
    baseline_errors: List[Dict[str, Any]] = []
    original_indeterminate: List[Dict[str, Any]] = []
    if args.reassess_indeterminate:
        baseline_pool = read_json(args.baseline_pool).get("boards", [])
        baseline_excluded = read_json(args.baseline_excluded).get("boards", [])
        baseline_errors = read_json(args.baseline_errors).get("boards", [])
        original_indeterminate = [
            row for row in baseline_errors if row.get("status") == "INDETERMINATE_LOCATION"
        ]
        boards = [
            {"company": row["company"], "board_slug": row["board_slug"], "source": row["source"]}
            for row in original_indeterminate
        ]
        if args.checkpoint == DEFAULT_CHECKPOINT:
            args.checkpoint = DEFAULT_REASSESS_CHECKPOINT

    selected_slugs: Optional[set[str]] = None
    if args.smoke_test:
        smoke_boards = REASSESS_SMOKE_BOARDS if args.reassess_indeterminate else SMOKE_BOARDS
        selected_slugs = {slug.casefold() for slug in smoke_boards}
    elif args.boards:
        selected_slugs = {slug.strip().casefold() for slug in args.boards.split(",") if slug.strip()}
    if selected_slugs is not None:
        boards = [board for board in boards if board["board_slug"].casefold() in selected_slugs]
        found = {board["board_slug"].casefold() for board in boards}
        missing = selected_slugs - found
        if missing:
            raise RuntimeError(f"selected board slugs are absent from master: {sorted(missing)}")
    if not boards:
        raise RuntimeError("no boards selected")

    matcher = load_greenhouse_matcher(args.greenhouse_rule)
    checkpoint = Checkpoint(args.checkpoint, args.master, reset=args.reset_checkpoint)
    resumed = sum(checkpoint.completed(board["board_slug"]) for board in boards)
    pending = [board for board in boards if not checkpoint.completed(board["board_slug"])]
    print(f"SCAN candidates={len(boards)} resumed={resumed} pending={len(pending)} workers={args.workers}", flush=True)

    with ThreadPoolExecutor(max_workers=args.workers, thread_name_prefix="ashby-board") as executor:
        futures = {
            executor.submit(scan_board, board, matcher, args.timeout, args.max_retries, args.backoff_base): board
            for board in pending
        }
        for number, future in enumerate(as_completed(futures), start=1):
            board = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                result = {
                    "company": board["company"], "board_slug": board["board_slug"], "source": board["source"],
                    "total_jobs": 0, "matching_location_jobs": 0, "checked_at": utc_now(),
                    "status": "UNEXPECTED_ERROR", "error_type": "UNEXPECTED_ERROR",
                    "error": f"{type(exc).__name__}: {exc}", "http_status": None,
                    "request_attempts": 0, "request_seconds": 0.0,
                    "location_match_counts": {"toronto": 0, "gta": 0, "canada_remote": 0},
                    "matched_job_samples": [], "indeterminate_location_jobs": 0,
                    "indeterminate_samples": [], "indeterminate_jobs": [],
                    "classification_reason_counts": {},
                }
            checkpoint.put(result)
            print(
                f"BOARD {resumed + number}/{len(boards)} slug={result['board_slug']} "
                f"status={result['status']} jobs={result['total_jobs']} "
                f"matches={result['matching_location_jobs']} seconds={result['request_seconds']}",
                flush=True,
            )

    results = [checkpoint.results[board["board_slug"].casefold()] for board in boards]
    if args.reassess_indeterminate:
        rescanned_by_slug = {row["board_slug"].casefold(): row for row in results}
        untouched_indeterminate = [
            row for row in original_indeterminate if row["board_slug"].casefold() not in rescanned_by_slug
        ]
        pool_rows = baseline_pool + [row for row in results if row["status"] == "IN_POOL"]
        excluded_rows = baseline_excluded + [
            row for row in results if row["status"] == "EXCLUDED_NO_TARGET_LOCATION"
        ]
        error_rows = [
            row for row in baseline_errors if row.get("status") != "INDETERMINATE_LOCATION"
        ] + untouched_indeterminate + [
            row for row in results if row["status"] not in {"IN_POOL", "EXCLUDED_NO_TARGET_LOCATION"}
        ]
        all_results = pool_rows + excluded_rows + error_rows
        summary = build_summary(all_results, started, resumed)
        summary["reassessment"] = {
            "selected_boards": len(results),
            "added_to_pool": sum(row["status"] == "IN_POOL" for row in results),
            "moved_to_excluded": sum(row["status"] == "EXCLUDED_NO_TARGET_LOCATION" for row in results),
            "remaining_indeterminate": sum(row["status"] == "INDETERMINATE_LOCATION" for row in results),
            "request_failures": sum(
                row["status"] not in {"IN_POOL", "EXCLUDED_NO_TARGET_LOCATION", "INDETERMINATE_LOCATION"}
                for row in results
            ),
            "classification_reason_counts": build_summary(results, started, resumed)["classification_reason_counts"],
        }
    else:
        summary = build_summary(results, started, resumed)
        pool_rows = [row for row in results if row["status"] == "IN_POOL"]
        excluded_rows = [row for row in results if row["status"] == "EXCLUDED_NO_TARGET_LOCATION"]
        error_rows = [row for row in results if row["status"] not in {"IN_POOL", "EXCLUDED_NO_TARGET_LOCATION"}]
    pool_rows = sorted(pool_rows, key=lambda row: row["board_slug"].casefold())
    excluded_rows = sorted(excluded_rows, key=lambda row: row["board_slug"].casefold())
    error_rows = sorted(error_rows, key=lambda row: row["board_slug"].casefold())
    atomic_write_json(args.pool, output_document("ashby_board_pool", pool_rows, summary))
    atomic_write_json(args.excluded, output_document("ashby_boards_excluded", excluded_rows, summary))
    atomic_write_json(args.errors, output_document("ashby_boards_errors", error_rows, summary))
    print("SUMMARY " + json.dumps(summary, ensure_ascii=False, sort_keys=True), flush=True)
    print(f"OUTPUT pool={args.pool} excluded={args.excluded} errors={args.errors}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
