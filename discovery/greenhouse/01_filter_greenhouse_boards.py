#!/usr/bin/env python3
"""Build a Canada-relevant registry from the verified Greenhouse directory.

This intentionally uses only Greenhouse's inexpensive board jobs list endpoint.
It does not fetch job descriptions or filter by job title/date.
"""

from __future__ import annotations

import argparse
import sys
import csv
import io
import json
import logging
import random
import re
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from http.client import IncompleteRead
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


SOURCE_URL = "https://kalebconfer-sys.github.io/job-board-directory/greenhouse.csv"
SOURCE_PAGE = "https://kalebconfer-sys.github.io/job-board-directory/greenhouse.html"
API_TEMPLATE = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs"

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repository root
from common import paths  # noqa: E402

DATA_ROOT = paths.data_dir("discovery", "greenhouse")  # runtime state, logs, outputs
DEFAULT_CHECKPOINT = DATA_ROOT / (".runtime/greenhouse_board_scan_checkpoint.json")
DEFAULT_LOG = DATA_ROOT / ("logs/01_filter_greenhouse_boards.log")
DEFAULT_RELEVANT_OUTPUT = DATA_ROOT / ("outputs/greenhouse_boards_relevant.json")
DEFAULT_AUDIT_OUTPUT = DATA_ROOT / ("outputs/greenhouse_board_scan_audit.json")
USER_AGENT = "greenhouse-board-geographic-bootstrap/1.0"
FILTER_VERSION = 5
SUCCESSFUL_CHECKPOINT_STATUSES = {"LIVE", "EMPTY", "INVALID"}
RETRYABLE_HTTP_STATUSES = {429, 500, 502, 503, 504}


# Full province/territory names are safe signals. Two-letter abbreviations are
# checked as delimited location components below, rather than arbitrary words.
CANADIAN_REGIONS = (
    "alberta",
    "british columbia",
    "manitoba",
    "new brunswick",
    "newfoundland and labrador",
    "newfoundland",
    "northwest territories",
    "nova scotia",
    "nunavut",
    "ontario",
    "prince edward island",
    "quebec",
    "saskatchewan",
    "yukon",
)
CANADIAN_REGION_CODES = {
    "ab", "bc", "mb", "nb", "nl", "ns", "nt", "nu", "on", "pe", "qc", "sk", "yt"
}

# These are only used to disambiguate place names shared with Canada (for
# example Burlington, Massachusetts). An explicit Canada signal above always
# wins, so multi-country locations containing Canada are still retained.
EXPLICIT_FOREIGN_MARKERS = (
    "united states", "u.s.a", "usa", "u.s.", "united kingdom", "u.k.", "uk",
    "england", "scotland",
    "wales", "ireland", "australia", "new zealand", "india", "germany",
    "france", "spain", "italy", "netherlands", "singapore", "mexico",
    "brazil", "colombia", "argentina", "south africa", "japan", "china",
)
US_STATE_NAMES = (
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
    "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana",
    "maine", "maryland", "massachusetts", "michigan", "minnesota",
    "mississippi", "missouri", "montana", "nebraska", "nevada",
    "new hampshire", "new jersey", "new mexico", "new york",
    "north carolina", "north dakota", "ohio", "oklahoma", "oregon",
    "pennsylvania", "rhode island", "south carolina", "south dakota",
    "tennessee", "texas", "utah", "vermont", "virginia", "washington",
    "west virginia", "wisconsin", "wyoming", "district of columbia",
)
US_STATE_CODES = {
    "al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga", "hi",
    "id", "il", "in", "ia", "ks", "ky", "la", "me", "md", "ma", "mi",
    "mn", "ms", "mo", "mt", "ne", "nv", "nh", "nj", "nm", "ny", "nc",
    "nd", "oh", "ok", "or", "pa", "ri", "sc", "sd", "tn", "tx", "ut",
    "vt", "va", "wa", "wv", "wi", "wy", "dc",
}

