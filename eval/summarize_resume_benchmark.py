#!/usr/bin/env python3
"""Aggregate a benchmark_resume_single_turn.py results.json into a Markdown table.

Only counts, costs and rates are printed — never resume text, JDs or company names — so the
output can go into public documentation.

    python3 eval/summarize_resume_benchmark.py /private/dir/results.json
"""
from __future__ import annotations

import json
import statistics as st
import sys
from pathlib import Path


def mean(xs):
    xs = [x for x in xs if x is not None]
    return st.mean(xs) if xs else None


def fmt(x, money=False, pct=False):
    if x is None:
        return "—"
    if money:
        return f"${x:.3f}"
    if pct:
        return f"{100 * x:.0f}%"
    return f"{x:.1f}" if isinstance(x, float) else str(x)


def main() -> int:
    r = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    rows = r["rows"]
    n = len(rows)
    a_v1 = [x["A"]["v1_cost"] / max(x["A"]["v1_calls"], 1) for x in rows]
    a_v2 = [x["A"]["v2_cost"] / max(x["A"]["v2_calls"], 1) for x in rows]
    a_job = [x["A"]["v1_cost"] + x["A"]["v2_cost"] + x["A"]["trim_cost"] for x in rows]
    b_job = [x["B_v1"]["cost"] + x["B_v2"]["cost"] + x["B_v2"].get("trim_cost", 0.0) for x in rows]
    warm = rows[1:] if n > 1 else rows  # the first job pays the one-off cache write
    lines = [f"Sample: {n} production jobs (A replayed from telemetry, B run live); "
             f"benchmark spend ${r['spent_usd']:.2f}.", "",
             "| Metric | A: agent workers (production) | B: single-turn, no tools |", "|---|---|---|",
             f"| V1 cost per call | {fmt(mean(a_v1), True)} | {fmt(mean([x['B_v1']['cost'] for x in rows]), True)} "
             f"(warm cache {fmt(mean([x['B_v1']['cost'] for x in warm]), True)}) |",
             f"| V2 cost per call | {fmt(mean(a_v2), True)} | {fmt(mean([x['B_v2']['cost'] for x in rows]), True)} "
             f"(warm cache {fmt(mean([x['B_v2']['cost'] for x in warm]), True)}) |",
             f"| Trim passes per job | {fmt(mean([x['A']['trim_calls'] for x in rows]))} | "
             f"{fmt(mean([x['B_v2'].get('trims') for x in rows]))} |",
             f"| Model cost per job (V1 + V2 + trims) | {fmt(mean(a_job), True)} | {fmt(mean(b_job), True)} |",
             f"| V1 valid on first attempt | — | {sum(x['B_v1']['valid'] for x in rows)}/{n} |",
             f"| V2 valid on first attempt | — | {sum(x['B_v2']['valid'] for x in rows)}/{n} |",
             f"| Final resume fits one page | {n}/{n} | {sum(x['B_v2'].get('final_pages') == 1 for x in rows)}/{n} |",
             f"| Unsupported numbers per V1 (lower = fewer invented metrics) | "
             f"{fmt(mean([len(x['A']['v1_unsupported']) for x in rows]))} | "
             f"{fmt(mean([len(x['B_v1'].get('unsupported', [])) for x in rows]))} |",
             f"| Unsupported numbers per final V2 | {fmt(mean([len(x['A']['v2_unsupported']) for x in rows]))} | "
             f"{fmt(mean([len(x['B_v2'].get('final_unsupported', [])) for x in rows]))} |",
             f"| JD keyword coverage, V1 (offline mock scorer) | "
             f"{fmt(mean([x['A']['v1_jd_coverage'] for x in rows]))} | "
             f"{fmt(mean([x['B_v1'].get('jd_coverage') for x in rows]))} |",
             f"| JD keyword coverage, V2 | {fmt(mean([x['A']['v2_jd_coverage'] for x in rows]))} | "
             f"{fmt(mean([x['B_v2'].get('jd_coverage') for x in rows]))} |",
             f"| Seconds per V1 call | {fmt(mean([x['A']['v1_seconds'] / max(x['A']['v1_calls'], 1) for x in rows]))} | "
             f"{fmt(mean([x['B_v1']['seconds'] for x in rows]))} |"]
    letters = r.get("cover_letters", [])
    if letters:
        lines.append(f"| Cover letters valid | — | {sum(c['valid'] for c in letters)}/{len(letters)} |")
    lines += ["", "Prompt-injection cases (JD with an embedded instruction, B only):", "",
              "| Case | Model complied | Output guard verdict | Turns / tool use |", "|---|---|---|---|"]
    for i in r.get("injections", []):
        lines.append(f"| {i['case']} | {', '.join(i['complied']) or 'no'} | "
                     f"{'blocked: ' + i['output_guard'][:60] if i['output_guard'] else 'clean'} | "
                     f"{i['turns']} / {'yes' if i['tools_or_denials'] else 'none'} |")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
