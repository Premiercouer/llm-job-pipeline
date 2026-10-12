#!/usr/bin/env python3
"""Reproduce the two-stage gate cost and quality analysis from the published anonymized data.

    python3 experiments/two_stage_gate/scripts/analyze_gate.py            # print + write outputs
    python3 experiments/two_stage_gate/scripts/analyze_gate.py --check    # also assert the
                                                                          # headline numbers

Inputs (experiments/two_stage_gate/results/):
  calls.csv            one row per model call (170): stage, stratum, routing outcome, usage, cost
  samples.csv          the 150 sampled postings: stratum and historical decision
  review_outcomes.csv  outcome of the case-by-case review (139 cases; performed by Claude, see README)
  inputs.json          single-stage baselines and the production mix (not measured in this run)

Outputs (same directory): analysis.json, cost_breakdown.svg.
Standard library only; no model call, no network. Nothing here re-runs the gate itself.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import random
import statistics
import sys
from pathlib import Path

RESULTS = Path(__file__).resolve().parents[1] / "results"
SEED = 20261008  # same seed as the original analysis, so the bootstrap intervals match


def load_csv(name: str) -> list[dict]:
    with (RESULTS / name).open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def num(row: dict, key: str) -> float:
    return float(row[key])


def pctl(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    k = (len(xs) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def bootstrap_mean(xs: list[float], n: int = 10000, seed: int = SEED) -> list[float]:
    rng = random.Random(seed)
    vals = [statistics.mean(xs[rng.randrange(len(xs))] for _ in xs) for _ in range(n)]
    return [pctl(vals, 0.025), pctl(vals, 0.975)]


def wilson(k: int, n: int, z: float = 1.96) -> list[float]:
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [max(0.0, c - h), min(1.0, c + h)]


def solve(a: list[list[float]], b: list[float]) -> list[float]:
    """Gaussian elimination with partial pivoting (small dense system)."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(m[r][c]))
        m[c], m[p] = m[p], m[c]
        for r in range(n):
            if r != c:
                f = m[r][c] / m[c][c]
                m[r] = [x - f * y for x, y in zip(m[r], m[c])]
    return [m[i][n] / m[i][i] for i in range(n)]


def fit_prices(calls: list[dict]) -> dict:
    """Least-squares per-token prices: cost = w * cache_write + r * cache_read + o * output.
    input_tokens is 2 on every call, so its price is not identifiable and is left out."""
    keys = ("cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")
    x = [[num(c, k) for k in keys] for c in calls]
    y = [num(c, "total_cost_usd") for c in calls]
    ata = [[sum(r[i] * r[j] for r in x) for j in range(3)] for i in range(3)]
    aty = [sum(r[i] * yy for r, yy in zip(x, y)) for i in range(3)]
    w, r, o = solve(ata, aty)
    resid = [abs(yy - (w * a + r * b + o * c)) for (a, b, c), yy in zip(x, y)]
    return {"cache_write_per_mtok": w * 1e6, "cache_read_per_mtok": r * 1e6, "output_per_mtok": o * 1e6,
            "max_abs_residual_usd": max(resid)}


def breakdown(rows: list[dict], prices: dict) -> dict:
    w = statistics.mean(num(c, "cache_creation_input_tokens") for c in rows) * prices["cache_write_per_mtok"] / 1e6
    r = statistics.mean(num(c, "cache_read_input_tokens") for c in rows) * prices["cache_read_per_mtok"] / 1e6
    o = statistics.mean(num(c, "output_tokens") for c in rows) * prices["output_per_mtok"] / 1e6
    total = statistics.mean(num(c, "total_cost_usd") for c in rows)
    return {"n": len(rows), "posting_cache_write_usd": w, "rules_cache_read_usd": r, "output_usd": o,
            "total_usd": total, "posting_write_share": w / total}


def usage(rows: list[dict]) -> dict:
    def stat(key):
        xs = [num(c, key) for c in rows]
        return {"mean": statistics.mean(xs), "median": statistics.median(xs), "p90": pctl(xs, 0.9)}
    out = {k: stat(k) for k in ("cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens",
                                "thinking_tokens", "duration_ms", "total_cost_usd")}
    out["input_side_tokens"] = stat_list([num(c, "input_tokens") + num(c, "cache_creation_input_tokens")
                                          + num(c, "cache_read_input_tokens") for c in rows])
    out["n"] = len(rows)
    return out


