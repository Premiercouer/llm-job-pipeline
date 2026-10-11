#!/usr/bin/env python3
"""Offline end-to-end demo: 8 fictional postings -> screening -> merge -> Resume V1 -> quality
review -> Resume V2 -> one-page PDF -> application queue, through the same `run_pipeline.py`
entry point used in production. No network, no API key, no account.

The model is replaced by tools/fake_claude.py (a deterministic, rule-based stand-in that obeys
the same single-turn contract and passes every validator and guardrail), the review provider by
scoring.mock, and the candidate by the fictional examples/candidate. Every pipeline stage is the
real code, run as a subprocess exactly as in production.

    python3 demo/run_demo.py                 # workspace: workspace/demo (git-ignored)
    python3 demo/run_demo.py --workspace /tmp/ljp-demo --keep
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PY = sys.executable


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--workspace", type=Path, default=REPO / "workspace" / "demo")
    ap.add_argument("--keep", action="store_true", help="keep an existing workspace instead of starting fresh")
    args = ap.parse_args()
    ws = args.workspace.resolve()
    if ws.exists() and not args.keep:
        shutil.rmtree(ws)
    today = datetime.date.today().isoformat()
    src = json.loads((REPO / "examples" / "job_postings" / "greenhouse_sample.json").read_text())
    src["date"] = today
    out_dir = ws / "discovery" / "greenhouse" / "outputs"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{today}_1200_greenhouse_jobs.json").write_text(json.dumps(src, indent=1))
    queue = ws / "resume" / "application_list.json"  # stages 01 / 05 refuse to guess this file
    queue.parent.mkdir(parents=True, exist_ok=True)
    if not queue.exists():
        queue.write_text(json.dumps({"jobs": []}, indent=1))

    env = {**os.environ, "LJP_WORKSPACE": str(ws), "LJP_CANDIDATE_DIR": str(REPO / "examples" / "candidate"),
           "CLAUDE_BIN": str(REPO / "tools" / "fake_claude.py"), "JOB_MATCH_WORKER": "single-turn",
           "LJP_RESUME_WORKER": "single-turn", "LJP_SCORER": "mock",
           "PIPELINE_TELEMETRY_PATH": str(ws / "resume" / ".runtime" / "telemetry" / "worker_usage.jsonl")}
    env.pop("LJP_CONFIG", None)
    os.chmod(REPO / "tools" / "fake_claude.py", 0o755)

    # One command, as in production: the unified runner from screening onwards (discovery needs the
    # public board APIs, so the demo starts from a discovery output file written above).
    print("$ run_pipeline.py --from screening --sources greenhouse --workers 2", flush=True)
    p = subprocess.run([PY, "run_pipeline.py", "--from", "screening", "--sources", "greenhouse", "--workers", "2"],
                       cwd=REPO, env=env, capture_output=True, text=True)
    keep = ("=== ", "PROGRESS", "DONE exit", "FAILED", "All ", "verification", "Total")
    print("\n".join(l for l in p.stdout.splitlines() if l.startswith(keep) and l != "FAILED: 0"))
    if p.returncode != 0:
        if "Chrome/Chromium not found" not in p.stdout + p.stderr:
            print(p.stdout[-2000:], p.stderr[-2000:], sep="\n")
            raise SystemExit(f"demo failed (exit {p.returncode})")
        print("Chrome/Chromium not found: PDF rendering and queueing skipped (set CHROME_BIN to enable)")

    screened = json.loads(next((ws / "screening" / ".runtime").glob("*/screened_all.json")).read_text())
    print(f"\n=== Summary\nscreened {screened['screened']} | ACCEPT {screened['accept']} | "
          f"REJECT {screened['reject']} | UNABLE {screened['unable_to_assess']}")
    for j in screened["jobs"]:
        hr = (j.get("hard_reject") or {}).get("type") or ""
        print(f"  {j['decision']:<8} {j['company']} — {j['position']} {('(' + hr + ')') if hr else ''}")
    for jf in sorted((ws / "resume" / "jobs").glob("*/*/job.json")):
        data = json.loads(jf.read_text())
        review = data.get("quality_review") or {}
        pdf = jf.with_name("resume.pdf")
        print(f"  resume: {data['company']} — {data['position']} | review score {review.get('score')} | "
              f"{'PDF ' + str(pdf.relative_to(ws)) if pdf.exists() else 'no PDF'}")
    print(f"\nworkspace: {ws}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
