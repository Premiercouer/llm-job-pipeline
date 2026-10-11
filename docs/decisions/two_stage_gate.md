# Decision: no two-stage screening gate

Date: 2026-10-07. Status: **rejected**. Production stays on the single-stage, single-turn worker
([benchmark](../benchmarks/screening_single_turn.md)).

## Context

Most postings are rejected on a hard rule: required years of experience, seniority, language, student
eligibility. The idea was to stop those cheaply. A short "hard-reject gate" call would reject
clear-cut postings with high confidence, and everything else would go to the full screening worker.
The goal was lower token use and cost **without adding a single false reject on an acceptable job**.

## Design under test

- **Gate:** same model as production, single turn, no tools. Its system prompt has about half the
  size of the full prompt (10.6k characters vs. 26k). It contains gate rules plus 17 candidate facts,
  each traced to a section of the profile, and leaves out the full profile and the responsibility
  rubric.
- **Output:** `REJECT` (must be `HIGH` confidence, with a hard-reject type and an evidence anchor),
  `PASS`, or `UNCERTAIN`. Only `REJECT` + `HIGH` ends a posting; every other outcome, including a
  parse failure, goes to stage 2. Anchors get the same verbatim check as the production validator.
- **Conservative by construction:**
  - 1–2 year requirements, "preferred" or "typically" wording, alternative paths ("or equivalent"),
    licences and designations, and specialised-domain judgements always go to stage 2;
  - a senior-sounding title alone is never a reason to reject;
  - generic language-policy boilerplate is not a language requirement.
- **Stage 2:** the unchanged production worker; its system prompt hash matched production.

## Method

- **Gate sample:** 150 postings, stratified by historical outcome (seed 20261008). The sample
  includes all 35 historical ACCEPTs in the pool, and those ran first: the run would have stopped if
  the gate rejected any of the first 20 with high confidence.
- **End to end:** 30 postings run through both stages.
- **Calls:** 170 model calls in total, 0 retries, $5.68 at list price.
- **Run checks:** every call was asserted to be single-turn, tool-free and on one model. The gate's
  cached prefix stayed at 6,838 tokens throughout, which confirms it never loaded the full profile.
- **Manual review:** 139 cases checked by hand. This covered:
  - all 55 high-confidence rejects, each read in the context around its anchor;
  - all 35 ACCEPTs;
  - every historical hard reject that the gate sent to stage 2;
  - every language case, every case with a 2-year requirement and every title-only seniority case.

## Results

### Quality: the gate was safe

| Metric | Result |
|---|---|
| High-confidence rejects | 55 / 150 (36.7%) |
| Rejects confirmed correct by manual review | **55 / 55** (Wilson 93.5% – 100%) |
| Historical ACCEPTs rejected by the gate | **0 / 35** (upper bound ~10%) |
| Anchors that pass the verbatim check | 100% |
| Output format failures | 1 / 150: prose before the JSON. It went to stage 2 safely |
| Sent to stage 2 | 95 / 150 (63.3%); ~43% when reweighted to the production mix |
| End-to-end decision agreement (n = 30) | 28 / 30. Both differences came from stage 2 re-run noise, not from the gate |

The gate also handled cases a regex pre-filter would get wrong, and sent all of them to stage 2:

- "3–5 years … preferred";
- years listed under "even better if you have, but not necessary";
- "or equivalent" that applies to the degree, not to the years.

### Cost: the gate made it more expensive

| Scenario | Single stage | Two stage | Change |
|---|---|---|---|
| Sample formula: gate + 63% × stage 2 | $0.0436 | $0.0603 | **+38%** |
| Reweighted to the production mix | $0.0367 | $0.0481 | **+31%** |
| End to end, 30 postings (stage-2 cold start excluded) | $0.0420 (counterfactual) | $0.0616 | +47% |
| Latency per posting (production mix) | 12.5 s | ~12.5 s | none; routed postings wait ~7 s longer |

## Why: the per-call cost is dominated by writing the posting

Per-token prices fitted from the measured calls reproduce every reported cost to within $0.00001:

| Per call | Write posting to cache | Read cached rules | Output | Total |
|---|---|---|---|---|
| Gate | **$0.0226 (74%)** | $0.0014 | $0.0065 | $0.0305 |
| Stage 2 | **$0.0225 (48%)** | $0.0025 | $0.0220 | $0.0469 |

- Both stages must read the whole posting, and the posting is written to the cache on every call, at
  40× the cache-read price. Postings that pass the gate pay for that write twice.
- A shorter system prompt only reduces the cache *read*, which was already almost free (~$0.001 per
  call).
- The production worker already finishes hard-reject postings quickly and cheaply (~$0.0335, short
  output). Stopping one of them at the gate saves almost nothing.

**Break-even:** comparing like with like (the postings the gate ends would have cost ~$0.0335 in a
single stage), the gate pays off only if at most ~9% of postings go to stage 2. Even a gate that ended
100% of postings would save only ~9%. The measured share was 43–63%.

## Decision

Do not ship the gate. Quality was not the reason (no false rejects were found). The reason is that a
same-model gate cannot save money under this cost structure at any realistic routing rate. It would
also add a second prompt that must stay in sync with the screening rules, and a second place where
output can fail to parse.

## What would change the decision

- **A cheaper gate model:** the gate would have to cost ≤ ~$0.019 per call to break even, and
  ≤ ~$0.012 (≈ 40% of the current cost) to save 20%. Half of the current gate cost is the
  unavoidable posting write, so only a much cheaper input price could get there. A cheaper model
  running the *single-stage* worker avoids the double write entirely, and would be the better
  experiment to run first.
- **A deterministic pre-filter** for unambiguous patterns ("currently enrolled", "French required")
  costs nothing per call. The near-misses above show its false-reject risk, so it would need its own
  precision study before use.

## Side findings

- One gate rule was worded too loosely ("no 'or equivalent'" should be "no alternative path for the
  *years*"). The model applied the intended meaning anyway.
- Hard-reject *type* can differ between runs when a posting violates two rules (for example student
  status and a language test note). The decision stays the same; only the label differs.