# City names are useful for terse Greenhouse locations such as just "Toronto"
# or "Vancouver". The list is deliberately broad because this is a high-recall
# board pre-filter, not a final job-location decision.
CANADIAN_PLACES = (
    "ajax", "barrie", "brampton", "brantford", "burlington", "calgary",
    "cambridge", "charlottetown", "edmonton", "etobicoke", "fredericton",
    "gatineau", "greater toronto area", "gta", "guelph", "halifax",
    "hamilton", "kelowna", "kingston", "kitchener", "laval",
    "markham", "milton", "mississauga", "moncton", "montreal", "north york",
    "oakville", "oshawa", "ottawa", "pickering", "quebec city", "regina",
    "richmond hill", "saskatoon", "scarborough", "st. catharines",
    "st john's", "st. john's", "surrey", "thunder bay", "toronto", "vancouver",
    "vaughan", "victoria", "waterloo", "whitby", "windsor", "winnipeg",
    "yellowknife",
)
STRONG_GTA_PLACES = (
    "toronto", "greater toronto area", "gta", "north york", "scarborough",
    "etobicoke", "mississauga", "brampton", "markham", "vaughan",
    "richmond hill", "oakville", "milton", "pickering", "ajax", "whitby",
    "oshawa",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_location(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.casefold().replace("&", " and ")
    value = re.sub(r"[\u2010-\u2015]", "-", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def _contains_phrase(text: str, phrase: str) -> bool:
    return bool(re.search(r"(?<![a-z0-9])" + re.escape(phrase) + r"(?![a-z0-9])", text))


def match_canadian_location(location: str) -> Tuple[bool, Optional[str]]:
    """Return a conservative explicit-Canada match and an auditable reason."""
    normalized = normalize_location(location)
    if not normalized:
        return False, None

    if _contains_phrase(normalized, "canada") or _contains_phrase(normalized, "canadian"):
        return True, "country"

    # Some feeds use CAN as a country component. Preserve its original case so
    # ordinary words beginning with "can" do not become country signals.
    if re.search(r"(?:^|[,;/|])\s*CAN(?=\s*(?:$|[,;/|]|\bOR\b|\bAND\b))", location):
        return True, "country_code"

    us_region_alternatives = "|".join(
        [re.escape(state) for state in US_STATE_NAMES] + sorted(US_STATE_CODES)
    )
    canadian_place_followed_by_us_region = any(
        re.search(
            re.escape(place) + r"\s*,\s*(?:" + us_region_alternatives + r")(?:\b|$)",
            normalized,
        )
        for place in CANADIAN_PLACES
    )
    if canadian_place_followed_by_us_region:
        return False, None

    # These GTA names are sufficiently strong for this specific registry and
    # can occur in multi-location strings that also mention the US.
    if any(_contains_phrase(normalized, place) for place in STRONG_GTA_PLACES):
        return True, "gta_place"

    explicit_foreign_country = (
        any(_contains_phrase(normalized, marker) for marker in EXPLICIT_FOREIGN_MARKERS)
        or bool(re.search(r"\bUS\b|\bU\.S\.", location))
    )
    if explicit_foreign_country:
        return False, None

    # Treat an abbreviation as Canadian only when it is a whole delimited
    # location component. This recognizes "Remote - ON" and "ON / Remote"
    # while avoiding prose such as "work on site".
    components = [
        part.strip(" .()[]{}")
        for part in re.split(r"[,/|;]|\s+-\s+", normalized)
    ]
    canadian_code_pattern = (
        r"(?:^|[,;/|]|\s-\s)\s*(?:"
        + "|".join(sorted(CANADIAN_REGION_CODES))
        + r")(?=\s*(?:$|[,;/|)]|\bor\b|\band\b))"
    )
    if (
        any(component in CANADIAN_REGION_CODES for component in components)
        or re.search(canadian_code_pattern, normalized)
    ):
        return True, "province_or_territory_code"

    # Resolve patterns such as Ontario, CA and Yukon, OK as US locations. A
    # Canadian region followed immediately by a US postal code is explicit
    # foreign evidence, not a Canadian province signal.
    us_code_pattern = r"(?:^|[,;/|]|\s-\s)\s*(?:" + "|".join(sorted(US_STATE_CODES)) + r")(?:\b|$)"
    canadian_region_followed_by_us_code = any(
        re.search(re.escape(region) + r"\s*,\s*(?:" + "|".join(sorted(US_STATE_CODES)) + r")(?:\b|$)", normalized)
        for region in CANADIAN_REGIONS
    )
    if not canadian_region_followed_by_us_code:
        for region in CANADIAN_REGIONS:
            if _contains_phrase(normalized, region):
                return True, "province_or_territory"

    # Do not let an ambiguous Canadian city name override a clearly stated US
    # state name or postal abbreviation.
    if (
        any(_contains_phrase(normalized, state) for state in US_STATE_NAMES)
        or re.search(us_code_pattern, normalized)
    ):
        return False, None

    for place in CANADIAN_PLACES:
        if _contains_phrase(normalized, place):
            return True, "canadian_place"

    return False, None


def http_get(url: str, timeout: float) -> Tuple[int, bytes, Dict[str, str]]:
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json,text/csv,*/*"})
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.status, response.read(), dict(response.headers.items())
    except HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers.items()) if exc.headers else {}


def load_source_boards(timeout: float) -> Tuple[List[Dict[str, str]], str]:
    status, payload, _ = http_get(SOURCE_URL, timeout)
    if status != 200:
        raise RuntimeError(f"directory CSV returned HTTP {status}")
    text = payload.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    required = {"platform", "token", "company", "board_url", "api_url", "open_roles"}
    actual = set(reader.fieldnames or [])
    missing = required - actual
    if missing:
        raise RuntimeError(f"directory CSV missing fields: {sorted(missing)}; actual={reader.fieldnames}")

    boards: List[Dict[str, str]] = []
    seen = set()
    for row in reader:
        token = (row.get("token") or "").strip()
        if not token or token in seen:
            continue
        if (row.get("platform") or "").strip().casefold() != "greenhouse":
            continue
        seen.add(token)
        boards.append({
            "board_token": token,
            "company": (row.get("company") or "").strip(),
            "source_open_roles": (row.get("open_roles") or "").strip(),
            "source_board_url": (row.get("board_url") or "").strip(),
        })
    if not boards:
        raise RuntimeError("directory CSV contained no Greenhouse board tokens")
    return boards, text


def parse_retry_after(headers: Dict[str, str]) -> Optional[float]:
    value = next((v for k, v in headers.items() if k.casefold() == "retry-after"), None)
    if value is None:
        return None
    try:
        return max(0.0, min(float(value), 60.0))
    except ValueError:
        return None


def fetch_json_endpoint(
    url: str, timeout: float, max_attempts: int, backoff_base: float
) -> Dict[str, Any]:
    """Fetch a public JSON endpoint with shared retry and status handling."""
    last_error = "unknown error"
    exhausted_status = "REQUEST_FAILED"
    for attempt in range(1, max_attempts + 1):
        try:
            status, payload, headers = http_get(url, timeout)
            if status == 404:
                return {
                    "status": "NOT_FOUND", "data": None,
                    "error": "HTTP 404", "request_attempts": attempt,
                }
            if status in RETRYABLE_HTTP_STATUSES:
                exhausted_status = "RATE_LIMITED" if status == 429 else "REQUEST_FAILED"
                last_error = f"HTTP {status}"
                if attempt < max_attempts:
                    delay = parse_retry_after(headers)
                    if delay is None:
                        delay = backoff_base * (2 ** (attempt - 1)) + random.uniform(0, backoff_base)
                    time.sleep(delay)
                    continue
                break
            if status != 200:
                last_error = f"HTTP {status}"
                break
            try:
                decoded = json.loads(payload)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                last_error = f"invalid JSON: {exc}"
                if attempt < max_attempts:
                    time.sleep(backoff_base * (2 ** (attempt - 1)))
                    continue
                return {
                    "status": "INVALID_JSON", "data": None,
                    "error": last_error, "request_attempts": attempt,
                }
            return {"status": "SUCCESS", "data": decoded, "request_attempts": attempt}
        except (URLError, TimeoutError, OSError, IncompleteRead) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < max_attempts:
                time.sleep(backoff_base * (2 ** (attempt - 1)) + random.uniform(0, backoff_base))
                continue
            break

    return {
        "status": exhausted_status, "data": None, "error": last_error,
        "request_attempts": max_attempts,
    }


def fetch_board_jobs(
    token: str, timeout: float, max_attempts: int, backoff_base: float
) -> Dict[str, Any]:
    """Fetch one board's list endpoint with the shared retry/status policy."""
    url = API_TEMPLATE.format(token=quote(token, safe=""))
    fetched = fetch_json_endpoint(url, timeout, max_attempts, backoff_base)
    if fetched["status"] == "NOT_FOUND":
        return {
            "status": "INVALID", "jobs": [], "error": fetched["error"],
            "request_attempts": fetched["request_attempts"],
        }
    if fetched["status"] != "SUCCESS":
        status = fetched["status"] if fetched["status"] == "RATE_LIMITED" else "REQUEST_FAILED"
        return {
            "status": status, "jobs": [], "error": fetched.get("error", fetched["status"]),
            "request_attempts": fetched["request_attempts"],
        }
    decoded = fetched["data"]
    jobs = decoded.get("jobs") if isinstance(decoded, dict) else None
    if not isinstance(jobs, list):
        return {
            "status": "REQUEST_FAILED", "jobs": [],
            "error": "response did not contain a jobs list",
            "request_attempts": fetched["request_attempts"],
        }
    return {
        "status": "LIVE" if jobs else "EMPTY", "jobs": jobs,
        "request_attempts": fetched["request_attempts"],
    }


def scan_board(
    source: Dict[str, str], timeout: float, max_attempts: int, backoff_base: float
) -> Dict[str, Any]:
    fetched = fetch_board_jobs(source["board_token"], timeout, max_attempts, backoff_base)
    status = fetched["status"]
    jobs = fetched["jobs"]
    if status != "LIVE":
        result: Dict[str, Any] = {
            **source, "status": status, "total_jobs": 0,
            "geographic_match": False, "matched_job_count": 0,
            "matched_locations": [], "sample_matches": [],
            "request_attempts": fetched["request_attempts"],
        }
        if "error" in fetched and status in {"REQUEST_FAILED", "RATE_LIMITED"}:
            result["error"] = fetched["error"]
        return result

    matches: List[Dict[str, Any]] = []
    matched_locations: List[str] = []
    seen_locations = set()
    reasons = set()
    for job in jobs:
        location = ((job.get("location") or {}).get("name") or "").strip()
        matched, reason = match_canadian_location(location)
        if not matched:
            continue
        if location not in seen_locations:
            matched_locations.append(location)
            seen_locations.add(location)
        if reason:
            reasons.add(reason)
        matches.append({
            "job_id": job.get("id"),
            "title": job.get("title"),
            "location": location,
        })
    return {
        **source,
        "status": "LIVE",
        "total_jobs": len(jobs),
        "geographic_match": bool(matches),
        "matched_job_count": len(matches),
        "matched_locations": matched_locations,
        "sample_matches": matches[:3],
        "match_reasons": sorted(reasons),
        "request_attempts": fetched["request_attempts"],
    }


def atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    with temp.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    temp.replace(path)


def load_checkpoint(path: Path) -> Dict[str, Dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        with path.open(encoding="utf-8") as handle:
            decoded = json.load(handle)
        if decoded.get("filter_version") != FILTER_VERSION:
            logging.info("CHECKPOINT invalidated: filter version changed")
            return {}
        results = decoded.get("results", {})
        return results if isinstance(results, dict) else {}
    except (OSError, json.JSONDecodeError):
        logging.exception("Could not read checkpoint %s; starting without it", path)
        return {}


def save_checkpoint(path: Path, results: Dict[str, Dict[str, Any]]) -> None:
    atomic_write_json(path, {
        "updated_at": utc_now(), "source": SOURCE_PAGE,
        "filter_version": FILTER_VERSION, "results": results,
    })


def summarize(results: Iterable[Dict[str, Any]]) -> Dict[str, int]:
    rows = list(results)
    counts = {name: sum(row.get("status") == name for row in rows) for name in (
        "LIVE", "EMPTY", "INVALID", "REQUEST_FAILED", "RATE_LIMITED"
    )}
    counts["relevant"] = sum(bool(row.get("geographic_match")) for row in rows)
    counts["not_geographically_relevant"] = sum(
        row.get("status") == "LIVE" and not row.get("geographic_match") for row in rows
    )
    counts["api_request_attempts"] = sum(int(row.get("request_attempts", 0)) for row in rows)
    return counts


def configure_logging(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(path, encoding="utf-8"), logging.StreamHandler()],
    )


def select_boards(
    boards: Sequence[Dict[str, str]], tokens: Sequence[str], limit: Optional[int]
) -> List[Dict[str, str]]:
    if tokens:
        by_token = {row["board_token"]: row for row in boards}
        missing = [token for token in tokens if token not in by_token]
        if missing:
            raise RuntimeError(f"requested tokens not present in source: {missing}")
        selected = [by_token[token] for token in tokens]
    else:
        selected = list(boards)
    return selected[:limit] if limit is not None else selected


def run(args: argparse.Namespace) -> int:
    configure_logging(args.log)
    boards, _ = load_source_boards(args.timeout)
    selected = select_boards(boards, args.tokens, args.limit)
    logging.info("SOURCE boards=%d selected=%d csv=%s", len(boards), len(selected), SOURCE_URL)

    checkpoint = load_checkpoint(args.checkpoint)
    results: Dict[str, Dict[str, Any]] = {}
    pending: List[Dict[str, str]] = []
    for source in selected:
        cached = checkpoint.get(source["board_token"])
        if cached and cached.get("status") in SUCCESSFUL_CHECKPOINT_STATUSES:
            results[source["board_token"]] = cached
        else:
            pending.append(source)
    logging.info("RESUME reused=%d pending=%d", len(results), len(pending))

    lock = threading.Lock()
    completed_since_save = 0
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(scan_board, source, args.timeout, args.max_attempts, args.backoff_base): source
            for source in pending
        }
        for future in as_completed(futures):
            result = future.result()
            token = result["board_token"]
            with lock:
                results[token] = result
                checkpoint[token] = result
                completed_since_save += 1
                done = len(results)
                if completed_since_save >= args.checkpoint_every:
                    save_checkpoint(args.checkpoint, checkpoint)
                    completed_since_save = 0
                if done % args.progress_every == 0 or done == len(selected):
                    stats = summarize(results.values())
                    failed = stats["REQUEST_FAILED"] + stats["RATE_LIMITED"]
                    logging.info(
                        "PROGRESS boards=%d/%d live=%d relevant=%d failed=%d",
                        done, len(selected), stats["LIVE"], stats["relevant"], failed,
                    )
    save_checkpoint(args.checkpoint, checkpoint)

    ordered = [results[row["board_token"]] for row in selected]
    stats = summarize(ordered)
    generated_at = utc_now()
    relevant_rows = [row for row in ordered if row.get("geographic_match")]

    if not args.no_output:
        relevant_document = {
            "generated_at": generated_at,
            "source": SOURCE_PAGE,
            "source_data": SOURCE_URL,
            "total_source_boards": len(boards),
            "boards_scanned": len(ordered),
            "live_boards_checked": stats["LIVE"],
            "relevant_boards": len(relevant_rows),
            "api_request_attempts": stats["api_request_attempts"],
            "boards": [
                {
                    key: row[key]
                    for key in (
                        "board_token", "company", "status", "total_jobs",
                        "matched_job_count", "matched_locations", "match_reasons", "sample_matches"
                    )
                    if key in row
                }
                for row in relevant_rows
            ],
        }
        audit_document = {
            "generated_at": generated_at,
            "source": SOURCE_PAGE,
            "source_data": SOURCE_URL,
            "total_source_boards": len(boards),
            "boards_scanned": len(ordered),
            "summary": stats,
            "boards": ordered,
        }
        atomic_write_json(args.relevant_output, relevant_document)
        atomic_write_json(args.audit_output, audit_document)

    logging.info("DONE")
    logging.info("Source boards: %d", len(boards))
    logging.info("Live: %d", stats["LIVE"])
    logging.info("Empty: %d", stats["EMPTY"])
    logging.info("Invalid: %d", stats["INVALID"])
    logging.info("Failed: %d", stats["REQUEST_FAILED"])
    logging.info("Rate limited: %d", stats["RATE_LIMITED"])
    logging.info("Relevant: %d", stats["relevant"])
    logging.info("Not geographically relevant: %d", stats["not_geographically_relevant"])
    logging.info("API request attempts: %d", stats["api_request_attempts"])
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=20)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--backoff-base", type=float, default=1.0)
    parser.add_argument("--progress-every", type=int, default=100)
    parser.add_argument("--checkpoint-every", type=int, default=25)
    parser.add_argument("--checkpoint", type=Path, default=DEFAULT_CHECKPOINT)
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG)
    parser.add_argument("--relevant-output", type=Path, default=DEFAULT_RELEVANT_OUTPUT)
    parser.add_argument("--audit-output", type=Path, default=DEFAULT_AUDIT_OUTPUT)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--tokens", nargs="*", default=[])
    parser.add_argument("--no-output", action="store_true")
    return parser


if __name__ == "__main__":
    raise SystemExit(run(build_parser().parse_args()))
