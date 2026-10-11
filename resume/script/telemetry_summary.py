#!/usr/bin/env python3
"""Summarize .runtime/telemetry/worker_usage.jsonl (read-only; never runs a stage).

Usage:
    python3 telemetry_summary.py                       # whole file, text
    python3 telemetry_summary.py --batch 2026-10-09_1200 [--json]
    python3 telemetry_summary.py --run-id <uuid> [--json]
    python3 telemetry_summary.py --path /abs/file.jsonl [--json]

Every metric is summed only over the events that actually reported it, with its own
coverage n/of. A missing value is never counted as 0: a sum with incomplete coverage is
marked PARTIAL (text) / "partial": true (JSON), and a sum with no data at all is null.
Events are de-duplicated by invocation_id (first occurrence wins).
01, 03 and 05 are pure Python and always reported with 0 model calls / 0 tokens. Stage 3
(Resume Quality Review) calls an external provider, not a model; its operational counters
come from .runtime/telemetry/review_events.jsonl (--review-path). Events with the stage names
03_prepare / 03_scan (an earlier model-driven stage 3) are still summarized, marked legacy.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pipeline_telemetry as telemetry  # noqa: E402

ORDER = ("01_prepare", "02_v1", "03_review", "04_v2", "04_trim", "05_enqueue")
NO_MODEL = ("01_prepare", "03_review", "05_enqueue")
LEGACY_03 = ("03_prepare", "03_scan")   # earlier model-driven stage 3
REVIEW_PATH = telemetry.DEFAULT_PATH.with_name("review_events.jsonl")
REVIEW_COUNTERS = ("submit_calls", "collect_calls", "lookup_calls", "duration_ms")
TOKENS = telemetry.USAGE_FIELDS
FIELDS = TOKENS + ("total_cost_usd", "duration_ms", "num_turns")


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def load(path: Path, batch: str | None, run_id: str | None) -> tuple[list[dict], int, int]:
    """-> (unique matching events, unreadable lines, duplicate events dropped)."""
    rows, bad, dup, seen = [], 0, 0, set()
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                bad += 1
                continue
            if not isinstance(r, dict):
                bad += 1
                continue
            if (batch and r.get("batch") != batch) or (run_id and r.get("run_id") != run_id):
                continue
            iid = r.get("invocation_id")
            if iid:
                if iid in seen:
                    dup += 1
                    continue
                seen.add(iid)
            rows.append(r)
    return rows, bad, dup


def field_stat(rows: list[dict], field: str) -> dict:
    vals = [v for v in (_num(r.get(field)) for r in rows) if v is not None]
    n, of = len(vals), len(rows)
    total = sum(vals) if vals else None
    if isinstance(total, float):
        total = round(total, 6)
    return {"sum": total, "n": n, "of": of, "partial": n < of}


def combine(stats: list[dict]) -> dict:
    """Sum of several field sums; partial if any part is partial or has no data."""
    parts = [s["sum"] for s in stats if s["sum"] is not None]
    total = sum(parts) if parts else None
    return {"sum": total, "partial": any(s["partial"] or s["sum"] is None for s in stats)}


def per_job(stat: dict, jobs: int) -> dict:
    avg = round(stat["sum"] / jobs, 3) if jobs and stat["sum"] is not None else None
    return {"avg": avg, "partial": stat["partial"] or stat["sum"] is None}


def job_key(r: dict) -> tuple:
    return (r.get("batch"), r.get("job_run_id") or r.get("job_folder"))  # job_folder: older events


def summarize(rows: list[dict]) -> dict:
    by_stage = defaultdict(list)
    for r in rows:
        by_stage[str(r.get("stage", "?"))].append(r)
    jobs = {job_key(r) for r in rows}

    stages = {}
    for st in list(ORDER) + sorted(set(by_stage) - set(ORDER)):
        if st == "03_review":
            stages[st] = {"model_calls": 0, "model_tokens": 0, "reason": "external_provider"}
            continue
        if st in NO_MODEL:
            stages[st] = {"model_calls": 0, "reason": "pure_python"}
            continue
        rs = by_stage.get(st, [])
        tools = Counter()
        for r in rs:
            for k, v in (r.get("tool_counts") or {}).items():
                if _num(v) is not None:
                    tools[k] += v
        stages[st] = {
            "model_calls": len(rs),
            "unique_jobs": len({job_key(r) for r in rs}),
            "retries": sum(1 for r in rs if (_num(r.get("attempt")) or 1) > 1),
            "cli_status": dict(sorted(Counter(str(r.get("cli_status")) for r in rs).items())),
            "validation_status": dict(sorted(Counter(str(r.get("validation_status")) for r in rs).items())),
            "fields": {f: field_stat(rs, f) for f in FIELDS},
            "tool_counts": dict(sorted(tools.items())),
        }
        if st in LEGACY_03:
            stages[st]["legacy"] = True

    coverage = {f: field_stat(rows, f) for f in FIELDS}
    n_jobs = len(jobs)
    totals = {
        "model_calls": len(rows),
        "retries": sum(1 for r in rows if (_num(r.get("attempt")) or 1) > 1),
        "fields": coverage,
        "uncached_input_plus_output": combine([coverage["input_tokens"], coverage["output_tokens"]]),
        "all_input_plus_output": combine([coverage[f] for f in TOKENS]),
        "total_duration_ms": combine([coverage["duration_ms"]]),
        "invocations_per_job": round(len(rows) / n_jobs, 3) if n_jobs else None,
        "per_job": {f: per_job(coverage[f], n_jobs) for f in FIELDS},
    }

    models = Counter((str(r.get("stage")), r.get("model_requested"), r.get("model_actual")) for r in rows)
    model_list = [{"stage": s, "requested": q, "actual": a, "attempts": n}
                  for (s, q, a), n in sorted(models.items(), key=lambda x: tuple(map(str, x[0])))]

    # accepted (VALID) trim passes per job, over jobs with a VALID V2 or any trim
    trims = Counter(job_key(r) for r in by_stage.get("04_trim", []) if r.get("validation_status") == "VALID")
    v2_jobs = {job_key(r) for r in by_stage.get("04_v2", []) if r.get("validation_status") == "VALID"}
    trim_jobs = v2_jobs | {job_key(r) for r in by_stage.get("04_trim", [])}
    dist = Counter(trims.get(j, 0) for j in trim_jobs)
    trim_distribution = {str(k): v for k, v in sorted(dist.items())}

    # legacy model-driven 03 only (the review stage makes no tool calls)
    tool_counts = {st: stages.get(st, {}).get("tool_counts", {}) for st in LEGACY_03}
    tool_counts["total"] = dict(sorted((Counter(tool_counts["03_prepare"]) + Counter(tool_counts["03_scan"])).items()))

    return {"unique_jobs": n_jobs, "stages": stages, "models": model_list, "totals": totals,
            "coverage": coverage, "trim_distribution": trim_distribution, "tool_counts": tool_counts}


def summarize_review(path: Path, batch: str | None) -> dict | None:
    """Stage 3 operational counters from the review events JSONL (None if there is no file)."""
    if not path.is_file():
        return None
    rows = []
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(r, dict) and (not batch or r.get("batch") == batch):
                rows.append(r)
    out = {"path": str(path), "job_runs": len(rows), "model_calls": 0, "model_tokens": 0,
           "outcomes": dict(sorted(Counter(str(r.get("outcome")) for r in rows).items())),
           "providers": dict(sorted(Counter(str(r.get("provider")) for r in rows).items())),
           "reconciled": sum(1 for r in rows if r.get("reconciled") is True)}
    for k in REVIEW_COUNTERS:
        out[k] = sum(v for v in (_num(r.get(k)) for r in rows) if v is not None)
    return out


def _fmt(stat: dict, key: str = "sum") -> str:
    v = stat[key]
    s = "null" if v is None else (f"{v:.4f}" if isinstance(v, float) else str(v))
    cov = f" (coverage {stat['n']}/{stat['of']})" if "n" in stat else ""
    return s + cov + (" PARTIAL" if stat["partial"] else "")


def print_text(rep: dict) -> None:
    print(f"File: {rep['path']} | filters {rep['filters']} | events {rep['matched_events']} | "
          f"duplicates dropped {rep['duplicate_events']} | unreadable lines {rep['unreadable_lines']} | "
          f"unique jobs {rep['unique_jobs']}")
    for st, s in rep["stages"].items():
        if s.get("reason") == "external_provider":
            print(f"\n[{st}] 0 model calls, 0 tokens (external review provider)")
            c = s.get("review")
            if c:
                print(f"  job runs {c['job_runs']} | submits {c['submit_calls']} | collects "
                      f"{c['collect_calls']} | lookups {c['lookup_calls']} | reconciled {c['reconciled']} | "
                      f"outcomes {c['outcomes']} | providers {c['providers']}")
            continue
        if s.get("reason") == "pure_python":
            print(f"\n[{st}] 0 model calls (pure Python)")
            continue
        tag = " (legacy AI 03)" if s.get("legacy") else ""
        print(f"\n[{st}]{tag} calls {s['model_calls']} | jobs {s['unique_jobs']} | retries {s['retries']} | "
              f"cli {s['cli_status']} | validation {s['validation_status']}")
        if s["model_calls"]:
            for f, stat in s["fields"].items():
                print(f"  {f}: {_fmt(stat)}")
            if s["tool_counts"]:
                print(f"  tool_counts: {s['tool_counts']}")
    t = rep["totals"]
    print(f"\nTOTALS | model calls {t['model_calls']} | retries {t['retries']} | "
          f"invocations/job {t['invocations_per_job']}")
    for f, stat in t["fields"].items():
        print(f"  {f}: {_fmt(stat)} | per job {_fmt(t['per_job'][f], 'avg')}")
    for k in ("uncached_input_plus_output", "all_input_plus_output", "total_duration_ms"):
        print(f"  {k}: {_fmt(t[k])}")
    print(f"\nTrim passes per job (accepted): {rep['trim_distribution'] or '-'}")
    print(f"Legacy AI 03 tool counts: {rep['tool_counts']}")
    print("Models (stage | requested -> actual: attempts):")
    for m in rep["models"]:
        print(f"  {m['stage']} | {m['requested']} -> {m['actual']}: {m['attempts']}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Summarize worker usage telemetry.")
    ap.add_argument("--path", default=str(telemetry.telemetry_path()))
    ap.add_argument("--batch")
    ap.add_argument("--run-id")
    ap.add_argument("--review-path", default=str(REVIEW_PATH),
                    help="stage 3 review events JSONL (operational counters; no model data)")
    ap.add_argument("--json", action="store_true", help="print one JSON object only")
    args = ap.parse_args(argv)
    path = Path(args.path).expanduser()
    filters = {"batch": args.batch, "run_id": args.run_id}
    if not path.is_file():
        if args.json:
            print(json.dumps({"path": str(path), "filters": filters, "error": "no telemetry file"},
                             sort_keys=True))
        else:
            print(f"no telemetry file: {path}")
        return 1
    rows, bad, dup = load(path, args.batch, args.run_id)
    rep = {"path": str(path), "filters": filters, "matched_events": len(rows),
           "duplicate_events": dup, "unreadable_lines": bad, **summarize(rows)}
    if not args.run_id:  # review events have no run_id; a run filter cannot apply to them
        events = summarize_review(Path(args.review_path).expanduser(), args.batch)
        if events is not None:
            rep["stages"]["03_review"]["review"] = events
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print_text(rep)
    return 0


if __name__ == "__main__":
    sys.exit(main())
