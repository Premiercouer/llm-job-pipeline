# Operational experience

What happened when this pipeline was used for a real job search: scale, the daily loop, failures
and how they were recovered, where a person steps in, how the design changed, and what is still
open. It is written from run records, telemetry, benchmarks and decision records only.

**Scope.** One user, one local macOS machine, workers started through the Claude Code CLI. This is
personal-scale use (hundreds of postings a day), not a service or a multi-user deployment. The
private setup currently collects from seven job platforms, two of which are published here
([discovery](../discovery/README.md#private-workflow-and-published-scope)); the counts below cover
all the sources that were running, so they are not results of the two published adapters alone.
Each benchmark keeps the sample its own report describes.

**Cost basis.** Every dollar figure is the list-price equivalent the CLI reports per call
(`total_cost_usd`), not what a subscription actually charges.

| Label | Meaning |
|---|---|
| **measured** | counted from run records: merged screening results, run logs, worker telemetry |
| **benchmark** | a controlled test with its own report in [`benchmarks/`](benchmarks/) or [`../experiments/`](../experiments/) |
| **inference** | an interpretation of the records; stated as such |
| **not recorded** | the records do not say; left open rather than guessed |

Run logs, telemetry and checkpoints stay private: they contain job titles, companies and
descriptions. Where they are used below, only counts and outcomes are reported.

## Scale and environment

**Measured** from the merged screening results of 9 run days (2026-10-01 to 2026-10-10, no run on
10-05):

| | |
|---|---|
| Postings screened by a full-description model call | 2,846 (163–451 per day) |
| Flagged as cross-source or repeated duplicates at merge | 160 (5.6%) |
| Unique postings | 2,686 |
| Unique ACCEPTs | 168 (6.3% of unique postings) |
| Screening cost after the single-turn switch | $0.037 per posting over 1,890 calls with logs kept ([benchmark](benchmarks/screening_single_turn.md#what-shipped)) |

The low acceptance rate is the reason for the design: most postings fail on a hard rule, so the
daily cost is dominated by screening postings that end up rejected.

## Daily workflow

The loop is the one [`run_pipeline.py`](../run_pipeline.py) runs: discovery per source, then
screening, merge and deduplication, then resume generation for each unique ACCEPT, and finally a
queue that a person reviews. Several merged outputs per day are normal; each handoff file carries
its write time (`YYYY-MM-DD_HHMM`), so a later run does not overwrite an earlier one.

What a person does each day (from the stage design, see [Human intervention](#human-intervention)):
read the queue, decide which applications to make, and resolve anything a stage stopped on.
Submitting applications is outside this repository.

## Failures and recovery

### One unparseable answer blocked one batch, not the run

- **Event (measured).** On 2026-10-08, one of 328 postings came back as output that was not valid
  JSON. In single-turn mode an invalid output is not retried within the run (only transient
  failures are), so that posting failed. Its batch was not checkpointed; the other 34 of 35 batches
  were; the run (328 calls, $12.56) ended with exit code 1 and named the failed batch.
- **Recovery (measured).** Each rerun skipped the 34 checkpointed batches and re-screened the 10
  postings of the unfinished one (10 calls, about $0.32 per rerun). The first rerun hit the same
  failure; the second succeeded, and the day's merge covered all 328 postings. All three runs used
  the same system-prompt hash, so the rules did not change between them.
- **Why it worked.** A batch is checkpointed atomically only after every job in it validates, and a
  failed job blocks only its own batch ([`02_run_screening.py`](../screening/scripts/02_run_screening.py)).
  The cost of recovery is one batch, not one posting.
- **Not recorded.** Why the third attempt parsed; the same input and prompt produced different
  output across runs (**inference**: sampling variation).

### Resume V1 output failing the structural check

- **Event (measured).** One day of resume-stage telemetry (2026-10-10, 3 batches, 35 jobs, the
  earlier agent worker): 32 jobs got a valid Resume V1 on the first attempt. 10 of 44 V1 calls
  failed the stage's structural validator (required names, sections, title; no HTML). Three jobs
  failed both attempts of at least one run; two of them got a valid V1 on a later rerun, one did
  not, so the day ended with a valid V1 for 34 of the 35 jobs.
- **Recovery.** A failed attempt gets one fresh-worker retry; a job that fails both is reported and
  rerun with `--job FOLDER` ([`02_generate_v1.py`](../resume/script/02_generate_v1.py)).
- **Not recorded.** Which check failed. Telemetry stores counters and status only, never model
  output, by design ([`pipeline_telemetry.py`](../resume/script/pipeline_telemetry.py)).

### Re-screening after a rule change, without redoing the day

- **Event (measured).** On 2026-10-01, the targeted re-screen ran twice: once on 75 postings
  (all eligibility terms) and once on 40 (language scope only); 0 failures. Only those postings' results were replaced inside the existing
  checkpoints, and the merge was rebuilt.
- **Implementation.** [`04_rescreen_eligibility.py`](../screening/scripts/04_rescreen_eligibility.py)
  selects postings whose title or description mentions student, co-op, internship, French or
  citizenship terms.
- **Not recorded.** Which rule wording changed that day.

### Hidden context and cost in the earlier agent workers

- **Event (benchmark).** The earlier screening worker ran inside the project directory and loaded
  the project instruction file and memory on every call, and its rules arrived as tool results
  after the posting, so nothing could be cached across postings. The single-turn redesign cut cost
  per posting by 82.3% ([benchmark](benchmarks/screening_single_turn.md#why-b-is-cheaper-where-the-cache-prefix-sits)).
- **Change and check (benchmark).** Single-turn workers run in an empty private directory with
  instruction-file discovery and auto-memory disabled ([`common/single_turn.py`](../common/single_turn.py)).
  The cached prefix was exactly 12,373 tokens on all 99 warm benchmark requests, which shows that
  nothing was added per call ([guardrails](benchmarks/screening_single_turn.md#setup-and-guardrails)).

### A prompt-injection test that took two turns

- **Event (benchmark, not a production incident).** One of three injected postings in the resume
  benchmark made the model take 2 turns although it had no tools. In the pipeline that attempt is
  rejected by the single-turn guardrail (`num_turns` must be 1) and retried
  ([resume benchmark](benchmarks/resume_single_turn.md)).

## Human intervention

| Where | What the person does | Implementation |
|---|---|---|
| Application queue | reviews each queued resume and decides whether to apply; nothing is submitted by this repository | [`05_enqueue_applications.py`](../resume/script/05_enqueue_applications.py) |
| Quality review, manual provider | writes the report for a request file; the next run collects it | [`scoring/manual.py`](../scoring/manual.py) |
| Quality review, unprovable submission | checks the external service, then marks the job not-submitted or adopts a report by hand | [`03_review_resumes.py`](../resume/script/03_review_resumes.py) `--resolve-not-submitted`, `--adopt-report` |
| Provider out of quota | decides what to do; stage 4 refuses the batch until stage 3 is rerun | [`scoring/engine.py`](../scoring/engine.py) |
| Rules | changes rule wording, then re-screens the affected postings | [`04_rescreen_eligibility.py`](../screening/scripts/04_rescreen_eligibility.py) |

How often the manual-recovery paths were needed in practice is not recorded in the published
material.

## Design evolution

| Change | Evidence | Outcome |
|---|---|---|
| Screening: agent worker with `Read` → single-turn, rules embedded | [benchmark](benchmarks/screening_single_turn.md) | adopted; agent mode kept for rollback |
| Concurrent screening: serial warm-up before the pool | [benchmark](benchmarks/screening_single_turn.md#concurrency-and-the-warm-up-decision) | adopted; a cold start with several workers was avoided rather than measured |
| Resume workers: agent → single-turn | [benchmark](benchmarks/resume_single_turn.md) | adopted |
| Title screen: hand-run agent session → callable worker | [check](benchmarks/title_screen_worker.md) | adopted; more permissive than the earlier decisions |
| Two-stage screening gate | [experiment](../experiments/two_stage_gate/README.md) | not adopted: about +31% cost (estimated) |
| Candidate-fact RAG for resumes | [experiment](../experiments/candidate_fact_rag/README.md) | not adopted: cheaper context, higher batch cost under caching, evidence left out |

## Open problems

- **An under-specified rubric rule.** Whether a hard 1–2-year minimum counts as a
  required-experience reject is not defined; borderline postings can flip between runs
  ([screening benchmark](benchmarks/screening_single_turn.md)).
- **No independent labels.** Screening quality is measured against the pipeline's own earlier
  decisions, and reviews in the benchmarks were not done by an independent human panel.
- **Re-run noise.** The production worker agreed with its own earlier decision on 19 / 20
  postings when re-run ([benchmark](benchmarks/screening_single_turn.md#productions-own-re-run-noise-as-a-baseline)).
- **Parallel cold starts** of workers sharing one cached prompt were not evaluated.
- **Resume workers** still call the CLI directly rather than the shared model-call interface
  ([`common/`](../common/README.md)).
