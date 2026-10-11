#!/usr/bin/env python3
"""Run the whole pipeline, or a slice of it, with one command.

    discovery   per source: stage 01 prepare -> title-screen worker -> stage 01 finalize
                (Greenhouse also runs 02_fetch_jd)
    screening   split_<source> -> 02_run_screening -> 03_merge_results
    resume      01_prepare_jobs -> 02_generate_v1 -> 03_review_resumes -> 04_generate_final
                -> 05_enqueue_applications

Every step is the stage's own script, run as a subprocess in order; the first failing step stops the
run and its exit code is returned. Stages keep their own checkpoints, so rerunning after a failure
skips finished work.

    python3 run_pipeline.py                                  # everything, both sources
    python3 run_pipeline.py --from screening                 # discovery outputs already exist
    python3 run_pipeline.py --sources ashby --until screening
    python3 run_pipeline.py --limit-boards 5 --sources ashby # discovery trial run on test paths only
    python3 run_pipeline.py --dry-run                        # print the plan
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))
from common import paths  # noqa: E402

STAGES = ("discovery", "screening", "resume")
SOURCES = ("greenhouse", "ashby")
PY = sys.executable


class StepFailed(Exception):
    def __init__(self, step: str, code: int):
        super().__init__(step)
        self.step, self.code = step, code


class Runner:
    def __init__(self, dry_run: bool):
        self.dry_run, self.done = dry_run, []

    def run(self, title: str, script: str, *args, capture: bool = False) -> str:
        argv = [PY, str(REPO / script), *map(str, args)]
        print(f"\n=== {title}\n$ {script} {' '.join(map(str, args))}".rstrip(), flush=True)
        if self.dry_run:
            return ""
        t = time.monotonic()
        if capture:
            p = subprocess.run(argv, cwd=REPO, capture_output=True, text=True)
            sys.stdout.write(p.stdout)
            sys.stderr.write(p.stderr)
            out = p.stdout
        else:
            p = subprocess.run(argv, cwd=REPO)
            out = ""
        self.done.append((title, p.returncode, time.monotonic() - t))
        if p.returncode != 0:
            raise StepFailed(title, p.returncode)
        return out


def last_json_line(text: str) -> dict:
    for line in reversed(text.splitlines()):
        if line.startswith("{"):
            return json.loads(line)
    return {}


def discovery(r: Runner, source: str, a: argparse.Namespace) -> None:
    trial = ["--limit-boards", a.limit_boards] if a.limit_boards else []
    stage01 = f"discovery/{source}/scripts/01_search_jobs.py"
    r.run(f"discovery/{source}: scan boards (prepare)", stage01, *trial)
    screen = ["--source", source, "--backend", a.backend] + (["--test"] if a.limit_boards else []) + \
             (["--model", a.model] if a.model else [])
    summary = last_json_line(r.run(f"discovery/{source}: title screen (worker)", "discovery/title_screen.py",
                                   *screen, capture=True))
    usage = (["--ai-invocations", summary.get("calls", 0)] +
             (["--ai-cost-usd", summary["usage"]["cost_usd"]] if summary.get("usage", {}).get("cost_usd") is not None else [])
             if source == "ashby" else [])
    r.run(f"discovery/{source}: validate and commit (finalize)", stage01, "--phase", "finalize", *trial, *usage)
    if source == "greenhouse":
        fetch = []
        if a.limit_boards and not r.dry_run:  # trial run: stage 02 reads the test candidates file
            handoff = paths.data_dir("discovery", "greenhouse", ".runtime") / "01_search_handoff_test.json"
            fetch = ["--input", json.loads(handoff.read_text())["output_file"]]
        r.run("discovery/greenhouse: fetch full JDs", "discovery/greenhouse/scripts/02_fetch_jd.py", *fetch)


def screening(r: Runner, a: argparse.Namespace) -> None:
    for source in a.sources:
        r.run(f"screening: split {source} into batches", f"screening/split_{source}.py")
    opts = (["--platform", a.sources[0]] if len(a.sources) == 1 else []) + ["--workers", a.workers] + \
           ["--backend", a.backend] + \
           (["--model", a.model] if a.model else []) + \
           (["--max-budget-usd", a.max_budget_usd] if a.max_budget_usd is not None else [])
    r.run("screening: screen every posting", "screening/scripts/02_run_screening.py", *opts)
    r.run("screening: merge + cross-source dedupe", "screening/scripts/03_merge_results.py")


def resume(r: Runner, a: argparse.Namespace) -> None:
    model = ["--model", a.model] if a.model else []
    r.run("resume: prepare job folders", "resume/script/01_prepare_jobs.py")
    r.run("resume: Resume V1", "resume/script/02_generate_v1.py", *model)
    r.run("resume: quality review", "resume/script/03_review_resumes.py",
          *(["--provider", a.provider] if a.provider else []))
    r.run("resume: Resume V2 -> PDF -> one-page fit", "resume/script/04_generate_final.py", *model)
    r.run("resume: queue for human review", "resume/script/05_enqueue_applications.py")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("--from", dest="start", choices=STAGES, default="discovery")
    ap.add_argument("--until", choices=STAGES, default="resume")
    ap.add_argument("--sources", default=",".join(SOURCES), help="comma-separated: greenhouse,ashby")
    ap.add_argument("--limit-boards", type=int, help="discovery trial run on N boards (test paths; stops after discovery)")
    ap.add_argument("--model", help="model for every worker (default: the CLI default)")
    ap.add_argument("--backend", default="cli", help="discovery + screening model backend: cli (default), api, api:<model>")
    ap.add_argument("--workers", type=int, default=3, help="parallel screening workers")
    ap.add_argument("--max-budget-usd", type=float, help="screening spend cap")
    ap.add_argument("--provider", help="quality-review provider (default: $LJP_SCORER or mock)")
    ap.add_argument("--dry-run", action="store_true", help="print the steps without running them")
    a = ap.parse_args()
    a.sources = [s.strip() for s in a.sources.split(",") if s.strip()]
    if not a.sources or any(s not in SOURCES for s in a.sources):
        ap.error(f"--sources must be a subset of {','.join(SOURCES)}")
    if a.limit_boards is not None:
        if a.start != "discovery":
            ap.error("--limit-boards is a discovery trial run")
        a.until = "discovery"
    stages = STAGES[STAGES.index(a.start):STAGES.index(a.until) + 1]
    if not stages:
        ap.error("--from must not come after --until")

    r = Runner(a.dry_run)
    started = time.monotonic()
    try:
        if "discovery" in stages:
            for source in a.sources:
                discovery(r, source, a)
        if "screening" in stages:
            screening(r, a)
        if "resume" in stages:
            resume(r, a)
    except StepFailed as e:
        print(f"\nFAILED at step: {e.step} (exit {e.code}); finished steps keep their checkpoints, "
              f"rerun to continue", flush=True)
        print(f"DONE exit={e.code}")
        return e.code
    if not a.dry_run:
        print(f"\nAll {len(r.done)} steps succeeded in {time.monotonic() - started:.0f}s "
              f"({', '.join(stages)}; sources: {', '.join(a.sources)})")
    print("DONE exit=0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
