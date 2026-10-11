# Benchmark: single-turn, tool-less screening worker

Date: 2026-10-07. Outcome: **adopted**. The single-turn worker became the production default, behind
a feature flag (`--worker {agent,single-turn}`) that keeps the old design available for rollback.

**A** = the earlier production design: `claude -p --agent job-screener --tools Read`, run from the
project directory. The worker reads the screening rules and the candidate profile itself with the
`Read` tool. **B** = the current design: no tools, a single turn, and the rules and profile embedded in
the system prompt. The posting (`source_key`, `position`, `jd`) is the user prompt, and the worker runs
in an empty temporary directory with project instructions and memory disabled.

Both arms used the same model (the CLI default), the same effort setting and the same validator. A
and B produce identical result and checkpoint schemas, so runs can switch between them mid-day.

## Summary

| Metric | A: agent worker | B: single-turn | Change (95% CI) |
|---|---|---|---|
| Cost per posting, warm cache | $0.246 | $0.044 | **−82.3%** (−84.5% to −80.2%) |
| Input-side tokens per posting | 42.9k | 15.1k | **−64.8%** (−66.0% to −64.1%) |
| Latency per request | 14.9 s | 12.5 s | **−16.5%** (−26.5% to −7.3%) |
| Turns per request | 3 | 1 | |
| Decision agreement with prior production decisions | | 96 / 100 | Wilson 90.2% – 98.4% |
| Confirmed false rejects among prior ACCEPTs | | 1 / 25 | Wilson 0.7% – 19.5% |
| First-attempt schema / evidence failures | 0 / 21 | 0 / 100 | |

Cost and token figures come from 20 paired postings (bootstrap over pairs, 10,000 resamples). Quality
figures come from a 100-posting stratified sample. Benchmark spend: $9.38 at list price.

## Why B is cheaper: where the cache prefix sits

Most of the difference is a prompt-caching effect. The `Read` tool is not the main cost.

1. **Cache position.** In A, the rules and profile enter the context *after* the posting, as tool
   results, so the prefix differs for every posting and nothing is reused across postings. Every A
   call writes ~27.7k tokens to the cache, cold or warm. In B, the static content comes first, and
   each call writes only the posting itself (~2.7k tokens) and reads the rest (12.4k tokens) from the
   cache.
2. **Extra context.** A loads the project's instruction file, memory and the agent system prompt on
   every call; B loads none of them.
3. **Turns.** A's three turns re-send the growing context; B sends it once.

Cold-start cost: A $0.235, B $0.139. A's cold and warm costs are almost the same, because its prefix
is never reusable. B pays the cache write once and costs ~$0.04 per posting after that.

## Setup and guardrails

Every B request was checked, and the run would have stopped on any violation:

- `num_turns == 1`; no permission denials; no server tool use;
- one model per request and no model change during the run;
- the system prompt hash stays the same for the whole run, and the embedded rule and profile files
  match their sources byte for byte;
- the user prompt contains only the short instruction and the three posting fields;
- input-side tokens ≤ 18k (the observed maximum was 17,561, from a very long posting).

**Hidden-context check:** the cached prefix was exactly 12,373 tokens on all 99 warm requests. A
constant prefix means nothing per-request (instruction files, memory) was being added.

These checks later became the production guardrails in [`common/single_turn.py`](../../common/single_turn.py)
and [`screening/scripts/screening_core.py`](../../screening/scripts/screening_core.py).

## Sample

- **Pool:** valid checkpoints from the two days after the last change to the rules and profile, so
  every historical decision was made under the current rules. After deduplication by key and by
  title + company, the pool held 650 postings from four job sources.
- **Quality sample:** 100 postings, stratified by historical outcome with fixed seed 20261007. The
  hard cases are deliberately oversampled: ACCEPT is 25% of the sample but 5.4% of the pool.

  | Stratum | Sample | Pool |
  |---|---:|---:|
  | ACCEPT | 25 | 35 |
  | REJECT, no hard-reject rule (judged on responsibilities) | 15 | 60 |
  | REJECT: required years of experience | 15 | 314 |
  | REJECT: seniority | 10 | 106 |
  | REJECT: specialised domain experience | 10 | 54 |
  | REJECT: language | 10 | 41 |
  | REJECT: student eligibility | 8 | 22 |
  | REJECT: mandatory qualification | 5 | 8 |
  | UNABLE TO ASSESS | 2 | 10 |

- **Paired subset:** 20 of the 100 postings, covering every stratum. A and B ran on the same day in
  alternating order (AB / BA); the order effect was negligible ($0.252 vs $0.240 for A).

Because the sample is stratified, 96% agreement is not the agreement rate on production traffic.
Reweighted to the pool's composition, it is ~98.3%. Treat that figure as indicative only: each stratum
is small.

## Quality

### Agreement with prior production decisions (n = 100)

| Metric | Result |
|---|---|
| Decision agrees | 96 / 100 |
| Hard-reject hit agrees | 96 / 100 |
| Hard-reject type agrees | 87 / 100 |
| Prior ACCEPTs still accepted | 23 / 25 |
| ACCEPT → REJECT / REJECT → ACCEPT | 2 / 2 |
| Passed the validator on first attempt | 100 / 100 |

