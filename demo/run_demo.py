#!/usr/bin/env python3
"""Offline end-to-end demo: 8 fictional postings -> screening -> merge -> Resume V1 -> quality
review -> Resume V2 -> one-page PDF -> application queue. No network, no API key, no account.

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


def step(title: str, args: list[str], env: dict, cwd: Path = REPO) -> str:
    print(f"\n=== {title}\n$ {' '.join(Path(a).name if a.endswith('.py') else a for a in args)}", flush=True)
    p = subprocess.run([PY, *args], cwd=cwd, env=env, capture_output=True, text=True)
    tail = [l for l in p.stdout.splitlines() if l.startswith(("PROGRESS", "[DONE]", "[FAILED]", "[ADD]", "DONE exit",
                                                              "verification", "jobs:", "FAILED:", "Output", "Total"))]
    print("\n".join(tail[-12:]) or p.stdout[-800:])
    if p.returncode != 0:
        print(p.stdout[-2000:], p.stderr[-2000:], sep="\n")
        raise SystemExit(f"demo step failed: {title} (exit {p.returncode})")
    return p.stdout


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

    step("1. split discovery output into batches", ["screening/split_greenhouse.py"], env)
    step("2. screen every posting (single-turn worker, validated, checkpointed)",
         ["screening/scripts/02_run_screening.py", "--platform", "greenhouse", "--workers", "2"], env)
    step("3. merge + cross-source dedupe -> ACCEPT handoff", ["screening/scripts/03_merge_results.py"], env)
    step("4. prepare one folder per accepted job", ["resume/script/01_prepare_jobs.py"], env)
    step("5. Resume V1", ["resume/script/02_generate_v1.py"], env)
    step("6. quality review (mock provider, crash-safe state machine)",
         ["resume/script/03_review_resumes.py", "--poll-interval", "0"], env)
    has_chrome = True
    try:
        step("7. Resume V2 -> HTML -> PDF -> one-page check", ["resume/script/04_generate_final.py"], env)
    except SystemExit as e:
        if "Chrome" not in str(e) and "chrome" not in str(e).lower():
            raise
        has_chrome = False
        print("Chrome/Chromium not found: PDF rendering skipped (set CHROME_BIN to enable)")
    if has_chrome:
        step("8. queue finished resumes for applying", ["resume/script/05_enqueue_applications.py"], env)

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
