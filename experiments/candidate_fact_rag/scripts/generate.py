#!/usr/bin/env python3
"""One real generation for one job, in full or rag context mode. Dry run unless --execute is given.

    python3 experiments/candidate_fact_rag/scripts/generate.py --job JOB.json --mode rag             # plan only
    python3 experiments/candidate_fact_rag/scripts/generate.py --job JOB.json --mode rag --execute   # 1 model call

Cost boundary: an --execute run makes exactly ONE model call (no retry) through the pipeline's
own interface (common/llm.py; --backend cli = `claude -p`, api = Messages API with your key).
In the historical experiment one such call cost about $0.12-0.46 at list price (see the README);
the first full-mode call of a batch pays the cache write of the whole fact library. The CLI's
reported cost is a list-price figure, not what a subscription charges. Without --execute the
script only builds the prompts and prints their size; it never starts a model process.

JOB.json: {"company": ..., "position": ..., "jd": ...}. The candidate defaults to the fictional
examples/candidate/; --candidate-dir points elsewhere. Outputs go to workspace/rag_runs/ (git-ignored).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

EXP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EXP))
sys.path.insert(0, str(EXP.parents[1]))

from fact_rag import contract, pipeline, prompts  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--job", type=Path, required=True)
    ap.add_argument("--mode", choices=("full", "rag"), required=True)
    ap.add_argument("--candidate-dir", type=Path, default=pipeline.DEFAULT_CANDIDATE)
    ap.add_argument("--retrieval-config", type=Path, default=pipeline.DEFAULT_RETRIEVAL)
    ap.add_argument("--backend", default="cli", help="cli | cli:<model> | api | api:<model> (common/llm.py)")
    ap.add_argument("--model", default=None)
    ap.add_argument("--timeout", type=float, default=300)
    ap.add_argument("--out", type=Path, default=pipeline.REPO_ROOT / "workspace" / "rag_runs")
    ap.add_argument("--execute", action="store_true", help="actually call the model (one call)")
    args = ap.parse_args()

    job = json.loads(args.job.read_text(encoding="utf-8"))
    s = pipeline.Setup(args.candidate_dir, args.retrieval_config)
    r, bundle = s.context(job, args.mode)
    sp, meta = prompts.system_prompt(s.snapshot["candidate_name"], bundle, s.experience_ids)
    up = prompts.user_prompt(job)
    print(f"mode {args.mode}: {len(bundle['provided_fact_ids'])} facts, evidence ~{bundle['token_estimate']} tokens, "
          f"system prompt ~{len(sp) // 4} tokens, retrieval {r['status']}")
    if not args.execute:
        print("dry run: no model call made (add --execute to make exactly one call)")
        return 0

    from common import llm  # imported only when a real call is requested
    run_dir = args.out / time.strftime("%Y%m%d_%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    with llm.make_backend(args.backend, model=args.model, label="rag_experiment") as backend:
        res = backend.complete(sp, up, args.timeout)
    record = {"mode": args.mode, "prompt_meta": meta, "usage": res.usage.as_dict(), "backend": res.backend}
    try:
        obj = contract.parse_output(res.text, s.experience_ids)
        checked = s.check(obj, bundle)
        record["verification_summary"] = checked["verification"]["summary"]
        for name in ("provenance", "verification"):
            (run_dir / f"{name}.json").write_text(json.dumps(checked[name], indent=2) + "\n", encoding="utf-8")
        (run_dir / "resume.txt").write_text(checked["resume_text"], encoding="utf-8")
    except contract.ContractError as e:
        record["contract_error"] = str(e)
    (run_dir / "raw_output.txt").write_text(res.text, encoding="utf-8")
    (run_dir / "record.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: record[k] for k in record if k != "prompt_meta"}, indent=2))
    return 0 if "contract_error" not in record else 1


if __name__ == "__main__":
    sys.exit(main())
