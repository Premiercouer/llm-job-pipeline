"""Screen every job in today's existing batches with a fresh job-screener worker.

Reads:   input_batches/<platform>/<date>_<platform>_batch_NNN.json   (written by split_*.py)
Writes:  results/<platform>/<date>/result_NNN.json                   (one file per batch)

1 job = 1 fresh Claude process: a tool-less single-turn request with the rules embedded in the
system prompt (--worker single-turn, the default) or `claude -p --agent job-screener`
(--worker agent, rollback mode; default overridable with JOB_MATCH_WORKER). Single-turn screens the first job serially (warm-up,
so the prompt cache exists) before starting the pool, and treats any guardrail violation (tool use,
model change, prompt hash, missing usage, unexpected context) as GLOBAL fatal; only transient
network / API / timeout failures are retried in that mode.
1 batch = 1 result/checkpoint file, written only after every job in it passes validation.

Scheduling: batches with a valid checkpoint are skipped; every job of every other batch
(all selected platforms) goes into one global queue, screened by at most --workers
concurrent workers. Results are put back at their original batch/job position; when the
last job of a batch finishes, the controller validates and atomically writes its checkpoint.
A job that fails both attempts only blocks its own batch's checkpoint; the rest continue,
and the run ends with exit 1. --workers 1 screens the jobs one by one in batch order.

Workers receive only source_key / position / jd; other metadata stays controller-side.
A job whose jd is null / whitespace-only gets a local UNABLE TO ASSESS result (validated like
any worker result) without starting Claude — unless its title may be French (FRENCH_TITLE),
in which case the worker decides, since a French title alone is a LANGUAGE reject.
Retry: a failed attempt gets one fresh-worker retry, except non-retryable errors.
A reliably recognised environment failure (not logged in, HTTP 401/403, agent not found,
CLI rejects the command or cannot start) is GLOBAL fatal: no retry, queued jobs are
cancelled, running workers are stopped, partial batches are not checkpointed, exit 1.

Usage:
  python3 scripts/02_run_screening.py                 # screen today's batches (3 workers)
  python3 scripts/02_run_screening.py --workers 1     # serial
  python3 scripts/02_run_screening.py --dry-run       # validate batches/checkpoints, no Claude calls
  python3 scripts/02_run_screening.py                 # normal: single-turn, no budget cap
  python3 scripts/02_run_screening.py --worker agent  # rollback to the legacy agent worker
  python3 scripts/02_run_screening.py --platform greenhouse --date 2026-10-01

Progress protocol (one line per event, flushed immediately, for log monitoring):
  PROGRESS <platform> batch i/N | jobs done/total | ACCEPT a REJECT r UNABLE u | failed f [note]
      one line per batch event: checkpoint written, skipped ([checkpoint]) or [incomplete: ...];
      jobs / decisions / failed are global across platforms; batches may finish out of order
  FAILED: <reason>       per job that failed both attempts (run continues), and a final summary;
                         "FAILED: GLOBAL fatal environment error: ..." when the run is stopped
  DONE exit=<code>       always the last line (0 ok, 1 any failure, 130 interrupted)
"""
import argparse
import json
import os
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import screening_core as core

DEFAULT_WORKERS = 3  # concurrent fresh job-screener processes


