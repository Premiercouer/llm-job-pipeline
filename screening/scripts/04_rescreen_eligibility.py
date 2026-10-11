"""Re-screen already-screened jobs that touch eligibility rules, in place.

Use after a change to the Eligibility Facts / eligibility rules: instead of re-running whole
batches, only jobs whose title or JD mentions student / co-op / internship / French /
citizenship (or whose JD is written in French) get a fresh job-screener worker. Their
results are replaced inside the existing results/<platform>/<date>/result_NNN.json
checkpoints; every other result is left untouched. Run 03 afterwards to rebuild the output.

A job whose re-screen fails keeps its previous result and is reported.
--worker / JOB_MATCH_WORKER and --max-budget-usd behave as in 02 (single-turn: serial warm-up first,
guardrail violations are GLOBAL fatal). Each rewritten checkpoint gets a `rescreens` audit entry.
Do not run while 02 is still screening the same date.

Usage:
  python3 scripts/04_rescreen_eligibility.py --date 2026-10-01 [--scope all|language] [--workers 4] [--dry-run]
"""
import argparse
import json
from datetime import datetime
import re
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

import screening_core as core

ELIGIBILITY_PATTERN = re.compile(
    r"co-?op\b|work term|intern(ship)?\b|\bstudents?\b|enrol|returning to school"
    r"|french|bilingu|fran[cç]ais|citizen", re.I)
LANGUAGE_PATTERN = re.compile(r"french|bilingu|fran[cç]ais", re.I)
# French job titles: the shared detector in screening_core (also gates 02's blank-JD handling).
FRENCH_TITLE = core.FRENCH_TITLE
FRENCH_WORDS = re.compile(r"\b(nous|vous|avec|pour|les|des|une|est|dans|votre|expérience)\b", re.I)
FRENCH_WORD_RATIO = 0.06  # English JDs score ~0.00-0.01, French JDs ~0.15


def is_french_jd(jd):
    words = jd.split()
    return bool(words) and len(FRENCH_WORDS.findall(jd)) / len(words) > FRENCH_WORD_RATIO


def is_language_related(job):
    jd = job["jd"] or ""
    return bool(LANGUAGE_PATTERN.search(jd) or FRENCH_TITLE.search(job["position"] or "")) or is_french_jd(jd)


