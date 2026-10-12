# Decision: keep the full candidate fact library in the resume prompt

Date: 2026-10-11. Status: **evaluated on a 6-job development run, not adopted**. Resume V1 stays
single-turn with the full knowledge files in a cached system prompt
([benchmark](../benchmarks/resume_single_turn.md)).

Full case study, public implementation, data and reproduction: [experiments/candidate_fact_rag](../../experiments/candidate_fact_rag/README.md).

**Idea.** Retrieve only the candidate facts relevant to the job (BM25 + TF-IDF/LSA, grouped by
employer and project), require every resume claim to cite fact IDs, and check the citations.

**Result (6 dev jobs, no held-out test, no human labels).**
- Context per call −64–68% and latency about 7–8 s lower (measured).
- Cost per generation higher in back-to-back batches: the full-text prompt is identical for every
  job and is served from the prompt cache after the first job, while each retrieved bundle pays a
  new cache write. Full text was cheaper from the 5th job on (estimated).
- In 2 of 6 jobs the project that both comparison arms chose was missing from the retrieved
  context; a dev-tuned configuration recovered one of them.
- The automatic fact audit raised no SEVERE flag for any retrieval or full-text arm (MAJOR counts
  differed); quality differences were not verified by a person.

**Decision.** Keep full text for this batch workload.

**Revisit if** the fact library outgrows a cached prompt, jobs stop being processed in cached
batches, latency matters more than cost, or a retrieval method shows no evidence loss on a frozen,
human-labelled held-out set.
