#!/usr/bin/env python3
"""Compare model backends / models on the same screening workload.

Every backend runs the production screening worker (screening_core.Worker, single-turn): the same
system prompt, hash checks, guardrails, retry policy and ScreeningResult validation. Only the call
underneath changes, so the differences in the report are the model's and the runtime's, not the
harness's.

    python3 eval/compare_models.py --backends cli,api:claude-opus-5-5,api:claude-sonnet-5-5 \\
        --jobs examples/job_postings/greenhouse_sample.json --out /private/dir

Backends: cli | cli:<model> | api | api:<model> (api needs `pip install "llm-job-pipeline[api]"` and
Anthropic credentials). Jobs file: a Greenhouse/Ashby source file, a list of normalized jobs, or
{"jobs": [{"job": <normalized job>, "historical": <ScreeningResult>}, ...]} (then the historical
decision is reported as the reference). Every run spends real money: --max-budget-usd caps each
backend.

Per backend: valid results, failures by class, decisions, agreement with the first backend (and
with the reference), cost (total and per job), input-side / output tokens, latency.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import threading
import time
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "screening" / "scripts"))
import screening_core as core  # noqa: E402


def load_jobs(path: Path) -> list[tuple[dict, str | None]]:
    """[(normalized job, reference decision or None)]"""
    doc = json.loads(path.read_text(encoding="utf-8"))
    items = doc.get("jobs", doc) if isinstance(doc, dict) else doc
    out = []
    for i, it in enumerate(items):
        if isinstance(it, dict) and "job" in it:
            out.append((it["job"], (it.get("historical") or {}).get("decision")))
        elif isinstance(it, dict) and "platform" in it and "jd" in it:
            out.append((it, it.get("reference_decision")))
        else:  # raw source record (Greenhouse / Ashby share the schema)
            job = core.PLATFORMS["greenhouse"]["normalize"](it)
            job["source_key"] = job["source_key"] or job["apply_url"] or f"job-{i}"
            out.append((job, None))
    return out


def run_backend(spec: str, jobs, budget: float, timeout: int) -> dict:
    core.stop_event.clear()
    calls: dict[str, list[dict]] = {}
    lock = threading.Lock()
    row = {"backend": spec, "decisions": {}, "failures": Counter(), "latency_s": [], "fatal": None}
    try:
        worker = core.Worker(core.WORKER_SINGLE_TURN, core.find_claude() if spec.split(":")[0] == "cli" else None,
                             spec.partition(":")[2] or None, budget, spec.split(":")[0])
    except core.PipelineError as e:
        row["fatal"] = str(e)
        return row
    with worker:
        orig = worker.post_check

        def post_check(envelope, prompt):
            u = orig(envelope, prompt)
            key = json.loads(prompt.partition("\n\n")[2])["source_key"]
            with lock:
                calls.setdefault(key, []).append(u)
            return u

        worker.post_check = post_check
        for job, _ in jobs:
            t = time.monotonic()
            try:
                res = core.screen_with_retry(job, worker, timeout)
                row["decisions"][job["source_key"]] = res["decision"]
            except core.GlobalFatalError as e:
                row["fatal"] = str(e)
                break
            except core.BudgetExceeded:
                row["failures"]["budget"] += 1
                break
            except core.PipelineError as e:
                row["failures"][type(e).__name__] += 1
            row["latency_s"].append(time.monotonic() - t)
        row["manifest"] = worker.manifest()
    usage = [u for us in calls.values() for u in us]
    costs = [u["cost_usd"] for u in usage if isinstance(u.get("cost_usd"), (int, float))]
    row.update(calls=len(usage), retries=sum(max(0, len(us) - 1) for us in calls.values()),
               cost_usd=round(sum(costs), 4), cost_per_job=round(sum(costs) / max(1, len(row["decisions"])), 4),
               input_side_tokens=sum(sum(u.get(k) or 0 for k in ("input_tokens", "cache_creation_input_tokens",
                                                                 "cache_read_input_tokens")) for u in usage),
               output_tokens=sum(u.get("output_tokens") or 0 for u in usage))
    return row


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--backends", required=True, help="comma-separated, e.g. cli,api:claude-sonnet-5-5")
    ap.add_argument("--jobs", required=True, type=Path)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--max-budget-usd", type=float, default=2.0, help="per backend")
    ap.add_argument("--timeout", type=int, default=core.DEFAULT_TIMEOUT)
    args = ap.parse_args()
    jobs = load_jobs(args.jobs)
    specs = [s.strip() for s in args.backends.split(",") if s.strip()]
    rows = [run_backend(s, jobs, args.max_budget_usd, args.timeout) for s in specs]

    base = rows[0]["decisions"]
    refs = {j["source_key"]: ref for j, ref in jobs if ref}
    print(f"{len(jobs)} jobs\n")
    print(f"{'backend':<28}{'valid':>7}{'fail':>6}{'agree*':>8}{'vs ref':>8}{'$/job':>9}{'in tok':>9}{'out tok':>9}{'p50 s':>7}")
    for r in rows:
        d = r["decisions"]
        agree = sum(1 for k, v in d.items() if base.get(k) == v)
        ref = f"{sum(1 for k, v in d.items() if refs.get(k) == v)}/{len(refs)}" if refs else "-"
        p50 = f"{statistics.median(r['latency_s']):.1f}" if r["latency_s"] else "-"
        print(f"{r['backend']:<28}{len(d):>7}{sum(r['failures'].values()):>6}{f'{agree}/{len(d)}':>8}{ref:>8}"
              f"{r.get('cost_per_job', 0):>9.4f}{r.get('input_side_tokens', 0):>9}{r.get('output_tokens', 0):>9}{p50:>7}"
              + (f"   FATAL: {r['fatal']}" if r["fatal"] else ""))
    print("\n* agreement with the first backend's decisions")
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / "compare_models.json").write_text(json.dumps(
            [{**r, "failures": dict(r["failures"])} for r in rows], indent=1, default=str))
    return 1 if any(r["fatal"] for r in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
