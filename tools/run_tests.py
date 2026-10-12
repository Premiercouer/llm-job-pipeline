#!/usr/bin/env python3
"""Run every offline test suite, each in its own process (suites isolate their workspace via
environment variables set before import, so they must not share an interpreter).

    python3 tools/run_tests.py            # all suites
    python3 tools/run_tests.py scoring    # suites whose name contains "scoring"
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SUITES = [  # (name, start dir, top-level dir for imports, cwd)
    ("common", "common/tests", None, REPO),
    ("discovery", "discovery/tests", None, REPO),
    ("discovery/greenhouse", "discovery/greenhouse/tests", None, REPO),
    ("discovery/ashby", "discovery/ashby/tests", None, REPO),
    ("screening", "screening/tests", ".", REPO),
    ("scoring", "scoring/tests", ".", REPO),
    ("resume", "test", None, REPO / "resume"),
    # evaluated alternative, not part of the pipeline; numpy-dependent tests skip without the `rag` extra
    ("experiments/candidate_fact_rag", "tests", ".", REPO / "experiments" / "candidate_fact_rag"),
]


def main() -> int:
    wanted = sys.argv[1:]
    failed = []
    for name, start, top, cwd in SUITES:
        if wanted and not any(w in name for w in wanted):
            continue
        cmd = [sys.executable, "-W", "ignore", "-m", "unittest", "discover", "-s", start]
        if top:
            cmd += ["-t", top]
        env_ws = tempfile.mkdtemp(prefix="ljp_tests_")
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                           env={**__import__("os").environ, "LJP_WORKSPACE": env_ws})
        summary = [l for l in p.stderr.splitlines() if l.startswith(("Ran ", "OK", "FAILED"))]
        print(f"{name:<22} {' | '.join(summary)}")
        if p.returncode != 0:
            failed.append(name)
            print(p.stderr[-4000:])
    print("all suites passed" if not failed else f"FAILED: {', '.join(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