Responsibility-level matches on the 36 postings without a hard reject (prior → B): DIRECT 43 → 43,
ADJACENT 77 → 82, LEARNABLE 24 → 26, STRUCTURAL 32 → 27. B does not drift stricter; if anything it is
slightly more lenient.

### Production's own re-run noise as a baseline

Prior decisions are not ground truth, so A was also re-run on the paired subset the same day:

| Comparison (n = 20) | Decision agrees | Type agrees |
|---|---|---|
| A re-run vs. prior | 19 / 20 | 17 / 20 |
| B vs. prior | 18 / 20 | 18 / 20 |
| A vs. B, same day | 19 / 20 | 17 / 20 |

B differs from the historical decisions by about as much as the production worker differs from
itself. In one case the A re-run also broke a priority rule (language before student eligibility),
which B and the historical run both applied correctly.

### All 17 disagreements, reviewed by hand

| Finding | Count | Verdict |
|---|---:|---|
| B worse: an entry-level business-analyst role rejected for gaps the rules treat as learnable | 1 | **The one confirmed false reject** |
| Rule ambiguity: a hard "minimum 2+ years" requirement; B's reject follows the rule's literal text | 1 | Not counted as an error; the rule itself is under-specified (see below) |
| REJECT → ACCEPT on "years preferred" postings; one of them was also accepted by the A re-run | 2 | Defensible boundary cases; at worst a cheap extra application |
| Same decision, different hard-reject label (for example seniority vs. required years when the posting states both); quoted evidence valid either way | 12 | Equivalent |
| Same decision, different `main_gap` label | 1 | Equivalent |

The pattern behind the false reject (new STRUCTURAL gaps, then a flipped decision) did not appear in
any other posting without a hard reject. Even so, 1/25 has a wide interval: the sample can only say
the false-reject rate is probably below ~20%.

**Rule ambiguity found along the way:** the required-experience rule gives examples of 3+ years and
up, but does not say whether a hard 1–2 year minimum counts. Production had been inconsistent on this
before the benchmark. That is a rule decision, separate from the worker architecture, so it was kept
out of this change.

## Cost and tokens (20 pairs, warm cache)

| Per request | A mean / median / P90 | B mean / median / P90 | Change (95% CI) |
|---|---|---|---|
| Cache write tokens | 27,748 / 27,194 / 28,060 | 2,713 / 2,778 / 3,460 | |
| Cache read tokens | 15,129 / 15,144 / 16,004 | 12,373 / 12,373 / 12,373 | |
| **Input-side tokens** | 42,881 / 42,342 / 44,068 | 15,088 / 15,153 / 15,835 | **−64.8%** [−66.0, −64.1] |
| Output tokens | 1,038 / 979 / 1,574 | 969 / 748 / 1,644 | −6.7% [−16.7, +1.6], n.s. |
| Thinking tokens | 332 / 292 / 641 | 436 / 291 / 844 | +31.3% [+12.0, +56.1] |
| Latency (ms) | 14,938 / 15,392 / 19,194 | 12,475 / 11,554 / 17,684 | **−16.5%** [−26.5, −7.3] |
| **Cost (USD)** | 0.2458 / 0.2423 / 0.2532 | 0.0436 / 0.0441 / 0.0574 | **−82.3%** [−84.5, −80.2] |

Cost falls about 80% in every stratum. Latency barely changes on postings that need a full
responsibility analysis (ACCEPT, and REJECT without a hard-reject rule); the latency gain comes from
hard-reject postings, which B finishes quickly.

At the day's volume (304 postings), that is about $75 for A versus $13.50 for B, at list price.

## Concurrency and the warm-up decision

A replay of 79 postings with 3 parallel workers confirmed several things:

- the tool-less worker runs safely from a thread pool;
- the budget guard stops exactly at its reservation without overspending;
- the guardrails still hold under concurrency;
- all 79 requests hit the 12,373-token cache, with zero misses.

Throughput was 2.98× serial.

That replay started with a warm cache, so a cold start with several workers at once was not observed.
In that case each worker would probably write the same system prompt to the cache, at ~$0.10 per extra
write. Production avoids the case: **one posting is screened serially before the pool starts**. It is
a real posting from the queue, and its result counts normally.

## What shipped

- `--worker single-turn` became the default after a smoke test and clean production days, with
  `--worker agent` kept for rollback. The rule files and the candidate profile are still the single
  source of truth: the system prompt is built from them at run start and hashed, and its hash must
  match the benchmarked prompt.
- Production since then: $0.037 per screened posting over 1,890 calls, with 0 retries. This covers
  the runs whose logs were kept.

## Limitations

- Small samples: 20 pairs for cost, 100 postings for quality. Cost intervals are tight; quality
  intervals are wide.
- The reference is the pipeline's own earlier decisions, not independent human labels. Precision
  and recall against blind human labels have not been measured yet.
- The results depend on CLI behaviour (`--system-prompt`, `--tools ""`, the switches that disable
  instruction files). The per-request guardrails catch it if that behaviour changes.
- The benchmark scripts are not in this repository, because they read private production
  checkpoints. The resume-side equivalent ([`eval/`](../../eval/)) shows the same method.
