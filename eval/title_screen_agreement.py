#!/usr/bin/env python3
"""Agreement check: the title-screen worker vs. title decisions recorded by earlier runs.

Earlier Ashby runs recorded, per run, which job IDs the (agent-session) title screen accepted and
rejected (`*_ashby_jobs_audit.json`: accepted_internal_ids / rejected_internal_ids). This script
recovers each job's title from a board snapshot, re-screens the same titles with the worker
(discovery/title_screen.py, real model via the Claude Code CLI) and reports agreement. The title
screen is meant to be high-recall, so the number that matters most is how many historical accepts
the worker would have dropped.

    python3 eval/title_screen_agreement.py --audit 'DIR/*_ashby_jobs_audit.json' \\
        --boards DIR/01_board_fetch_checkpoint.json --out PRIVATE_DIR [--max-budget-usd 2]

The outputs contain real job titles: write them to a private directory, not into the repository.
"""
from __future__ import annotations

import argparse
import glob
import importlib.util
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from common import llm  # noqa: E402

spec = importlib.util.spec_from_file_location("title_screen", REPO / "discovery" / "title_screen.py")
ts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ts)


def history(audit_glob: str) -> dict[str, str]:
    """source_key -> 'accept' | 'reject' (a later run's decision wins; conflicts are counted)."""
    label, conflicts = {}, 0
    for f in sorted(glob.glob(audit_glob)):
        doc = json.loads(Path(f).read_text(encoding="utf-8"))
        if doc.get("test_run"):
            continue
        for key, lab in ([(k, "accept") for k in doc.get("accepted_internal_ids", [])] +
                         [(k, "reject") for k in doc.get("rejected_internal_ids", [])]):
            conflicts += key in label and label[key] != lab
            label[key] = lab
    if conflicts:
        print(f"note: {conflicts} IDs were decided differently by different runs; latest kept")
    return label


def titles(boards_path: Path) -> dict[str, str]:
    doc = json.loads(boards_path.read_text(encoding="utf-8"))
    out = {}
    for slug, board in (doc.get("results") or {}).items():
        for job in board.get("jobs") or []:
            if isinstance(job, dict) and job.get("id") and job.get("title"):
                out[f"ashby:{slug}:{job['id']}"] = job["title"]
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--audit", required=True, help="glob of *_ashby_jobs_audit.json files")
    ap.add_argument("--boards", required=True, type=Path, help="board snapshot with job titles")
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--chunk-size", type=int, default=150)
    ap.add_argument("--model")
    ap.add_argument("--max-budget-usd", type=float, default=2.0)
    args = ap.parse_args()

    label, title_of = history(args.audit), titles(args.boards)
    jobs = [{"source_key": k, "position": title_of[k]} for k in sorted(label) if k in title_of]
    print(f"historical decisions: {len(label)} | titles recovered: {len(jobs)} "
          f"({len(label) - len(jobs)} no longer on their board)")
    started = time.monotonic()
    with llm.ClaudeCliBackend(model=args.model, label="title-eval") as backend:
        result, summary = ts.screen(jobs, backend, "ashby", args.chunk_size, timeout=300,
                                    max_budget_usd=args.max_budget_usd)
    worker = {k: "accept" for k in result["accepted_ids"]} | {k: "reject" for k in result["rejected_ids"]}

    cm = {(h, w): 0 for h in ("accept", "reject") for w in ("accept", "reject")}
    for j in jobs:
        cm[(label[j["source_key"]], worker[j["source_key"]])] += 1
    n = len(jobs)
    agree = cm[("accept", "accept")] + cm[("reject", "reject")]
    hist_acc = cm[("accept", "accept")] + cm[("accept", "reject")]
    report = {
        "titles": n, "agreement": [agree, n],
        "historical_accepts_kept": [cm[("accept", "accept")], hist_acc],
        "historical_rejects_now_accepted": [cm[("reject", "accept")], n - hist_acc],
        "confusion": {f"history_{h}__worker_{w}": v for (h, w), v in cm.items()},
        "worker": summary, "wall_seconds": round(time.monotonic() - started, 1),
        "disagreements": [{"source_key": j["source_key"], "title": j["position"],
                           "history": label[j["source_key"]], "worker": worker[j["source_key"]]}
                          for j in jobs if label[j["source_key"]] != worker[j["source_key"]]],
    }
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "title_screen_agreement.json").write_text(json.dumps(report, indent=1, ensure_ascii=False))
    print(json.dumps({k: v for k, v in report.items() if k != "disagreements"}, indent=1))
    print(f"{len(report['disagreements'])} disagreements -> {args.out / 'title_screen_agreement.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