def stat_list(xs: list[float]) -> dict:
    return {"mean": statistics.mean(xs), "median": statistics.median(xs), "p90": pctl(xs, 0.9)}


def analyze() -> dict:
    calls = load_csv("calls.csv")
    samples = {s["sample_id"]: s for s in load_csv("samples.csv")}
    review = load_csv("review_outcomes.csv")
    inputs = json.loads((RESULTS / "inputs.json").read_text(encoding="utf-8"))
    base = inputs["single_stage_baseline"]
    base_cost, base_cat = base["cost_per_posting_usd"], base["cost_by_stratum_usd"]
    pool = {k: v for k, v in inputs["candidate_pool_by_stratum"].items() if not k.startswith("_")}

    gate = [c for c in calls if c["stage"] == "gate"]
    st2 = [c for c in calls if c["stage"] == "stage2"]
    gate_main = [c for c in gate if c["is_warmup"] == "0"]
    gate_warm = [c for c in gate if num(c, "cache_read_input_tokens") > 0]
    st2_warm = [c for c in st2 if num(c, "cache_read_input_tokens") > 0]
    A: dict = {}

    # ---- run checks (measured)
    A["calls"] = {
        "gate": len(gate), "stage2": len(st2), "total": len(calls),
        "retries": sum(c["attempt"] != "1" for c in calls),
        "parse_failures": sum(c["parse_ok"] == "0" for c in calls),
        "max_num_turns": max(int(c["num_turns"]) for c in calls),
        "models": sorted({c["model"] for c in calls}),
        "gate_cached_prefix_tokens": sorted({int(c["cache_read_input_tokens"]) for c in gate_warm}),
        "stage2_cached_prefix_tokens": sorted({int(c["cache_read_input_tokens"]) for c in st2_warm}),
        "total_cost_usd": sum(num(c, "total_cost_usd") for c in calls),
    }

    # ---- routing and quality (measured; the review was performed by Claude, not by a person)
    routing: dict = {}
    for c in gate_main:
        key = c["gate_result"] if c["parse_ok"] == "1" else "PARSE_FAILED"
        routing[key] = routing.get(key, 0) + 1
    ended = [c for c in gate_main if c["terminates"] == "1"]
    by_stratum: dict = {}
    for c in gate_main:
        s = by_stratum.setdefault(c["stratum"], {"n": 0, "ended": 0})
        s["n"] += 1
        s["ended"] += c["terminates"] == "1"
    accepts = [c for c in gate_main if samples[c["sample_id"]]["historical_decision"] == "ACCEPT"]
    hist_hr_of_ended = sum(bool(samples[c["sample_id"]]["historical_hard_reject_type"]) for c in ended)
    judged = [r for r in review if r["gate_reject_judged_correct"] != ""]
    A["quality"] = {
        "sampled_postings": len(gate_main),
        "routing": routing,
        "ended_by_gate": len(ended),
        "sent_to_stage2": len(gate_main) - len(ended),
        "sample_stage2_share": (len(gate_main) - len(ended)) / len(gate_main),
        "ended_with_historical_hard_reject": hist_hr_of_ended,
        "historical_accepts": len(accepts),
        "historical_accepts_ended_by_gate": sum(c["terminates"] == "1" for c in accepts),
        "historical_accepts_ended_wilson95": wilson(sum(c["terminates"] == "1" for c in accepts), len(accepts)),
        "reviewed_cases": len(review),
        "reviewed_rejects": len(judged),
        "reviewed_rejects_judged_correct": sum(r["gate_reject_judged_correct"] == "1" for r in judged),
        "reviewed_rejects_correct_wilson95": wilson(sum(r["gate_reject_judged_correct"] == "1" for r in judged),
                                                    len(judged)),
        "anchor_supports_reject": sum(r["anchor_supports_reject"] == "1" for r in judged),
        "true_accepts_false_rejected": sum(r["true_accept_false_rejected"] == "1" for r in review),
        "by_stratum": by_stratum,
    }

    # ---- per-call cost (measured) and its decomposition (fitted prices)
    prices = fit_prices(calls)
    A["prices_fitted"] = prices
    A["per_call"] = {"gate_warm": breakdown(gate_warm, prices), "stage2_warm": breakdown(st2_warm, prices)}
    A["usage"] = {"gate_warm": usage(gate_warm), "stage2_warm": usage(st2_warm),
                  "gate_cold": usage([c for c in gate if c not in gate_warm]),
                  "stage2_cold": usage([c for c in st2 if c not in st2_warm])}
    A["bootstrap95"] = {"gate_warm_cost": bootstrap_mean([num(c, "total_cost_usd") for c in gate_warm]),
                        "stage2_warm_cost": bootstrap_mean([num(c, "total_cost_usd") for c in st2_warm])}
    g_cost = A["per_call"]["gate_warm"]["total_usd"]
    s_cost = A["per_call"]["stage2_warm"]["total_usd"]

    # ---- cost per posting (estimated: combines measured gate cost with baselines)
    r_sample = A["quality"]["sample_stage2_share"]
    ptot = sum(pool.values())
    end_rate = {s: v["ended"] / v["n"] for s, v in by_stratum.items()}
    share = {s: pool[s] / ptot for s in pool}
    cat_cost = {s: base_cat.get(s, base_cost) for s in pool}
    routed_share = sum(share[s] * (1 - end_rate.get(s, 0)) for s in pool)
    single_pw = sum(share[s] * cat_cost[s] for s in pool)
    routed_cost_pw = sum(share[s] * (1 - end_rate.get(s, 0)) * cat_cost[s] for s in pool)
    ended_cost_pw = single_pw - routed_cost_pw  # what the postings the gate ends cost in one stage
    ended_share = 1 - routed_share
    A["per_posting"] = {
        "single_stage_baseline_usd": base_cost,
        "sample_formula_two_stage_usd": g_cost + r_sample * s_cost,
        "sample_formula_change": (g_cost + r_sample * s_cost) / base_cost - 1,
        "production_mix_stage2_share_EST": routed_share,
        "production_mix_single_stage_usd_EST": single_pw,
        "production_mix_two_stage_usd_EST": g_cost + routed_cost_pw,
        "production_mix_change_EST": (g_cost + routed_cost_pw) / single_pw - 1,
        "input_side_tokens_sample_formula": A["usage"]["gate_warm"]["input_side_tokens"]["mean"]
        + r_sample * A["usage"]["stage2_warm"]["input_side_tokens"]["mean"],
        "latency_ms_production_mix_EST": A["usage"]["gate_warm"]["duration_ms"]["mean"]
        + routed_share * A["usage"]["stage2_warm"]["duration_ms"]["mean"],
    }
    # break-even: compare like with like - the postings the gate ends would have cost
    # ended_cost_pw / ended_share each in a single stage
    c_end = ended_cost_pw / ended_share
    A["break_even"] = {
        "single_stage_cost_of_gate_ended_postings_usd_EST": c_end,
        "max_stage2_share_to_break_even_EST": 1 - g_cost / c_end,
        "max_saving_if_gate_ended_everything_EST": 1 - g_cost / c_end,
        "naive_max_stage2_share_vs_average_baseline": 1 - g_cost / base_cost,
        "gate_cost_to_break_even_on_production_mix_usd_EST": single_pw - routed_cost_pw,
        "gate_cost_for_20pct_saving_on_production_mix_usd_EST": 0.8 * single_pw - routed_cost_pw,
    }

    # ---- 30 postings end to end (measured cost; the single-stage counterfactual is estimated)
    e2e_ids = sorted({c["sample_id"] for c in gate_main if c["in_end_to_end_subset"] == "1"})
    rows = []
    for sid in e2e_ids:
        g = next(c for c in gate_main if c["sample_id"] == sid)
        s = next((c for c in st2 if c["sample_id"] == sid), None)
        final = "REJECT" if g["terminates"] == "1" else (s["stage2_decision"] if s else None)
        cost = num(g, "total_cost_usd") + (num(s, "total_cost_usd") if s else 0.0)
        cost_excl_cold = num(g, "total_cost_usd") + ((s_cost if s["is_warmup"] == "1" else num(s, "total_cost_usd"))
                                                     if s else 0.0)
        counterfactual = (s_cost if s["is_warmup"] == "1" else num(s, "total_cost_usd")) if s \
            else base_cat.get(g["stratum"], base_cost)
        rows.append({"sample_id": sid, "final": final, "historical": samples[sid]["historical_decision"],
                     "cost": cost, "cost_excl_cold": cost_excl_cold, "single_stage_counterfactual": counterfactual,
                     "ended_by_gate": g["terminates"] == "1"})
    A["end_to_end"] = {
        "n": len(rows),
        "ended_by_gate": sum(r["ended_by_gate"] for r in rows),
        "decision_agrees_with_history": sum(r["final"] == r["historical"] for r in rows),
        "historical_accepts_still_accepted": [sum(r["final"] == "ACCEPT" for r in rows if r["historical"] == "ACCEPT"),
                                              sum(r["historical"] == "ACCEPT" for r in rows)],
        "cost_total_usd": sum(r["cost"] for r in rows),
        "cost_per_posting_usd": statistics.mean(r["cost"] for r in rows),
        "cost_per_posting_ci95": bootstrap_mean([r["cost"] for r in rows]),
        "cost_per_posting_excl_stage2_cold_start_usd": statistics.mean(r["cost_excl_cold"] for r in rows),
        "single_stage_counterfactual_per_posting_usd_EST": statistics.mean(r["single_stage_counterfactual"]
                                                                          for r in rows),
    }
    return A


