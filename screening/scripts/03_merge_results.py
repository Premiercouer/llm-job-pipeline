"""Deterministically merge today's screening results into one consolidated file.

Reads:   input_batches/<platform>/<date>_<platform>_batch_NNN.json
         results/<platform>/<date>/result_NNN.json
Writes:  .runtime/<date>/screened_all.json        full internal result (audit / debugging)
         output/<date>_<HHMM>_jobs.json             Resume Assistant handoff: unique ACCEPT jobs only

No Claude calls. Any completeness problem stops the merge — nothing is partially written.
HHMM is the local time the handoff file is written, so several runs per day each keep their file.

Duplicate postings (same job listed on several platforms, or twice on one platform) are
detected at this output layer: titles mostly overlap (>= TITLE_WORD_OVERLAP of the shorter
title's words appear in the other) AND JD text overlap >= DUP_JD_SIMILARITY.
Company names are not compared — platforms spell them differently ("TD" vs "TD Bank").
Every record is kept; duplicates point at their primary via `duplicate_of`, and the
top-level counts and ACCEPT list cover unique jobs only.

Usage:
  python3 scripts/03_merge_results.py [--date YYYY-MM-DD]
"""
import argparse
import json
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from itertools import combinations
from datetime import date, datetime

import screening_core as core  # batch discovery, normalization, checkpoint validation, atomic writes

ROOT = core.ROOT
RUNTIME_ROOT = core.DATA_ROOT / ".runtime"
OUTPUT_ROOT = core.DATA_ROOT / "output"
DOWNSTREAM_FIELDS = ["company", "position", "jd", "apply_url", "source_key"]
METADATA_FIELDS = ["source_key", "platform", "date", "position", "company", "location", "apply_url"]
SCREENING_FIELDS = ["decision", "reason", "hard_reject", "core_responsibilities",
                    "anchor_direct_match", "main_gap"]
DUP_JD_SIMILARITY = 0.85  # observed: true duplicates 0.89-1.00, distinct same-title jobs <= 0.70
SHINGLE_WORDS = 5
# Share of the shorter title's words that must appear in the other title before JDs are compared.
# Tolerates title noise ("IoT & Data Intelligence Sept. 30, 2026" vs "IoT and Data Intelligence",
# extra " – Tableau, ..." suffix) but keeps template-JD roles apart that differ by one level/role
# word ("Senior" vs "Lead AI Engineer", "Analyst" vs "Manager, Investment Insights": 0.67-0.75).
TITLE_WORD_OVERLAP = 0.8


def merge_platform(platform, day, problems, warnings):
    """Return merged job records for one platform in batch/job order; append issues to problems.

    A platform with no batches for the day is skipped with a warning, not a failure.
    """
    try:
        batches = core.load_batches(platform, day)
    except core.PipelineError as e:
        problems.append(str(e))
        return []

    rdir = core.result_dir(platform, day)
    if not batches:
        orphans = sorted(p.name for p in rdir.glob("result_*.json")) if rdir.is_dir() else []
        note = f" ({len(orphans)} result file(s) in {core.display_path(rdir)} ignored)" if orphans else ""
        warnings.append(f"{platform}: no batches for {day} — platform skipped{note}")
        return []

    expected = {core.result_path(platform, day, b["batch_index"]).name for _, b, _ in batches}
    if rdir.is_dir():
        extra = sorted(p.name for p in rdir.glob("result_*.json") if p.name not in expected)
        if extra:
            # Batches are the source of truth; leftovers from an earlier split are ignored.
            warnings.append(f"{platform}: ignored result files without a matching batch: {extra}")

    merged = []
    for _, batch, jobs in batches:
        idx = batch["batch_index"]
        rpath = core.result_path(platform, day, idx)
        if not rpath.exists():
            problems.append(f"{platform} batch {idx:03d}: missing {core.display_path(rpath)}")
            continue
        err = core.validate_checkpoint(rpath, platform, idx, jobs)
        if err:
            problems.append(f"{platform} batch {idx:03d}: {rpath.name} invalid: {err}")
            continue

        by_key = {r["source_key"]: r
                  for r in json.loads(rpath.read_text(encoding="utf-8"))["results"]}
        for job in jobs:  # batch order, not result-file order
            result = by_key[job["source_key"]]
            record = {k: job[k] for k in METADATA_FIELDS}
            record.update({k: result[k] for k in SCREENING_FIELDS})
            merged.append((record, job["jd"] or ""))
    return merged


# --------------------------------------------------------------------------
# Duplicate detection (output layer only; screening results are never changed)
# --------------------------------------------------------------------------

def _norm(text):
    text = unicodedata.normalize("NFKC", text or "").casefold()
    return re.sub(r"[^\w]+", " ", text).strip()


def _shingles(jd):
    words = _norm(jd).split()
    return {tuple(words[i:i + SHINGLE_WORDS]) for i in range(len(words) - SHINGLE_WORDS + 1)}


def jd_similarity(a, b):
    """Share of the shorter JD's word 5-grams that also appear in the other JD."""
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def _title_words(position):
    return set(_norm((position or "").replace("&", " and ")).split())


