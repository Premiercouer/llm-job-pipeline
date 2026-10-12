# Experiments

Evaluated alternatives to parts of the production pipeline. Each one was implemented or run against
real data, measured, and **not adopted** as the default. The code here is independent of the
pipeline: nothing in `discovery/`, `screening/` or `resume/` imports it, and the pipeline runs
without its optional dependencies.

Each conclusion holds for the configuration, model, pricing and workload it was tested on; it is
not a general verdict on the technique.

| Experiment | Question | Result | Status |
|---|---|---|---|
| [Two-stage screening gate](two_stage_gate/) | Can a short hard-reject call in front of the full screen cut cost without extra false rejects? | No false rejects found in the sample, but about **+31%** cost per posting on the production mix (estimated), because each routed posting is written to the prompt cache twice | not adopted; single stage kept |
| [Candidate-fact RAG](candidate_fact_rag/) | Does retrieving only relevant candidate facts beat sending the whole fact library to the resume generator? | Context −64–68% and ~7–8 s faster on 6 dev jobs, but more expensive in back-to-back batches (the full prompt is cached) and relevant evidence was left out | not adopted; full text kept |

Each case study states what was measured, what was estimated, what only automatic checks or a
Claude review support, and how to reproduce the published numbers. Decision summaries live in
[`docs/decisions/`](../docs/decisions/).