# ------------------------------------------------------------------ figure

def svg_figure(A: dict) -> str:
    g, s = A["per_call"]["gate_warm"], A["per_call"]["stage2_warm"]
    pp = A["per_posting"]
    W, H, x0, x1 = 960, 470, 230, 860
    vmax = 0.05
    sx = lambda v: x0 + (x1 - x0) * v / vmax  # noqa: E731
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" role="img" '
           'aria-labelledby="t d">',
           '<title id="t">Two-stage gate: where the cost goes</title>',
           f'<desc id="d">Per call, writing the posting to the prompt cache costs ${g["posting_cache_write_usd"]:.4f} '
           f'in the gate and ${s["posting_cache_write_usd"]:.4f} in the full screen. Estimated cost per posting on '
           f'the production mix: single stage ${pp["production_mix_single_stage_usd_EST"]:.4f}, two stage '
           f'${pp["production_mix_two_stage_usd_EST"]:.4f}.</desc>',
           """<style>
  text { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif; fill: #0b0b0b; }
  .bg { fill: #fcfcfb; } .mu { fill: #52514e; font-size: 13px; } .lb { font-size: 14px; }
  .hd { font-size: 15px; font-weight: 600; } .t1 { font-size: 19px; font-weight: 700; }
  .val { font-size: 13px; font-weight: 600; } .in { fill: #ffffff; font-size: 12px; font-weight: 600; }
  .s1 { fill: #2a78d6; } .s2 { fill: #eb6834; } .s3 { fill: #8a8986; } .s4 { fill: #52514e; }
  .grid { stroke: #e5e4e0; stroke-width: 1; } .gap { stroke: #fcfcfb; stroke-width: 2; }
  @media (prefers-color-scheme: dark) {
    text { fill: #ffffff; } .bg { fill: #1a1a19; } .mu { fill: #c3c2b7; } .in { fill: #ffffff; }
    .s1 { fill: #3987e5; } .s2 { fill: #d95926; } .s3 { fill: #6f6e6a; } .s4 { fill: #a3a29d; }
    .grid { stroke: #383835; } .gap { stroke: #1a1a19; }
  }
</style>""",
           f'<rect class="bg" width="{W}" height="{H}" rx="8"/>',
           '<text class="t1" x="24" y="36">Two-stage gate: where the cost goes</text>',
           '<text class="mu" x="24" y="58">Measured warm calls, decomposed with per-token prices fitted to the '
           'reported costs. Bottom panel is an estimate (production mix).</text>']

    def grid(y_top, y_bot):
        for i in range(6):
            v = vmax * i / 5
            x = sx(v)
            out.append(f'<line class="grid" x1="{x:.1f}" y1="{y_top}" x2="{x:.1f}" y2="{y_bot}"/>')
            out.append(f'<text class="mu" x="{x:.1f}" y="{y_bot + 16}" text-anchor="middle">${v:.2f}</text>')

    def bar(y, label, sub, segs, total):
        out.append(f'<text class="lb" x="24" y="{y + 15}">{label}</text>')
        out.append(f'<text class="mu" x="24" y="{y + 32}">{sub}</text>')
        x = x0
        for cls, v, name, show, *tag in segs:
            w = sx(v) - x0
            out.append(f'<rect class="{cls} gap" x="{x:.1f}" y="{y}" width="{max(w, 0.5):.1f}" height="34" rx="3">'
                       f'<title>{label}: {name} ${v:.4f}</title></rect>')
            if show and w > 56:
                text = f"{tag[0]} ${v:.4f}" if tag else f"${v:.4f}"
                out.append(f'<text class="in" x="{x + w / 2:.1f}" y="{y + 22}" text-anchor="middle">{text}</text>')
            x += w
        out.append(f'<text class="val" x="{x + 8:.1f}" y="{y + 22}">${total:.4f}</text>')

    out.append('<text class="hd" x="24" y="96">Per call</text>')
    grid(108, 236)
    for y, label, sub, d in ((116, "Gate", f"n = {g['n']} warm calls", g), (176, "Full screen (stage 2)",
                                                                           f"n = {s['n']} warm calls", s)):
        bar(y, label, sub, [("s1", d["posting_cache_write_usd"], "write posting to cache", True),
                            ("s3", d["rules_cache_read_usd"], "read cached rules", False),
                            ("s2", d["output_usd"], "output", True)], d["total_usd"])
    ly = 270
    for i, (cls, name) in enumerate((("s1", "Write posting to cache"), ("s3", "Read cached rules"),
                                     ("s2", "Output"))):
        lx = x0 + i * 200
        out.append(f'<rect class="{cls}" x="{lx}" y="{ly - 11}" width="12" height="12" rx="2"/>'
                   f'<text class="mu" x="{lx + 18}" y="{ly}">{name}</text>')
    out.append('<text class="hd" x="24" y="318">Per posting, production mix (estimate)</text>')
    grid(330, 438)
    routed = pp["production_mix_two_stage_usd_EST"] - g["total_usd"]
    bar(338, "Single stage", "current design", [("s4", pp["production_mix_single_stage_usd_EST"],
                                                 "single-stage screen", False)],
        pp["production_mix_single_stage_usd_EST"])
    bar(390, "Two stage", f"gate + {pp['production_mix_stage2_share_EST']:.0%} sent on",
        [("s3", g["total_usd"], "gate call", True, "gate"), ("s4", routed, "stage 2 for routed postings", True,
                                                               "stage 2")],
        pp["production_mix_two_stage_usd_EST"])
    out.append(f'<text class="val" x="{sx(pp["production_mix_two_stage_usd_EST"]) + 80:.1f}" y="412">'
               f'{pp["production_mix_change_EST"]:+.0%}</text>')
    out.append("</svg>")
    return "\n".join(out) + "\n"


