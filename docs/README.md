# Docs

| Folder / file | What it holds |
|---|---|
| [`benchmarks/`](benchmarks/) | evaluations of designs that are **in production** |
| [`decisions/`](decisions/) | short decision records, including designs that were **not adopted** |
| [`diagrams/`](diagrams/) | the architecture diagram used in the root README |
| [`operational_experience.md`](operational_experience.md) | real use: scale, failures and recovery, human intervention, open problems |
| [`../experiments/`](../experiments/README.md) | full case studies of evaluated alternatives, with data, code and reproduction steps |

## Benchmarks (adopted designs)

| Report | Question |
|---|---|
| [screening_single_turn.md](benchmarks/screening_single_turn.md) | single-turn screening worker vs. the earlier tool-using agent: cost, agreement, guardrails, concurrency |
| [resume_single_turn.md](benchmarks/resume_single_turn.md) | single-turn resume workers vs. the earlier agents: cost per job, one-page rate, prompt-injection cases |
| [title_screen_worker.md](benchmarks/title_screen_worker.md) | title-screen worker vs. earlier hand-run decisions |

## Decision records

| Record | Status |
|---|---|
| [two_stage_gate.md](decisions/two_stage_gate.md) | not adopted; case study in [experiments/two_stage_gate](../experiments/two_stage_gate/README.md) |
| [candidate_fact_rag.md](decisions/candidate_fact_rag.md) | not adopted; case study in [experiments/candidate_fact_rag](../experiments/candidate_fact_rag/README.md) |

## Reading the evidence

Reports keep these apart: **measured** (from real calls or run records), **estimated** (derived
from measured values plus assumptions), **automatic checks** (deterministic validators and
heuristics), **Claude review** (case-by-case reading by an LLM) and **human review**. Comparisons
are usually against the pipeline's own earlier outputs, not independent human labels, and samples
are small; each report states its limits. Numbers are maintained in the reports themselves, not
repeated here.