def title_overlap(a, b):
    """Share of the shorter title's words that also appear in the other title."""
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def mark_duplicates(merged):
    """Annotate records in place; return list of duplicate groups (each a list of records, primary first).

    Primary = first ACCEPT in the group if any (so an ACCEPT is never hidden), else the first
    record in platform/batch order.
    """
    records = [r for r, _ in merged]
    shingles = [_shingles(jd) for _, jd in merged]
    parent = list(range(len(records)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    titles = [_title_words(r["position"]) for r in records]
    for i, k in combinations(range(len(records)), 2):
        if (title_overlap(titles[i], titles[k]) >= TITLE_WORD_OVERLAP
                and jd_similarity(shingles[i], shingles[k]) >= DUP_JD_SIMILARITY):
            parent[find(k)] = find(i)

    clusters = defaultdict(list)
    for i in range(len(records)):
        clusters[find(i)].append(i)

    for r in records:
        r["duplicate_of"] = None
        r["duplicates"] = []
    groups = []
    for idxs in clusters.values():
        if len(idxs) < 2:
            continue
        group = [records[i] for i in idxs]
        primary = next((r for r in group if r["decision"] == "ACCEPT"), group[0])
        for r in group:
            if r is primary:
                continue
            r["duplicate_of"] = {"platform": primary["platform"], "source_key": primary["source_key"]}
            primary["duplicates"].append({k: r[k] for k in
                                          ("platform", "source_key", "company", "apply_url", "decision")})
        groups.append([primary] + [r for r in group if r is not primary])
    return groups


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", default=date.today().isoformat(), help="batch date (default: today)")
    args = ap.parse_args()
    day = args.date

    problems, warnings, merged = [], [], []
    for platform in core.PLATFORMS:
        merged.extend(merge_platform(platform, day, problems, warnings))

    for w in warnings:
        print(f"WARNING: {w}", file=sys.stderr)
    if not merged and not problems:
        problems.append(f"no batches for {day} on any platform")

    dup = [k for k, c in Counter((r["platform"], r["source_key"]) for r, _ in merged).items() if c > 1]
    if dup:
        problems.append(f"duplicate source_key across results: {dup[:5]}")

    if problems:
        print("Merge FAILED — nothing written:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        sys.exit(1)

    groups = mark_duplicates(merged)
    jobs = [r for r, _ in merged]
    unique = [j for j in jobs if j["duplicate_of"] is None]
    conflicts = [g for g in groups if len({r["decision"] for r in g}) > 1]
    counts = Counter(j["decision"] for j in unique)

    out = RUNTIME_ROOT / day / "screened_all.json"
    core.write_json_atomic(out, {
        "date": day,
        "total": len(unique),
        "accept": counts["ACCEPT"],
        "reject": counts["REJECT"],
        "unable_to_assess": counts["UNABLE TO ASSESS"],
        "screened": len(jobs),
        "duplicates": len(jobs) - len(unique),
        "decision_conflicts": [[{k: r[k] for k in ("platform", "source_key", "company", "position",
                                                   "decision")} for r in g] for g in conflicts],
        "jobs": jobs,
    })

    # Downstream handoff: unique ACCEPT jobs with full JD, no screening internals.
    handoff = [{k: (jd if k == "jd" else r[k]) for k in DOWNSTREAM_FIELDS}
               for r, jd in merged if r["duplicate_of"] is None and r["decision"] == "ACCEPT"]
    handoff_path = OUTPUT_ROOT / f"{day}_{datetime.now():%H%M}_jobs.json"
    core.write_json_atomic(handoff_path, {"date": day, "job_count": len(handoff), "jobs": handoff})

    print("Job Screening complete\n")
    print(f"Total: {len(unique)}  (screened {len(jobs)}, duplicates removed {len(jobs) - len(unique)})")
    print(f"ACCEPT: {counts['ACCEPT']}")
    print(f"REJECT: {counts['REJECT']}")
    print(f"UNABLE TO ASSESS: {counts['UNABLE TO ASSESS']}")
    print(f"\nOutput: {core.display_path(out)}")
    print(f"Resume Assistant handoff: {core.display_path(handoff_path)} ({len(handoff)} jobs)\n")
    accepted = [j for j in unique if j["decision"] == "ACCEPT"]
    if accepted:
        print("ACCEPT:")
        print("Company | Position | Apply URL")
        for j in accepted:
            print(f"{j['company']} | {j['position']} | {j['apply_url']}")
            for d in j["duplicates"]:
                print(f"    also on {d['platform']}: {d['apply_url']}")
    if groups:
        print(f"\nDuplicates ({len(groups)} groups):")
        for g in groups:
            where = ", ".join(f"{r['platform']}/{r['company']}" for r in g)
            print(f"  {g[0]['position']} — {where}")
    if conflicts:
        print("\nDECISION CONFLICTS (same job, different decisions — kept as ACCEPT by default):")
        for g in conflicts:
            print("  " + " vs ".join(f"{r['platform']}:{r['decision']}" for r in g)
                  + f" | {g[0]['company']} — {g[0]['position']}")


if __name__ == "__main__":
    main()