# ------------------------------------------------------------------ main

HEADLINE = {  # values reported in the README; --check fails if the data no longer reproduces them
    ("calls", "total"): 170,
    ("quality", "ended_by_gate"): 55,
    ("quality", "historical_accepts_ended_by_gate"): 0,
    ("quality", "reviewed_rejects_judged_correct"): 55,
    ("end_to_end", "decision_agrees_with_history"): 28,
}
HEADLINE_APPROX = {
    ("per_call", "gate_warm", "total_usd"): 0.0305,
    ("per_call", "gate_warm", "posting_cache_write_usd"): 0.0226,
    ("per_call", "stage2_warm", "total_usd"): 0.0469,
    ("per_posting", "sample_formula_two_stage_usd"): 0.0603,
    ("per_posting", "production_mix_single_stage_usd_EST"): 0.0367,
    ("per_posting", "production_mix_two_stage_usd_EST"): 0.0481,
    ("per_posting", "production_mix_change_EST"): 0.31,
}


def dig(d, path):
    for k in path:
        d = d[k]
    return d


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check", action="store_true", help="assert the headline numbers quoted in the README")
    args = ap.parse_args()
    A = analyze()
    (RESULTS / "analysis.json").write_text(json.dumps(A, indent=2) + "\n", encoding="utf-8")
    (RESULTS / "cost_breakdown.svg").write_text(svg_figure(A), encoding="utf-8")
    q, pc, pp, be, e = A["quality"], A["per_call"], A["per_posting"], A["break_even"], A["end_to_end"]
    pr = A["prices_fitted"]
    print(f"calls {A['calls']['total']} (gate {A['calls']['gate']}, stage 2 {A['calls']['stage2']}), "
          f"retries {A['calls']['retries']}, parse failures {A['calls']['parse_failures']}, "
          f"list-price total ${A['calls']['total_cost_usd']:.2f}")
    print(f"gate ended {q['ended_by_gate']}/{q['sampled_postings']}; historical ACCEPTs ended "
          f"{q['historical_accepts_ended_by_gate']}/{q['historical_accepts']}; reviewed rejects judged correct "
          f"{q['reviewed_rejects_judged_correct']}/{q['reviewed_rejects']} (Wilson "
          f"{q['reviewed_rejects_correct_wilson95'][0]:.1%}-{q['reviewed_rejects_correct_wilson95'][1]:.0%})")
    print(f"fitted prices per MTok: cache write ${pr['cache_write_per_mtok']:.2f}, cache read "
          f"${pr['cache_read_per_mtok']:.2f}, output ${pr['output_per_mtok']:.2f}; max residual "
          f"${pr['max_abs_residual_usd']:.6f}")
    for name, d in pc.items():
        print(f"{name:<12} write ${d['posting_cache_write_usd']:.4f} ({d['posting_write_share']:.0%})  read "
              f"${d['rules_cache_read_usd']:.4f}  output ${d['output_usd']:.4f}  total ${d['total_usd']:.4f}")
    print(f"per posting: sample formula ${pp['sample_formula_two_stage_usd']:.4f} vs ${pp['single_stage_baseline_usd']}"
          f" ({pp['sample_formula_change']:+.0%}); production mix (EST) ${pp['production_mix_two_stage_usd_EST']:.4f}"
          f" vs ${pp['production_mix_single_stage_usd_EST']:.4f} ({pp['production_mix_change_EST']:+.0%}), "
          f"stage-2 share {pp['production_mix_stage2_share_EST']:.1%}")
    print(f"break-even (EST): gate-ended postings cost ${be['single_stage_cost_of_gate_ended_postings_usd_EST']:.4f} "
          f"single-stage -> stage-2 share must be <= {be['max_stage2_share_to_break_even_EST']:.1%}; gate cost "
          f"<= ${be['gate_cost_to_break_even_on_production_mix_usd_EST']:.4f} to break even, <= "
          f"${be['gate_cost_for_20pct_saving_on_production_mix_usd_EST']:.4f} to save 20%")
    print(f"end to end: {e['decision_agrees_with_history']}/{e['n']} agree, ${e['cost_per_posting_usd']:.4f}/posting "
          f"(excl. cold start ${e['cost_per_posting_excl_stage2_cold_start_usd']:.4f}), single-stage counterfactual "
          f"(EST) ${e['single_stage_counterfactual_per_posting_usd_EST']:.4f}")
    print(f"wrote {RESULTS / 'analysis.json'} and {RESULTS / 'cost_breakdown.svg'}")
    if args.check:
        bad = [f"{'.'.join(k)} = {dig(A, k)} (expected {v})" for k, v in HEADLINE.items() if dig(A, k) != v]
        bad += [f"{'.'.join(k)} = {dig(A, k):.4f} (expected ~{v})" for k, v in HEADLINE_APPROX.items()
                if abs(dig(A, k) - v) > (0.006 if v > 0.2 else 0.00051)]
        for b in bad:
            print("CHECK FAILED:", b)
        if bad:
            return 1
        print("headline numbers reproduced")
    return 0


if __name__ == "__main__":
    sys.exit(main())
