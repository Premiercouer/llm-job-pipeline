#!/usr/bin/env python3
"""Recompute the historical dev-run tables and figures from the published anonymized data.

    python3 experiments/candidate_fact_rag/scripts/analyze_dev_results.py [--check]

Input: results/historical_dev/{generations.csv, retrieval.csv, summary.json} - per-generation
usage, cost, latency and automatic-audit counts from the private run (6 dev jobs). These numbers
were measured on the historical private implementation and candidate, not on the public port.
Output (same directory): analysis.json, fig_cost_pairs.svg, fig_latency_pairs.svg,
fig_auto_quality.svg. Standard library only; no model call.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import random
import statistics as st
import sys
from pathlib import Path

DATA = Path(__file__).resolve().parents[1] / "results" / "historical_dev"
SEED = 20261011
ARMS = ("L", "A", "B", "B_prime")
NAME = {"L": "L  Read-agent (earlier production)", "A": "A  full fact library", "B": "B  retrieval v1",
        "B_prime": "B′ retrieval v2"}
SHORT = {"L": "L", "A": "A", "B": "B", "B_prime": "B′"}


def load() -> tuple[list[dict], list[dict], dict]:
    def rows(name):
        with (DATA / name).open(newline="", encoding="utf-8") as fh:
            return list(csv.DictReader(fh))
    g = rows("generations.csv")
    for u in g:
        for k in ("cost_usd", "latency_ms", "input_tokens", "cache_write", "cache_read", "output_tokens",
                  "thinking_tokens", "severe_auto", "major_auto", "unknown", "claims"):
            u[k] = float(u[k])
        u["rep"] = int(u["rep"])
        u["total_input"] = u["input_tokens"] + u["cache_write"] + u["cache_read"]
    return g, rows("retrieval.csv"), json.loads((DATA / "summary.json").read_text(encoding="utf-8"))


def pctl(xs, q):
    xs = sorted(xs)
    k = (len(xs) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def boot(xs, stat=st.mean, n=10000, seed=SEED):
    """Job-level bootstrap: resample jobs (never claims), report the 2.5 / 97.5 percentiles."""
    rng = random.Random(seed)
    vals = [stat([xs[rng.randrange(len(xs))] for _ in xs]) for _ in range(n)]
    return [pctl(vals, 0.025), pctl(vals, 0.975)]


def analyze(g, retr, summary) -> dict:
    A: dict = {"n_jobs": len({u["sample_id"] for u in g}), "generations": len(g)}
    arms = {}
    for arm in ARMS:
        us = [u for u in g if u["arm"] == arm]
        r0 = [u for u in us if u["rep"] == 0]
        arms[arm] = {
            "generations": len(us), "jobs": len(r0),
            "first_pass": sum(u["first_pass"] == "True" for u in us), "retries": sum(int(u["retries"]) for u in us),
            "total_input_mean_rep0": st.mean(u["total_input"] for u in r0),
            "cost_mean_all": st.mean(u["cost_usd"] for u in us),
            "latency_median_all_ms": st.median(u["latency_ms"] for u in us),
            "output_mean": st.mean(u["output_tokens"] for u in us), "thinking_mean": st.mean(u["thinking_tokens"] for u in us),
            "turns": sorted({u["num_turns"] for u in us}),
            "severe_auto_rep0": sum(u["severe_auto"] for u in r0), "major_auto_all": sum(u["major_auto"] for u in us),
            "unknown_share_mean": st.mean(u["unknown"] / u["claims"] for u in us),
            "citation_valid_rate_mean": st.mean(float(u["citation_valid_rate"]) for u in us)
            if all(u["citation_valid_rate"] for u in us) else None,
        }
    A["arms"] = arms
    A["context_reduction_vs_A"] = {b: 1 - arms[b]["total_input_mean_rep0"] / arms["A"]["total_input_mean_rep0"]
                                   for b in ("B", "B_prime")}
    cache = []
    for arm in ARMS:
        for cs in ("cold", "warm"):
            for kind, sel in (("first generation", lambda u: u["rep"] == 0), ("same-job repeat", lambda u: u["rep"] == 1)):
                us = [u for u in g if u["arm"] == arm and u["cache_state"] == cs and sel(u)]
                if us:
                    cache.append({"arm": arm, "cache_state": cs, "kind": kind, "n": len(us),
                                  "cost_mean": st.mean(u["cost_usd"] for u in us),
                                  "latency_median_ms": st.median(u["latency_ms"] for u in us),
                                  "cache_write_mean": st.mean(u["cache_write"] for u in us),
                                  "cache_read_mean": st.mean(u["cache_read"] for u in us)})
    A["cache_states"] = cache

    def unit(arm, sid, rep=0):
        return next((u for u in g if u["arm"] == arm and u["sample_id"] == sid and u["rep"] == rep), None)
    jobs = sorted({u["sample_id"] for u in g})
    paired = {}
    for b in ("B", "B_prime"):
        rows = []
        for sid in jobs:
            a, x = unit("A", sid), unit(b, sid)
            rows.append({"sample_id": sid, "A_cache": a["cache_state"], "cost_diff": x["cost_usd"] - a["cost_usd"],
                         "latency_diff_ms": x["latency_ms"] - a["latency_ms"]})
        warm = [r for r in rows if r["A_cache"] == "warm"]
        lat = [r["latency_diff_ms"] for r in rows]
        paired[b] = {"rows": rows,
                     "cost_diff_when_A_warm_mean": st.mean(r["cost_diff"] for r in warm),
                     "cost_diff_when_A_warm_boot95": boot([r["cost_diff"] for r in warm]),
                     "n_A_warm_jobs": len(warm),
                     "cost_diff_when_A_cold": [r["cost_diff"] for r in rows if r["A_cache"] == "cold"],
                     "latency_diff_mean_ms": st.mean(lat), "latency_diff_mean_boot95_ms": boot(lat),
                     "latency_diff_median_ms": st.median(lat), "latency_diff_range_ms": [min(lat), max(lat)],
                     "jobs_faster": sum(d < 0 for d in lat)}
    A["paired_vs_A"] = paired

    a_cold = st.mean(u["cost_usd"] for u in g if u["arm"] == "A" and u["cache_state"] == "cold")
    a_warm = st.mean(u["cost_usd"] for u in g if u["arm"] == "A" and u["cache_state"] == "warm")
    b_cold = {b: st.mean(u["cost_usd"] for u in g if u["arm"] == b and u["cache_state"] == "cold") for b in ("B", "B_prime")}
    breakeven = {b: next(n for n in range(1, 100) if a_cold + a_warm * (n - 1) < c * n) for b, c in b_cold.items()}
    A["sequential_batch_model_EST"] = {
        "_doc": "Cost of N jobs generated back to back within the cache TTL: A = cold + warm * (N - 1); B = cold_B * N. "
                "Derived from measured means; concurrent cold starts may duplicate cache writes, and parallel "
                "cache behavior was not evaluated.",
        "A_cold": a_cold, "A_warm": a_warm, "B_cold": b_cold,
        "smallest_N_where_A_is_cheaper": breakeven}

    proxy = {}
    for cfg in ("B_v1", "B_prime_v2"):
        rs = [r for r in retr if r["retrieval_config"] == cfg]
        hit = sum(int(r["L_project_in_context"]) + int(r["A_project_in_context"]) for r in rs)
        proxy[cfg] = {"L_or_A_project_in_context": [hit, 2 * len(rs)],
                      "evidence_tokens_est_mean": st.mean(float(r["evidence_tokens_est"]) for r in rs),
                      "projects_in_context": len(rs[0]["projects_in_context"].split("|")),
                      "retrieval_insufficient": sum(r["status"] != "OK" for r in rs),
                      "retrieval_ms_per_job_range": [min(float(r["retrieval_ms_per_job"]) for r in rs),
                                                     max(float(r["retrieval_ms_per_job"]) for r in rs)],
                      "one_time_index_ms": float(rs[0]["one_time_index_ms"]),
                      "jobs_where_L_and_A_chose_same_project_absent": [
                          r["sample_id"] for r in rs if r["L_project"] == r["A_project"] and r["A_project_in_context"] == "0"]}
    A["retrieval_proxy"] = proxy
    A["historical_L_production_telemetry"] = summary["historical_L_production_telemetry"]
    A["spend_ledger_usd"] = summary["spend_ledger_usd"]
    return A


# ------------------------------------------------------------------ SVG

STYLE = """<style>
  text { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif; fill: #0b0b0b; }
  .bg { fill: #fcfcfb; } .mu { fill: #52514e; font-size: 12px; } .lb { font-size: 13px; }
  .t1 { font-size: 17px; font-weight: 700; } .warn { fill: #b3261e; font-size: 12px; font-weight: 600; }
  .grid { stroke: #e5e4e0; stroke-width: 1; } .link { stroke: #c9c8c3; stroke-width: 1.5; }
  .L { fill: #8a8986; stroke: #8a8986; } .A { fill: #2a78d6; stroke: #2a78d6; }
  .B { fill: #eb6834; stroke: #eb6834; } .B_prime { fill: #1baf7a; stroke: #1baf7a; }
  .hollow { fill: #fcfcfb; stroke-width: 2; } .ring { stroke: #fcfcfb; stroke-width: 1.5; }
  @media (prefers-color-scheme: dark) {
    text { fill: #ffffff; } .bg { fill: #1a1a19; } .mu { fill: #c3c2b7; } .warn { fill: #f2b8b5; }
    .grid { stroke: #383835; } .link { stroke: #55554f; }
    .L { fill: #a3a29d; stroke: #a3a29d; } .A { fill: #3987e5; stroke: #3987e5; }
    .B { fill: #d95926; stroke: #d95926; } .B_prime { fill: #199e70; stroke: #199e70; }
    .hollow { fill: #1a1a19; } .ring { stroke: #1a1a19; }
  }
</style>"""


def marker(arm, x, y, hollow, tip):
    cls = f"{arm} {'hollow' if hollow else 'ring'}"
    t = f"<title>{tip}</title>"
    if arm == "L":
        return f'<path class="{cls}" d="M{x:.1f},{y - 6:.1f} L{x + 6:.1f},{y + 5:.1f} L{x - 6:.1f},{y + 5:.1f} Z">{t}</path>'
    if arm == "B_prime":
        return f'<path class="{cls}" d="M{x:.1f},{y - 6:.1f} L{x + 6:.1f},{y:.1f} L{x:.1f},{y + 6:.1f} L{x - 6:.1f},{y:.1f} Z">{t}</path>'
    if arm == "B":
        return f'<rect class="{cls}" x="{x - 5:.1f}" y="{y - 5:.1f}" width="10" height="10" rx="2">{t}</rect>'
    return f'<circle class="{cls}" cx="{x:.1f}" cy="{y:.1f}" r="5.5">{t}</circle>'


def legend(out, y, extra=None):
    x = 24
    for arm in ARMS:
        out.append(marker(arm, x + 6, y - 4, False, NAME[arm]))
        out.append(f'<text class="mu" x="{x + 16}" y="{y}">{NAME[arm]}</text>')
        x += 16 + 7.1 * len(NAME[arm]) + 18
    if extra:
        out.append(f'<text class="mu" x="{x}" y="{y}">{extra}</text>')


def pair_chart(g, metric, scale, fmt, title, xlabel, vmax, ticks, note, vmin=0.0):
    jobs = sorted({u["sample_id"] for u in g})
    W, row, top = 920, 40, 104
    H = top + row * len(jobs) + 70
    x0, x1 = 90, W - 40
    sx = lambda v: x0 + (x1 - x0) * (v - vmin) / (vmax - vmin)  # noqa: E731
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" role="img" '
           f'aria-label="{title}">', f"<title>{title}</title>", STYLE, f'<rect class="bg" width="{W}" height="{H}" rx="8"/>',
           f'<text class="t1" x="24" y="32">{title}</text>', f'<text class="warn" x="24" y="52">{note}</text>']
    legend(out, 78, "hollow = same-job repeat")
    for v in ticks:
        out.append(f'<line class="grid" x1="{sx(v):.1f}" y1="{top - 10}" x2="{sx(v):.1f}" y2="{top + row * len(jobs)}"/>')
        out.append(f'<text class="mu" x="{sx(v):.1f}" y="{top + row * len(jobs) + 16}" text-anchor="middle">{fmt(v)}</text>')
    out.append(f'<text class="mu" x="{(x0 + x1) / 2:.0f}" y="{top + row * len(jobs) + 36}" text-anchor="middle">{xlabel}</text>')
    offs = {"L": -12, "A": -4, "B": 4, "B_prime": 12}
    for i, sid in enumerate(jobs):
        yc = top + i * row + row / 2 - 6
        out.append(f'<text class="lb" x="24" y="{yc + 4:.1f}">{sid}</text>')
        pts = {u["arm"]: u for u in g if u["sample_id"] == sid and u["rep"] == 0}
        for b in ("B", "B_prime"):
            if "A" in pts and b in pts:
                out.append(f'<line class="link" x1="{sx(pts["A"][metric] * scale):.1f}" y1="{yc + offs["A"]:.1f}" '
                           f'x2="{sx(pts[b][metric] * scale):.1f}" y2="{yc + offs[b]:.1f}"/>')
        for u in [u for u in g if u["sample_id"] == sid]:
            v = u[metric] * scale
            tip = f"{sid} {SHORT[u['arm']]} rep {u['rep']} ({u['cache_state']}): {fmt(v)}"
            out.append(marker(u["arm"], sx(v), yc + offs[u["arm"]], u["rep"] == 1, tip))
    out.append("</svg>")
    return "\n".join(out) + "\n"


def quality_chart(g):
    jobs = sorted({u["sample_id"] for u in g})
    panels = [("severe_auto", "SEVERE flags (AUTO)", 4), ("major_auto", "MAJOR flags (AUTO)", 4),
              ("unknown", "UNKNOWN claims (AUTO)", 25)]
    W, top, row = 920, 126, 40
    H = top + row * len(jobs) + 50
    pw, gap, x0 = 250, 30, 70
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" role="img" '
           'aria-label="Automatic audit counts per job">', "<title>Automatic audit counts per job</title>", STYLE,
           f'<rect class="bg" width="{W}" height="{H}" rx="8"/>',
           '<text class="t1" x="24" y="32">Automatic audit per job (first generation)</text>',
           '<text class="warn" x="24" y="52">AUTO heuristics, not human-confirmed. n = 6 dev jobs. The one L SEVERE flag '
           'was judged a false positive by a Claude review.</text>']
    legend(out, 78)
    offs = {"L": -12, "A": -4, "B": 4, "B_prime": 12}
    for p, (m, label, vmax) in enumerate(panels):
        px = x0 + p * (pw + gap)
        out.append(f'<text class="lb" x="{px}" y="{top - 14}">{label}</text>')
        for v in range(0, vmax + 1, 1 if vmax <= 4 else 5):
            x = px + pw * v / vmax
            out.append(f'<line class="grid" x1="{x:.1f}" y1="{top - 6}" x2="{x:.1f}" y2="{top + row * len(jobs)}"/>')
            out.append(f'<text class="mu" x="{x:.1f}" y="{top + row * len(jobs) + 16}" text-anchor="middle">{v}</text>')
        for i, sid in enumerate(jobs):
            yc = top + i * row + row / 2 - 6
            if p == 0:
                out.append(f'<text class="lb" x="24" y="{yc + 4:.1f}">{sid}</text>')
            for arm in ARMS:
                u = next((x for x in g if x["sample_id"] == sid and x["arm"] == arm and x["rep"] == 0), None)
                if u is None:
                    continue
                w = pw * u[m] / vmax
                y = yc + offs[arm] - 3
                tip = f"{sid} {SHORT[arm]}: {int(u[m])}"
                out.append(f'<rect class="{arm}" x="{px}" y="{y:.1f}" width="{max(w, 1.5):.1f}" height="6" rx="2">'
                           f"<title>{tip}</title></rect>")
    out.append("</svg>")
    return "\n".join(out) + "\n"


EXPECTED = {  # README headline values; --check fails if the data stops reproducing them
    "A_input": 46698, "B_input": 15052, "B_prime_input": 16995, "A_warm": 0.1218, "B_cold": 0.2016,
    "breakeven_B": 5,
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    g, retr, summary = load()
    A = analyze(g, retr, summary)
    (DATA / "analysis.json").write_text(json.dumps(A, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    note = "Historical private dev run: n = 6 jobs, CLI-reported list-price cost. Not a held-out test."
    (DATA / "fig_cost_pairs.svg").write_text(pair_chart(
        g, "cost_usd", 1, lambda v: f"${v:.2f}", "Cost per generation, by dev job", "USD per generation (list price)",
        0.5, [0, 0.1, 0.2, 0.3, 0.4, 0.5], note), encoding="utf-8")
    (DATA / "fig_latency_pairs.svg").write_text(pair_chart(
        g, "latency_ms", 1 / 1000, lambda v: f"{v:.0f} s", "End-to-end latency per generation, by dev job",
        "seconds per generation (axis starts at 20 s)", 55, [20, 25, 30, 35, 40, 45, 50, 55],
        "Historical private dev run: n = 6 jobs, end-to-end wall time of each CLI call. Not a held-out test.", 20),
        encoding="utf-8")
    (DATA / "fig_auto_quality.svg").write_text(quality_chart(g), encoding="utf-8")

    ar, pv, bm, rp = A["arms"], A["paired_vs_A"], A["sequential_batch_model_EST"], A["retrieval_proxy"]
    print(f"{A['n_jobs']} dev jobs, {A['generations']} generations")
    for arm in ARMS:
        a = ar[arm]
        print(f"  {SHORT[arm]:<3} n={a['generations']:<2} input {a['total_input_mean_rep0']:>8,.0f} tok  cost "
              f"${a['cost_mean_all']:.3f}  latency {a['latency_median_all_ms'] / 1000:.1f} s  turns {a['turns']}  "
              f"SEVERE {a['severe_auto_rep0']:.0f}  MAJOR {a['major_auto_all']:.0f}  UNKNOWN {a['unknown_share_mean']:.0%}")
    print("context reduction vs A: " + ", ".join(f"{SHORT[b]} {v:.0%}" for b, v in A["context_reduction_vs_A"].items()))
    for c in A["cache_states"]:
        print(f"  {SHORT[c['arm']]:<3} {c['cache_state']:<4} {c['kind']:<16} n={c['n']}  ${c['cost_mean']:.3f}  "
              f"{c['latency_median_ms'] / 1000:.1f} s")
    for b, p in pv.items():
        lo, hi = p["cost_diff_when_A_warm_boot95"]
        llo, lhi = p["latency_diff_mean_boot95_ms"]
        print(f"  {SHORT[b]}-A: cost {p['cost_diff_when_A_warm_mean']:+.3f} on {p['n_A_warm_jobs']} A-warm jobs "
              f"(boot95 {lo:+.3f}..{hi:+.3f}); when A cold {p['cost_diff_when_A_cold'][0]:+.3f}; latency mean "
              f"{p['latency_diff_mean_ms'] / 1000:+.1f} s (boot95 {llo / 1000:+.1f}..{lhi / 1000:+.1f}), median "
              f"{p['latency_diff_median_ms'] / 1000:+.1f} s, faster on "
              f"{p['jobs_faster']}/{A['n_jobs']}")
    print(f"  sequential batch (EST): A ${bm['A_cold']:.3f} + ${bm['A_warm']:.3f}x(N-1) vs B ${bm['B_cold']['B']:.3f}xN "
          f"-> A cheaper from N = {bm['smallest_N_where_A_is_cheaper']['B']} (B′: {bm['smallest_N_where_A_is_cheaper']['B_prime']})")
    for k, v in rp.items():
        print(f"  {k}: L/A-chosen project in context {v['L_or_A_project_in_context'][0]}/{v['L_or_A_project_in_context'][1]}, "
              f"~{v['evidence_tokens_est_mean']:.0f} evidence tokens, L=A project missing in {v['jobs_where_L_and_A_chose_same_project_absent']}")
    print(f"wrote analysis.json and 3 figures to {DATA.relative_to(DATA.parents[3])}")
    if args.check:
        got = {"A_input": round(ar["A"]["total_input_mean_rep0"]), "B_input": round(ar["B"]["total_input_mean_rep0"]),
               "B_prime_input": round(ar["B_prime"]["total_input_mean_rep0"]), "A_warm": round(bm["A_warm"], 4),
               "B_cold": round(bm["B_cold"]["B"], 4), "breakeven_B": bm["smallest_N_where_A_is_cheaper"]["B"]}
        bad = {k: (got[k], v) for k, v in EXPECTED.items() if got[k] != v}
        for k, (x, v) in bad.items():
            print(f"CHECK FAILED: {k} = {x} (expected {v})")
        if bad:
            return 1
        print("headline numbers reproduced")
    return 0


if __name__ == "__main__":
    sys.exit(main())
