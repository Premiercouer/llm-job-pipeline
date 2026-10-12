#!/usr/bin/env python3
"""Offline demo: illustrative JD -> retrieval -> evidence package -> fake output -> citation checks.

    pip install ".[rag]"                                   # numpy + scikit-learn, only for this experiment
    python3 experiments/candidate_fact_rag/scripts/demo.py [--out DIR] [--embedding hashing]

No API key, network or account. The generator is fact_rag.fake_model, a deterministic stand-in
that copies fact text: it shows that the mechanism works (IDs, provenance, checks), not how a
real model writes or how often it errs. The candidate (examples/candidate/) and the job
(fixtures/illustrative_job.json) are invented. Artefacts go to workspace/rag_demo/ (git-ignored).
Exit code 1 if a clean output fails a check or a planted defect goes unflagged.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

EXP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EXP))

from fact_rag import corpus, fake_model, pipeline  # noqa: E402


def dump(path: Path, obj) -> None:
    path.write_text(obj if isinstance(obj, str) else json.dumps(obj, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", type=Path, default=pipeline.REPO_ROOT / "workspace" / "rag_demo")
    ap.add_argument("--job", type=Path, default=EXP / "fixtures" / "illustrative_job.json")
    ap.add_argument("--embedding", choices=("tfidf_lsa", "hashing"), default=None,
                    help="override the dense backend (hashing needs no scikit-learn)")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    job = json.loads(args.job.read_text(encoding="utf-8"))

    s = pipeline.Setup(embedding_backend=args.embedding)
    m = corpus.manifest(s.snapshot)
    print("=== 1. Canonical facts (fictional candidate)")
    print(f"facts {m['facts']} (evidence {m['evidence_facts']}), entities {', '.join(m['entities'])}, "
          f"corpus {m['corpus_hash'][:12]}")
    f0 = s.snapshot["facts"][0]
    print(f"  e.g. {f0['fact_id']}  {f0['source']['file']}:{f0['source']['line_start']}-{f0['source']['line_end']}"
          f"  sha256 {f0['content_hash'][:12]}")
    dump(args.out / "corpus_manifest.json", m)

    print(f"\n=== 2. Retrieval for '{job['position']}' (illustrative JD; embedding {s.index.embedder.model_id})")
    r, rag = s.context(job, "rag")
    _, full = s.context(job, "full")
    kinds = sorted({q["type"] for q in r["queries"]})
    print(f"queries {len(r['queries'])} ({', '.join(kinds)}); dropped JD sections: "
          f"{', '.join(sorted({d['heading'] or d['kind'] for d in r['dropped_jd_sections']})) or 'none'}")
    ranking = ", ".join(f"{p['entity_id']} {p['score']:.2f}" for p in r["project_ranking"])
    print(f"project ranking: {ranking}")
    print(f"selected {len(r['selected_fact_ids'])} evidence facts; status {r['status']}"
          + (f" ({', '.join(r['insufficient_reasons'])})" if r["insufficient_reasons"] else ""))
    for c in sorted(r["candidates"], key=lambda c: -c["agg"])[:3]:
        print(f"  {c['fact_id']:<12} agg {c['agg']:.3f} bm25 {c['best_bm25_norm']:.2f} dense {c['best_dense']:.2f}"
              f" -> {c['reason']}")
    left_out = [c for c in r["candidates"] if not c["selected"]]
    for c in left_out[:3]:
        print(f"  {c['fact_id']:<12} not selected: {c['reason']}")
    dump(args.out / "retrieval_trace.json", r)

    print("\n=== 3. Evidence packages")
    for b in (full, rag):
        print(f"  {b['mode']:<4} {len(b['provided_fact_ids']):>3} facts  ~{b['token_estimate']:>5} tokens  "
              f"context {b['context_hash'][:12]}")
        dump(args.out / f"evidence_{b['mode']}.txt", b["text"])

    print("\n=== 4. Fake generation (mechanism check only; NOT a quality result)")
    ok = True
    for b in (full, rag):
        clean = fake_model.generate(s.snapshot, b, job, s.lexicon, r)
        res = s.check(clean, b)
        flawed, planted = fake_model.inject_errors(clean, s.snapshot, b)
        bad = s.check(flawed, b)
        sm = res["verification"]["summary"]
        print(f"  {b['mode']:<4} clean : {sm['claims']} claims, supported {sm['supported_auto']}, unknown "
              f"{sm['unknown']}, fail {sm['fail_auto']}, citations valid {sm['citation_valid_rate']:.0%}")
        by_claim = {row["claim_id"]: row for row in bad["verification"]["claims"]}
        caught = [p for p in planted if by_claim[p["claim_id"]]["status"] == "FAIL_AUTO"]
        print(f"  {b['mode']:<4} flawed: {len(caught)}/{len(planted)} planted defects flagged FAIL_AUTO")
        for p in planted:
            row = by_claim[p["claim_id"]]
            codes = sorted({f["code"] for f in row["flags"] if f["severity"] == "SEVERE"})
            print(f"        {p['claim_id']:<10} {p['defect']:<58} -> {', '.join(codes) or 'NOT FLAGGED'}")
        ok &= sm["fail_auto"] == 0 and len(caught) == len(planted)
        for name, obj in (("output_clean", clean), ("output_flawed", flawed), ("planted_defects", planted),
                          ("provenance_clean", res["provenance"]), ("verification_clean", res["verification"]),
                          ("verification_flawed", bad["verification"])):
            dump(args.out / f"{b['mode']}_{name}.json", obj)
        dump(args.out / f"{b['mode']}_resume.txt", res["resume_text"])
    pr = json.loads((args.out / "rag_provenance_clean.json").read_text())["claims"][0]
    src = pr["sources"][0]
    print(f"\n  provenance e.g. {pr['claim_id']} '{pr['text'][:50]}...' <- {src['fact_id']} "
          f"{src.get('file')}:{src.get('lines')}")
    print(f"\nartefacts: {args.out}")
    print("demo OK" if ok else "demo FAILED: a clean output failed or a planted defect was missed")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