def write_checkpoint(path, payload, platform, batch_index, jobs):
    """Validate the full batch payload as a checkpoint, then atomically replace `path`.

    Returns None on success, else the validation error (and `path` is left untouched).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    err = core.validate_checkpoint(tmp, platform, batch_index, jobs)
    if err:
        tmp.unlink()
        return err
    tmp.replace(path)
    return None


# --------------------------------------------------------------------------
# Controller: plan batches, run one global job pool, checkpoint per batch
# --------------------------------------------------------------------------

def new_stats():
    return {"batches": 0, "jobs": 0, "checkpoint_batches": 0, "checkpoint_jobs": 0,
            "written_batches": 0, "failed_batches": 0, "pending_batches": 0, "pending_jobs": 0,
            "screened": 0, "local": 0, "blank_pending": 0, "failed_jobs": 0, "decisions": Counter()}


def print_progress(run, platform, idx, n_batches, note=""):
    """One protocol line per batch event; job counts and decisions are global across platforms.

    jobs done = jobs in valid checkpoints + jobs screened this run + jobs that finally failed.
    Decisions count only results that are in a checkpoint (existing or written this run).
    """
    stats = run["stats"].values()
    d = sum((s["decisions"] for s in stats), Counter())
    failed = sum(s["failed_jobs"] for s in stats)
    core.log(f"PROGRESS {platform} batch {idx}/{n_batches} | jobs {run['jobs_done']}/{run['total_jobs']}"
        f" | ACCEPT {d['ACCEPT']} REJECT {d['REJECT']} UNABLE {d['UNABLE TO ASSESS']}"
        f" | failed {failed}{note}")


def plan(platforms, day, run):
    """Load every selected batch; return (checkpointed, pending) in platform/batch order.

    checkpointed: [(platform, idx, n_batches, jobs, results)] — valid checkpoints, never re-screened
    pending:      [batch dict] — missing/invalid checkpoint; all of its jobs go into the global pool
    """
    checkpointed, pending = [], []
    for platform in platforms:
        label, stats = core.PLATFORMS[platform]["label"], run["stats"][platform]
        batches = core.load_batches(platform, day)
        if not batches:
            core.log(f"{label}: no batches for {day} in {core.display_path(core.batch_dir(platform))}")
            continue
        for _, batch, jobs in batches:
            idx = batch["batch_index"]
            out = core.result_path(platform, day, idx)
            stats["batches"] += 1
            stats["jobs"] += len(jobs)
            run["total_jobs"] += len(jobs)
            if out.exists():
                err = core.validate_checkpoint(out, platform, idx, jobs)
                if err is None:
                    results = json.loads(out.read_text(encoding="utf-8"))["results"]
                    checkpointed.append((platform, idx, len(batches), jobs, results))
                    continue
                core.log(f"{label} batch {idx:03d}: checkpoint {out.name} invalid ({err}) — batch will be re-run")
            stats["pending_batches"] += 1
            stats["pending_jobs"] += len(jobs)
            stats["blank_pending"] += sum(core.resolves_locally(j) for j in jobs)
            pending.append({"platform": platform, "idx": idx, "n_batches": len(batches), "jobs": jobs,
                            "out": out, "results": [None] * len(jobs), "remaining": len(jobs),
                            "failed": []})
    return checkpointed, pending


def finish_batch(pb, run):
    """Called on the controller thread once every job of a pending batch has finished."""
    platform, idx, jobs, out = pb["platform"], pb["idx"], pb["jobs"], pb["out"]
    stats = run["stats"][platform]
    if not pb["failed"]:
        # results[] is indexed by the job's position in the batch, so as_completed order never leaks in.
        # screening_run: worker mode / model / prompt + source hashes / CLI version (audit only;
        # validation and 03 ignore it, so checkpoints without it stay valid).
        err = write_checkpoint(out, {"platform": platform, "batch_index": idx,
                                     "job_count": len(jobs), "screening_run": run["worker"].manifest(),
                                     "results": pb["results"]},
                               platform, idx, jobs)
        if err is None:
            stats["written_batches"] += 1
            stats["decisions"].update(r["decision"] for r in pb["results"])
            core.log(f"  {platform} batch {idx:03d} PASSED → {core.display_path(out)}")
            print_progress(run, platform, idx, pb["n_batches"])
            return
        pb["failed"].append(f"checkpoint validation: {err}")
        core.log(f"FAILED: platform={platform} batch={idx:03d} checkpoint invalid ({err}); not written",
            file=sys.stderr)
    stats["failed_batches"] += 1
    run["failed_batches"].append(f"{platform} {idx:03d}")
    print_progress(run, platform, idx, pb["n_batches"],
                   f" [incomplete: {len(pb['failed'])} failure(s), no checkpoint written]")


def record_job(pb, i, run, result=None, error=None, local=False):
    """Controller thread: store one finished job (result or final error); finish its batch when complete."""
    job, platform, idx = pb["jobs"][i], pb["platform"], pb["idx"]
    stats = run["stats"][platform]
    run["jobs_done"] += 1
    if error is not None:
        stats["failed_jobs"] += 1
        pb["failed"].append(job["source_key"])
        core.log(f"FAILED: platform={platform} batch={idx:03d} source_key={job['source_key']} reason={error}"
            " — batch will not be checkpointed; other jobs continue", file=sys.stderr)
    else:
        pb["results"][i] = result
        stats["local" if local else "screened"] += 1
        name = f"{job['company']} — {job['position']}"
        core.log(f"  [{run['jobs_done']}/{run['total_jobs']}] {platform} {idx:03d} #{i + 1}"
            f" {name[:80]} → {result['decision']}{' (blank JD, no Claude call)' if local else ''}")
    pb["remaining"] -= 1
    if pb["remaining"] == 0:
        finish_batch(pb, run)


def abort_workers(ex):
    """Start nothing new (cancel queued futures, block retries/launches) and stop running workers."""
    core.stop_event.set()
    ex.shutdown(wait=False, cancel_futures=True)
    core.terminate_active_workers()


def fatal_message(e, pb, i):
    return (f"GLOBAL fatal environment error: {e} (first seen at platform={pb['platform']}"
            f" batch={pb['idx']:03d} source_key={pb['jobs'][i]['source_key']}); no further Claude"
            " workers started, partial batches not checkpointed")


def run_pool(pending, worker, timeout, workers, run):
    """Screen every pending job through one pool of at most `workers` concurrent fresh workers.

    Blank-JD jobs are resolved locally first and never reach the pool. Single-turn mode screens the
    first queued job serially first (core.warm_up) and starts the pool only after it. Worker threads
    only run screen_with_retry(); results, stats, printing and checkpoint writes all happen here on
    the controller thread. A GlobalFatalError (or Ctrl+C) stops the whole pool; a reached
    --max-budget-usd stops launching, lets running workers finish, then fails the run.
    """
    queue = []
    for pb in pending:
        for i, job in enumerate(pb["jobs"]):
            local = core.local_result(job)
            if local is None:
                queue.append((pb, i))
            else:
                record_job(pb, i, run, result=local, local=True)
    core.log(f"global queue: {len(queue)} job(s) in {len(pending)} batch(es), {workers} worker(s)")
    if not queue:
        return
    try:
        warm, queue = core.warm_up(worker, queue, lambda it: it[0]["jobs"][it[1]], timeout)
    except core.GlobalFatalError as e:
        run["global_fatal"] += 1
        run["cancelled"] = len(queue) - 1
        pb, i = queue[0]
        raise core.GlobalFatalError(fatal_message(e, pb, i))
    except core.BudgetExceeded as e:
        run["cancelled"] = len(queue)
        raise core.PipelineError(f"{e}; no worker pool started")
    if warm:
        (pb, i), result, error = warm
        record_job(pb, i, run, result=result, error=error)
    ex = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="screener")
    futures = {}
    try:
        # Submitted in platform/batch/job order; the executor starts them FIFO.
        futures = {ex.submit(core.screen_with_retry, pb["jobs"][i], worker, timeout): (pb, i) for pb, i in queue}
        for fut in as_completed(futures):
            pb, i = futures[fut]
            try:
                result = fut.result()
            except core.GlobalFatalError as e:
                run["global_fatal"] += 1
                raise core.GlobalFatalError(fatal_message(e, pb, i))
            except core.BudgetExceeded:
                continue  # not started; reported once below
            except core.PipelineError as e:
                record_job(pb, i, run, error=e)
            else:
                record_job(pb, i, run, result=result)
    except BaseException:
        # Global fatal error, Ctrl+C or an unexpected error.
        # Partial batches are simply dropped — only complete batches were ever written.
        abort_workers(ex)
        raise
    finally:
        ex.shutdown(wait=True)
        # Never started: cancelled in the queue, or picked up after the stop and not launched.
        run["cancelled"] = sum(f.cancelled() or isinstance(f.exception(), core.StopRequested) for f in futures)
    if worker.budget and worker.budget.stop:
        raise core.PipelineError(f"{worker.budget.stop}; {run['cancelled']} job(s) not started, their batches"
                                 " not checkpointed — re-run 02 to continue")


def print_report(platform, stats, dry_run):
    d = stats["decisions"]
    print(f"\n== {core.PLATFORMS[platform]['label']} ==")
    print(f"batches: {stats['batches']}  jobs: {stats['jobs']}")
    print(f"batches skipped (valid checkpoint): {stats['checkpoint_batches']}"
          f"  ({stats['checkpoint_jobs']} jobs)")
    if dry_run:
        print(f"batches pending (dry-run): {stats['pending_batches']}  ({stats['pending_jobs']} jobs,"
              f" {stats['blank_pending']} blank JD → local UNABLE TO ASSESS)")
    else:
        print(f"batches written this run: {stats['written_batches']}")
        print(f"batches incomplete (no checkpoint): {stats['failed_batches']}")
    print(f"jobs screened this run: {stats['screened']}  (+ {stats['local']} blank JD resolved locally)")
    print(f"ACCEPT: {d['ACCEPT']}  REJECT: {d['REJECT']}  UNABLE TO ASSESS: {d['UNABLE TO ASSESS']}"
          f"  (checkpointed results only)")
    print(f"failed jobs: {stats['failed_jobs']}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", default=date.today().isoformat(), help="batch date (default: today)")
    ap.add_argument("--platform", choices=sorted(core.PLATFORMS), action="append",
                    help="limit to a platform (repeatable; default: all)")
    ap.add_argument("--timeout", type=int, default=core.DEFAULT_TIMEOUT, help="seconds per worker")
    ap.add_argument("--model", help="override worker model (default: agent's 'inherit')")
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                    help=f"max concurrent job-screener processes (default: {DEFAULT_WORKERS})")
    ap.add_argument("--worker", choices=core.WORKER_MODES,
                    help=f"worker mode (default: ${core.WORKER_ENV} or {core.DEFAULT_WORKER})")
    ap.add_argument("--backend", default=os.environ.get("JOB_MATCH_BACKEND", "cli"),
                    help="cli (Claude Code CLI, default) or api / api:<model> (Messages API, single-turn only)")
    ap.add_argument("--max-budget-usd", type=float,
                    help="stop launching workers once this much would be spent (envelope total_cost_usd)")
    ap.add_argument("--dry-run", action="store_true", help="validate only; no Claude calls")
    args = ap.parse_args()
    if args.workers < 1:
        ap.error("--workers must be >= 1")
    if args.max_budget_usd is not None and args.max_budget_usd <= 0:
        ap.error("--max-budget-usd must be > 0")
    # Line-buffer both streams so a log file can be followed live while running in the background.
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)

    platforms = args.platform or list(core.PLATFORMS)
    run = {"stats": {p: new_stats() for p in platforms}, "total_jobs": 0, "jobs_done": 0,
           "failed_batches": [], "global_fatal": 0, "cancelled": 0, "worker": None}
    exit_code = 0
    try:
        mode = core.resolve_worker_mode(args.worker)
        # Dry-run validates batches/checkpoints only; it never needs the Claude CLI.
        if args.dry_run:
            core.log(f"worker: {mode} (dry-run)")
        else:
            run["worker"] = core.Worker(mode, core.find_claude() if args.backend == "cli" else None, args.model, args.max_budget_usd, args.backend)
            core.log(run["worker"].describe())
        checkpointed, pending = plan(platforms, args.date, run)

        for platform, idx, n_batches, jobs, results in checkpointed:
            stats = run["stats"][platform]
            stats["checkpoint_batches"] += 1
            stats["checkpoint_jobs"] += len(jobs)
            stats["decisions"].update(r["decision"] for r in results)
            run["jobs_done"] += len(jobs)
            print_progress(run, platform, idx, n_batches, " [checkpoint]")

        if args.dry_run:
            for pb in pending:
                core.log(f"DRY-RUN: would screen {pb['platform']} batch {pb['idx']:03d}"
                    f" ({len(pb['jobs'])} jobs) → {core.display_path(pb['out'])}")
            core.log(f"DRY-RUN: {len(pending)} pending batch(es), "
                f"{sum(len(pb['jobs']) for pb in pending)} pending job(s)")
        else:
            run_pool(pending, run["worker"], args.timeout, args.workers, run)
            if run["failed_batches"]:
                failed_jobs = sum(s["failed_jobs"] for s in run["stats"].values())
                raise core.PipelineError(
                    f"{failed_jobs} job(s) failed; batches without checkpoint: "
                    f"{', '.join(run['failed_batches'])} — re-run 02 to retry them")
    except core.PipelineError as e:
        core.log(f"\nFAILED: {e}", file=sys.stderr)
        exit_code = 1
    except KeyboardInterrupt:
        core.log("\nFAILED: interrupted; completed batches are checkpointed, partial batches discarded.",
            file=sys.stderr)
        exit_code = 130
    except BaseException as e:
        core.log(f"\nFAILED: unexpected {type(e).__name__}: {e}", file=sys.stderr)
        exit_code = 1
        raise
    finally:
        if run["worker"]:
            run["worker"].close()  # every worker has finished: remove the single-turn temp cwd
        for p in platforms:
            print_report(p, run["stats"][p], args.dry_run)
        if run["worker"]:
            print(f"\n== Run metrics ==")
            print(f"Claude invocations: {core.metrics['invocations']}  (incl. retries)")
            print(f"local short-circuits: {sum(s['local'] for s in run['stats'].values())}")
            print(f"worker retries: {core.metrics['retries']}")
            print(f"global fatal errors: {run['global_fatal']}")
            print(f"cancelled pending jobs: {run['cancelled']}")
            for line in core.usage_report(run["worker"]):
                print(line)
        print(f"DONE exit={exit_code}")
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