def needs_rescreen(job, scope):
    if scope == "language":
        return is_language_related(job)
    jd = job["jd"] or ""
    return bool(ELIGIBILITY_PATTERN.search(f"{job['position']} {jd}")) or is_language_related(job)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", required=True)
    ap.add_argument("--workers", type=int, default=4, help="parallel job-screener processes")
    ap.add_argument("--timeout", type=int, default=core.DEFAULT_TIMEOUT)
    ap.add_argument("--model")
    ap.add_argument("--scope", choices=["all", "language"], default="all",
                    help="all = student/co-op/language/citizenship; language = French-related only")
    ap.add_argument("--worker", choices=core.WORKER_MODES,
                    help=f"worker mode (default: ${core.WORKER_ENV} or {core.DEFAULT_WORKER})")
    ap.add_argument("--max-budget-usd", type=float, help="stop launching workers once this much would be spent")
    ap.add_argument("--dry-run", action="store_true", help="list selected jobs only")
    args = ap.parse_args()
    mode = core.resolve_worker_mode(args.worker)
    sys.stdout.reconfigure(line_buffering=True)
    day = args.date

    # Select jobs; every batch involved must already have a valid checkpoint.
    selected = []  # (platform, batch_index, batch_jobs, job, old_result)
    for platform in core.PLATFORMS:
        for _, batch, jobs in core.load_batches(platform, day):
            idx = batch["batch_index"]
            rpath = core.result_path(platform, day, idx)
            err = core.validate_checkpoint(rpath, platform, idx, jobs) if rpath.exists() else "missing"
            if err:
                sys.exit(f"{platform} batch {idx:03d}: checkpoint not usable ({err}); run 02 first")
            old = {r["source_key"]: r for r in json.loads(rpath.read_text(encoding="utf-8"))["results"]}
            selected += [(platform, idx, jobs, j, old[j["source_key"]]) for j in jobs if needs_rescreen(j, args.scope)]

    print(f"selected {len(selected)} job(s) in "
          f"{len({(p, i) for p, i, *_ in selected})} batch(es) for {day}")
    if args.dry_run:
        for p, i, _, j, old in selected:
            print(f"  {p} {i:03d} {old['decision']:7} {j['company']} — {j['position']}")
        print("DONE exit=0")
        return

    new, failed, done = {}, [], 0
    fatal = None

    def record(p, i, j, result=None, error=None):
        nonlocal done
        done += 1
        if error is None:
            new[(p, j["source_key"])] = result
        else:
            failed.append((p, i, j, str(error)))
            print(f"FAILED: platform={p} batch={i:03d} source_key={j['source_key']} reason={error}"
                  " (previous result kept)")
        if done % 10 == 0 or done == len(selected):
            print(f"PROGRESS rescreen {done}/{len(selected)} | failed {len(failed)}")

    with core.Worker(mode, core.find_claude(), args.model, args.max_budget_usd) as worker:
        print(worker.describe())
        # Blank-JD jobs resolve locally (no model call); only model jobs go through warm-up / the pool.
        queue = []
        for p, i, _, j, _ in selected:
            local = core.local_result(j)
            if local is None:
                queue.append((p, i, j))
            else:
                record(p, i, j, result=local)
        try:
            warm, queue = core.warm_up(worker, queue, lambda it: it[2], args.timeout)
        except (core.GlobalFatalError, core.BudgetExceeded) as e:
            fatal, warm, queue = e, None, []
        if warm:
            (p, i, j), result, error = warm
            record(p, i, j, result=result, error=error)
        if queue:
            with ThreadPoolExecutor(max_workers=args.workers) as ex:
                futures = {ex.submit(core.screen_with_retry, j, worker, args.timeout): (p, i, j) for p, i, j in queue}
                for fut in as_completed(futures):
                    p, i, j = futures[fut]
                    try:
                        record(p, i, j, result=fut.result())
                    except core.GlobalFatalError as e:
                        fatal = fatal or e
                        core.stop_event.set()
                        core.terminate_active_workers()
                    except core.BudgetExceeded as e:
                        fatal = fatal or e
                    except core.PipelineError as e:
                        if not isinstance(e, core.StopRequested):
                            record(p, i, j, error=e)
        manifest = worker.manifest()
        for line in core.usage_report(worker):
            print(line)
    if fatal is not None:
        # The run stopped: jobs not started keep their previous result; completed re-screens are kept.
        kind = "budget" if isinstance(fatal, core.BudgetExceeded) else "GLOBAL fatal environment error"
        print(f"FAILED: {kind}: {fatal}; jobs not re-screened keep their previous result")

    # Write back per batch, validating the full checkpoint before replacing it.
    by_batch = defaultdict(list)
    for p, i, jobs, j, _ in selected:
        by_batch[(p, i)].append(j["source_key"])
    for (p, i), keys in by_batch.items():
        rpath = core.result_path(p, day, i)
        data = json.loads(rpath.read_text(encoding="utf-8"))
        replaced = sum((p, r["source_key"]) in new for r in data["results"])
        if not replaced:
            continue  # every selected job of this batch failed / was not started: leave it as is
        data["results"] = [new.get((p, r["source_key"]), r) for r in data["results"]]
        # Audit only (validation / 03 ignore it); older checkpoints without it stay valid.
        data.setdefault("rescreens", []).append({"at": datetime.now().isoformat(timespec="seconds"),
                                                 "scope": args.scope, "replaced": replaced, **manifest})
        jobs = next(jb for pp, ii, jb, _, _ in selected if (pp, ii) == (p, i))
        tmp = rpath.with_suffix(".json.rescreen")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        err = core.validate_checkpoint(tmp, p, i, jobs)
        if err:
            tmp.unlink()
            print(f"FAILED: {p} batch {i:03d}: rewritten checkpoint invalid ({err}); left unchanged")
            continue
        tmp.replace(rpath)

    changes = []
    for p, i, _, j, old in selected:
        r = new.get((p, j["source_key"]))
        if r and r["decision"] != old["decision"]:
            changes.append((p, j, old, r))
    print(f"\nrescreened: {len(new)}  failed (kept old): {len(failed)}  decision changes: {len(changes)}")
    print("transitions:", dict(Counter(f"{o['decision']}→{r['decision']}" for _, _, o, r in changes)))
    for p, j, old, r in changes:
        print(f"  {old['decision']} → {r['decision']} | {p} | {j['company']} — {j['position']}"
              f" | {r['hard_reject']['type'] or '-'} | {r['reason'][:140]}")
    exit_code = 1 if failed or fatal is not None else 0
    print(f"DONE exit={exit_code}")
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
