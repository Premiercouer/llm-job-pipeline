#!/usr/bin/env python3
"""Fetch detail/JD for title-approved jobs and write the final backend output."""

from __future__ import annotations

import argparse
import sys
import html
import importlib.util
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, time, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import quote, urlparse
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

DEFAULT_LOG = DATA_ROOT / "logs/02_fetch_jd.log"
MAX_TEST_JOBS = 20
TORONTO_TZ = ZoneInfo("America/Toronto")
DETAIL_TEMPLATE = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{job_id}"
STAGE01_PRODUCTION_HANDOFF = DATA_ROOT / ".runtime/01_search_handoff.json"
PRODUCTION_HANDOFF = DATA_ROOT / ".runtime/02_fetch_handoff.json"
TEST_HANDOFF = DATA_ROOT / ".runtime/02_fetch_handoff_test.json"
REQUIRED_CANDIDATE_FIELDS = (
    "source_key", "board_token", "job_id", "position", "location", "updated_at", "absolute_url",
)


class _PlainTextHTMLParser(HTMLParser):
    BLOCK_TAGS = {
        "p", "div", "section", "article", "header", "footer", "main",
        "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "table",
        "tr", "blockquote", "pre",
    }
    SKIP_TAGS = {"script", "style", "noscript"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []
        self.skip_depth = 0

    def _newline(self) -> None:
        self.parts.append("\n")

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        tag = tag.lower()
        if tag in self.SKIP_TAGS:
            self.skip_depth += 1
            return
        if self.skip_depth:
            return
        if tag in self.BLOCK_TAGS or tag == "br":
            self._newline()
        if tag == "li":
            self._newline()
            self.parts.append("- ")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self.SKIP_TAGS and self.skip_depth:
            self.skip_depth -= 1
            return
        if not self.skip_depth and (tag in self.BLOCK_TAGS or tag == "li"):
            self._newline()

    def handle_data(self, data: str) -> None:
        if not self.skip_depth:
            self.parts.append(data)


def clean_job_description(content: Any) -> str:
    if not isinstance(content, str) or not content.strip():
        return ""
    decoded = content
    for _ in range(3):
        next_value = html.unescape(decoded)
        if next_value == decoded:
            break
        decoded = next_value
    parser = _PlainTextHTMLParser()
    parser.feed(decoded)
    parser.close()
    lines: List[str] = []
    blank = False
    for raw_line in "".join(parser.parts).replace("\xa0", " ").splitlines():
        line = re.sub(r"[ \t\f\v]+", " ", raw_line).strip()
        if line:
            lines.append(line)
            blank = False
        elif lines and not blank:
            lines.append("")
            blank = True
    return "\n".join(lines).strip()


def parse_aware_timestamp(value: Any) -> Tuple[Optional[datetime], str]:
    if value is None or not isinstance(value, str) or not value.strip():
        return None, "missing"
    raw = value.strip()
    try:
        parsed = datetime.fromisoformat(raw[:-1] + "+00:00" if raw.endswith("Z") else raw)
        if parsed.tzinfo is None:
            raise ValueError("timezone missing")
        return parsed.astimezone(timezone.utc), "valid"
    except (TypeError, ValueError, OverflowError):
        return None, "invalid"


def classify_first_published(value: Any, now: Optional[datetime] = None) -> Dict[str, Any]:
    now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    parsed, status = parse_aware_timestamp(value)
    if parsed is None:
        return {
            "status": status, "published_age_days": None,
            "published_within_7d": False, "parsed": None,
        }
    age_days = (now_utc - parsed).total_seconds() / 86400
    return {
        "status": "valid", "published_age_days": round(age_days, 3),
        "published_within_7d": parsed >= now_utc - timedelta(days=7),
        "parsed": parsed,
    }


def classify_updated_at(value: Any, now: datetime) -> Dict[str, Any]:
    parsed, status = parse_aware_timestamp(value)
    if parsed is None:
        return {"status": status, "updated_within_7d": False, "updated_age_days": None}
    age_days = (now.astimezone(timezone.utc) - parsed).total_seconds() / 86400
    return {
        "status": "valid", "updated_within_7d": parsed >= now.astimezone(timezone.utc) - timedelta(days=7),
        "updated_age_days": round(age_days, 3),
    }


def classify_deadline(value: Any, now: Optional[datetime] = None) -> Dict[str, Any]:
    now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if value is None or not isinstance(value, str) or not value.strip():
        return {"status": "missing", "application_deadline_passed": None}
    raw = value.strip()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            local_end = datetime.combine(datetime.fromisoformat(raw).date(), time.max, TORONTO_TZ)
            parsed = local_end.astimezone(timezone.utc)
        else:
            parsed = datetime.fromisoformat(raw[:-1] + "+00:00" if raw.endswith("Z") else raw)
            if parsed.tzinfo is None:
                raise ValueError("timezone missing")
            parsed = parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return {"status": "invalid", "application_deadline_passed": None}
    return {"status": "valid", "application_deadline_passed": parsed < now_utc}


def validate_detail(detail: Any, candidate: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(detail, dict):
        return {"status": "INVALID_DETAIL", "error": "detail response is not an object"}
    if detail.get("id") is None or str(detail.get("id")) != str(candidate.get("job_id")):
        return {"status": "DETAIL_ID_MISMATCH", "error": "detail.id does not match candidate.job_id"}
    title = detail.get("title")
    company = detail.get("company_name")
    location_obj = detail.get("location")
    location = location_obj.get("name") if isinstance(location_obj, dict) else None
    apply_url = detail.get("absolute_url")
    if not isinstance(title, str) or not title.strip():
        return {"status": "INVALID_DETAIL", "error": "missing detail.title"}
    if not isinstance(company, str) or not company.strip():
        return {
            "status": "INVALID_DETAIL", "error": "missing detail.company_name",
            "company": None, "company_status": "missing",
        }
    if not isinstance(location, str) or not location.strip():
        return {"status": "INVALID_DETAIL", "error": "missing detail.location.name"}
    if not isinstance(apply_url, str) or not apply_url.strip() or urlparse(apply_url).scheme not in {"http", "https"}:
        return {"status": "INVALID_DETAIL", "error": "missing or invalid detail.absolute_url"}
    jd = clean_job_description(detail.get("content"))
    if not jd:
        return {"status": "EMPTY_JD", "error": "missing or empty detail.content"}
    published = classify_first_published(detail.get("first_published"))
    if published["status"] != "valid":
        return {"status": "PUBLISHED_DATE_UNKNOWN", "error": f"first_published {published['status']}"}
    return {
        "status": "VALID", "title": title.strip(), "company": company.strip(),
        "company_status": "present", "location": location.strip(),
        "apply_url": apply_url.strip(), "jd": jd,
    }


def load_candidate_input(path: Path) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    with path.open(encoding="utf-8") as handle:
        document = json.load(handle)
    if not isinstance(document, dict) or not isinstance(document.get("test_run"), bool):
        raise RuntimeError("candidate input must contain boolean test_run")
    jobs = document.get("jobs")
    if not isinstance(jobs, list):
        raise RuntimeError("input jobs must be a list")
    if document.get("candidate_count") != len(jobs):
        raise RuntimeError("candidate_count does not equal len(jobs)")
    if document["test_run"] and len(jobs) > MAX_TEST_JOBS:
        raise RuntimeError(f"refusing to process more than {MAX_TEST_JOBS} test jobs")
    return document, jobs


def resolve_input_path(explicit_input: Optional[Path], handoff_path: Path) -> Path:
    if explicit_input is not None:
        return explicit_input.resolve()
    with handoff_path.open(encoding="utf-8") as handle:
        handoff = json.load(handle)
    if (
        not isinstance(handoff, dict)
        or handoff.get("status") != "READY"
        or handoff.get("stage") != "greenhouse_discovery_title_screen"
    ):
        raise RuntimeError(f"Stage 01 handoff is not READY: {handoff_path}")
    if handoff.get("test_run") is not False:
        raise RuntimeError("default Stage 02 run requires a production Stage 01 handoff")
    output_file = handoff.get("output_file")
    if not isinstance(output_file, str) or not output_file:
        raise RuntimeError("Stage 01 handoff has no output_file")
    path = Path(output_file)
    if not path.is_absolute():
        raise RuntimeError("Stage 01 handoff output_file must be absolute")
    return path


def process_candidate(
    candidate: Dict[str, Any], now: datetime, timeout: float,
    max_attempts: int, backoff_base: float,
) -> Dict[str, Any]:
    missing = [field for field in REQUIRED_CANDIDATE_FIELDS if field not in candidate]
    if missing:
        return {
            "kind": "failure", "internal_source_key": candidate.get("source_key"),
            "board_token": candidate.get("board_token"), "job_id": candidate.get("job_id"),
            "status": "INPUT_VALIDATION_FAILED", "error": f"missing fields: {missing}",
            "request_attempts": 0,
        }
    token = candidate.get("board_token")
    job_id = candidate.get("job_id")
    if not isinstance(token, str) or not token.strip() or job_id is None:
        return {
            "kind": "failure", "internal_source_key": candidate.get("source_key"),
            "board_token": token, "job_id": job_id,
            "status": "INPUT_VALIDATION_FAILED", "error": "board_token/job_id invalid",
            "request_attempts": 0,
        }
    expected_source_key = f"greenhouse:{token}:{job_id}"
    if candidate.get("source_key") != expected_source_key:
        return {
            "kind": "failure", "internal_source_key": candidate.get("source_key"),
            "board_token": token, "job_id": job_id,
            "status": "INPUT_VALIDATION_FAILED", "error": "source_key is not canonical",
            "request_attempts": 0,
        }

    url = DETAIL_TEMPLATE.format(token=quote(token, safe=""), job_id=quote(str(job_id), safe=""))
    fetched = CORE.fetch_json_endpoint(url, timeout, max_attempts, backoff_base)
    base = {
        "internal_source_key": expected_source_key, "board_token": token, "job_id": job_id,
        "request_attempts": fetched["request_attempts"],
    }
    if fetched["status"] != "SUCCESS":
        return {
            **base, "kind": "failure", "status": fetched["status"],
            "error": fetched.get("error", fetched["status"]),
        }

    detail = fetched["data"]
    validation = validate_detail(detail, candidate)
    if not isinstance(detail, dict):
        return {**base, "kind": "failure", **validation}
    if validation["status"] in {"DETAIL_ID_MISMATCH", "INVALID_DETAIL", "EMPTY_JD"}:
        return {
            **base, "kind": "failure", **validation,
            "company_status": validation.get("company_status"),
        }

    first_published = detail.get("first_published")
    published = classify_first_published(first_published, now=now)
    detail_updated_at = detail.get("updated_at")
    detail_updated = classify_updated_at(detail_updated_at, now)
    deadline_value = detail.get("application_deadline")
    deadline = classify_deadline(deadline_value, now=now)
    comparison = {
        "board_token": token,
        "job_id": job_id,
        "position": detail.get("title") or candidate.get("position"),
        "updated_at": candidate.get("updated_at"),
        "updated_within_7d": bool(candidate.get("updated_within_7d")),
        "first_published": first_published,
        "published_within_7d": published["published_within_7d"],
        "same_recent_classification": (
            bool(candidate.get("updated_within_7d")) == published["published_within_7d"]
            if published["status"] == "valid" else None
        ),
    }
    audit = {
        **base,
        "position": detail.get("title"),
        "company": detail.get("company_name"),
        "company_status": "present" if detail.get("company_name") else "missing",
        "location": ((detail.get("location") or {}).get("name") if isinstance(detail.get("location"), dict) else None),
        "stage01_updated_at": candidate.get("updated_at"),
        "stage01_updated_within_7d": candidate.get("updated_within_7d"),
        "updated_at": detail_updated_at,
        "updated_within_7d": detail_updated["updated_within_7d"],
        "first_published": first_published,
        "published_age_days": published["published_age_days"],
        "published_within_7d": published["published_within_7d"],
        "application_deadline": deadline_value,
        "application_deadline_passed": deadline["application_deadline_passed"],
        "application_deadline_status": deadline["status"],
        "jd_characters": len(validation.get("jd", "")),
        "comparison": comparison,
    }
    if validation["status"] == "PUBLISHED_DATE_UNKNOWN" or published["status"] != "valid":
        return {
            **audit, "kind": "rejected", "status": "PUBLISHED_DATE_UNKNOWN",
            "reason": "PUBLISHED_DATE_UNKNOWN",
        }
    if not published["published_within_7d"]:
        return {
            **audit, "kind": "rejected", "status": "REJECT_OLD",
            "reason": "PUBLISHED_OLDER_THAN_7D",
        }

    job = {
        "internal_source_key": expected_source_key,
        "platform": "greenhouse",
        "board_token": token,
        "job_id": detail.get("id"),
        "internal_job_id": detail.get("internal_job_id"),
        "position": validation["title"],
        "company": validation["company"],
        "company_status": validation["company_status"],
        "location": validation["location"],
        "date": published["parsed"].astimezone(TORONTO_TZ).date().isoformat(),
        "first_published": first_published,
        "published_age_days": published["published_age_days"],
        "published_within_7d": True,
        "updated_at": detail_updated_at,
        "updated_within_7d": detail_updated["updated_within_7d"],
        "application_deadline": deadline_value,
        "application_deadline_passed": deadline["application_deadline_passed"],
        "apply_url": validation["apply_url"],
        "jd": validation["jd"],
    }
    return {**audit, "kind": "job", "status": "SUCCESS", "job": job}


def choose_output_paths(output_dir: Path, now: datetime, test_run: bool) -> Tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    base = now.astimezone(TORONTO_TZ).strftime("%Y-%m-%d_%H%M_greenhouse_jobs")
    suffix_label = "_test" if test_run else ""
    backend = output_dir / f"{base}{suffix_label}.json"
    audit = output_dir / f"{base}_audit{suffix_label}.json"
    suffix = 2
    while backend.exists() or audit.exists():
        backend = output_dir / f"{base}{suffix_label}_{suffix}.json"
        audit = output_dir / f"{base}_audit{suffix_label}_{suffix}.json"
        suffix += 1
    return backend, audit


BACKEND_JOB_KEYS = {"company", "position", "jd", "apply_url", "source_key"}
BACKEND_TOP_LEVEL_KEYS = {"date", "job_count", "jobs"}


def serialize_backend_job(rich_job: Dict[str, Any]) -> Dict[str, str]:
    apply_url = rich_job.get("apply_url")
    result = {
        "company": rich_job.get("company"),
        "position": rich_job.get("position"),
        "jd": rich_job.get("jd"),
        "apply_url": apply_url,
        "source_key": apply_url,
    }
    validate_backend_job(result)
    return result


def validate_backend_job(job: Dict[str, Any]) -> None:
    if not isinstance(job, dict) or set(job) != BACKEND_JOB_KEYS:
        raise RuntimeError("backend job does not have the exact five-field schema")
    for field in ("company", "position", "jd"):
        if not isinstance(job[field], str) or not job[field].strip():
            raise RuntimeError(f"backend job {field} must be a non-empty string")
    apply_url = job["apply_url"]
    if not isinstance(apply_url, str) or urlparse(apply_url).scheme not in {"http", "https"}:
        raise RuntimeError("backend job apply_url must be an http/https URL")
    if job["source_key"] != apply_url:
        raise RuntimeError("backend source_key must equal apply_url")


def validate_backend_document(document: Dict[str, Any]) -> None:
    if not isinstance(document, dict) or set(document) != BACKEND_TOP_LEVEL_KEYS:
        raise RuntimeError("backend document does not have the exact top-level schema")
    if not isinstance(document["date"], str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", document["date"]):
        raise RuntimeError("backend date must be YYYY-MM-DD")
    jobs = document["jobs"]
    if not isinstance(jobs, list) or document["job_count"] != len(jobs):
        raise RuntimeError("backend job_count does not equal len(jobs)")
    source_keys = set()
    for job in jobs:
        validate_backend_job(job)
        if job["source_key"] in source_keys:
            raise RuntimeError("duplicate backend source_key")
        source_keys.add(job["source_key"])


def build_backend_document(
    rich_jobs: Sequence[Dict[str, Any]], now: datetime
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    backend_jobs: List[Dict[str, str]] = []
    duplicates: List[Dict[str, Any]] = []
    seen = set()
    for rich_job in rich_jobs:
        backend_job = serialize_backend_job(rich_job)
        source_key = backend_job["source_key"]
        if source_key in seen:
            duplicates.append({
                "status": "DUPLICATE_BACKEND_SOURCE_KEY",
                "source_key": source_key,
                "internal_source_key": rich_job.get("internal_source_key"),
                "board_token": rich_job.get("board_token"),
                "job_id": rich_job.get("job_id"),
            })
            continue
        seen.add(source_key)
        backend_jobs.append(backend_job)
    document = {
        "date": now.astimezone(TORONTO_TZ).date().isoformat(),
        "job_count": len(backend_jobs),
        "jobs": backend_jobs,
    }
    validate_backend_document(document)
    return document, duplicates


def configure_logging(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(path, encoding="utf-8"), logging.StreamHandler()],
    )


def run(args: argparse.Namespace) -> int:
    configure_logging(args.log)
    if args.workers < 1:
        raise RuntimeError("--workers must be >= 1")
    input_path = resolve_input_path(args.input, args.stage01_handoff)
    input_document, candidates = load_candidate_input(input_path)
    test_run = input_document["test_run"]
    now = datetime.now(timezone.utc)
    results: List[Dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=min(args.workers, max(1, len(candidates)))) as executor:
        futures = {
            executor.submit(
                process_candidate, candidate, now, args.timeout,
                args.max_attempts, args.backoff_base,
            ): candidate
            for candidate in candidates
        }
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            logging.info(
                "DETAIL board=%s job=%s status=%s published_within_7d=%s jd_chars=%s",
                result.get("board_token"), result.get("job_id"), result.get("status"),
                result.get("published_within_7d"), result.get("jd_characters", 0),
            )

    by_key = {row.get("internal_source_key"): row for row in results}
    ordered = [by_key.get(candidate.get("source_key")) for candidate in candidates]
    ordered = [row for row in ordered if row is not None]
    accepted_rich = [row["job"] for row in ordered if row["kind"] == "job"]
    rejected = [
        {key: value for key, value in row.items() if key not in {"kind", "job", "comparison"}}
        for row in ordered if row["kind"] == "rejected"
    ]
    failures = [
        {key: value for key, value in row.items() if key not in {"kind", "job", "comparison"}}
        for row in ordered if row["kind"] == "failure"
    ]
    successful_detail = [row for row in ordered if row["kind"] in {"job", "rejected"}]
    comparisons = [row["comparison"] for row in successful_detail if "comparison" in row]
    published_within = sum(row.get("published_within_7d") is True for row in successful_detail)
    published_old = sum(row.get("status") == "REJECT_OLD" for row in successful_detail)
    published_unknown = sum(row.get("status") == "PUBLISHED_DATE_UNKNOWN" for row in successful_detail)
    deadline_passed = sum(row.get("application_deadline_passed") is True for row in successful_detail)
    deadline_active = sum(row.get("application_deadline_passed") is False for row in successful_detail)
    deadline_missing = sum(row.get("application_deadline_passed") is None for row in successful_detail)
    empty_jd = sum(row.get("status") == "EMPTY_JD" for row in ordered)
    detail_requests = sum(int(row.get("request_attempts", 0)) > 0 for row in ordered)
    total_attempts = sum(int(row.get("request_attempts", 0)) for row in ordered)
    backend, backend_duplicates = build_backend_document(accepted_rich, now)
    audit = {
        "generated_at": now.isoformat(),
        "source": "greenhouse",
        "test_run": test_run,
        "input_file": str(input_path),
        "input_candidates": len(candidates),
        "detail_requests": detail_requests,
        "detail_attempts": total_attempts,
        "detail_api_attempts": total_attempts,
        "retries": total_attempts - detail_requests,
        "detail_success": len(successful_detail),
        "detail_failures": len(failures),
        "published_within_7d": published_within,
        "published_older_than_7d": published_old,
        "published_unknown": published_unknown,
        "deadline_passed": deadline_passed,
        "deadline_active": deadline_active,
        "deadline_missing": deadline_missing,
        "empty_jd": empty_jd,
        "accepted_count_before_backend_dedup": len(accepted_rich),
        "final_job_count": backend["job_count"],
        "accepted": accepted_rich,
        "rejected": rejected,
        "failures": failures,
        "updated_vs_first_published": comparisons,
        "backend_duplicates": backend_duplicates,
    }
    backend_path, audit_path = choose_output_paths(args.output_dir, now, test_run)
    CORE.atomic_write_json(audit_path, audit)
    CORE.atomic_write_json(backend_path, backend)
    with backend_path.open(encoding="utf-8") as handle:
        written_backend = json.load(handle)
    validate_backend_document(written_backend)
    if written_backend["job_count"] != len(written_backend["jobs"]):
        raise RuntimeError("written backend job_count validation failed")
    handoff_path = args.test_handoff if test_run else args.production_handoff
    CORE.atomic_write_json(handoff_path, {
        "generated_at": CORE.utc_now(),
        "stage": "greenhouse_fetch_jd",
        "status": "READY",
        "test_run": test_run,
        "output_file": str(backend_path.resolve()),
        "audit_file": str(audit_path.resolve()),
        "job_count": backend["job_count"],
    })
    logging.info("DONE")
    for label, value in (
        ("Input candidates", len(candidates)), ("Detail requests", detail_requests),
        ("Detail success", len(successful_detail)), ("Detail failures", len(failures)),
        ("Published within 7d", published_within), ("Published older than 7d", published_old),
        ("Published unknown", published_unknown), ("Deadline passed", deadline_passed),
        ("Deadline active", deadline_active), ("Deadline missing", deadline_missing),
        ("Empty JD", empty_jd), ("Final jobs", backend["job_count"]),
        ("Rejected", len(rejected)), ("Failures", len(failures)),
    ):
        logging.info("%s: %d", label, value)
    print(json.dumps({
        "output": str(backend_path.resolve()), "audit": str(audit_path.resolve()),
        "handoff": str(handoff_path.resolve()), "summary": {
        key: audit[key] for key in (
            "input_candidates", "detail_requests", "detail_api_attempts", "retries",
            "detail_success", "detail_failures", "published_within_7d",
            "published_older_than_7d", "published_unknown", "final_job_count",
        )
    }}, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--stage01-handoff", type=Path, default=STAGE01_PRODUCTION_HANDOFF)
    parser.add_argument("--output-dir", type=Path, default=DATA_ROOT / "outputs")
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--backoff-base", type=float, default=1.0)
    parser.add_argument("--production-handoff", type=Path, default=PRODUCTION_HANDOFF)
    parser.add_argument("--test-handoff", type=Path, default=TEST_HANDOFF)
    return parser


if __name__ == "__main__":
    raise SystemExit(run(build_parser().parse_args()))
