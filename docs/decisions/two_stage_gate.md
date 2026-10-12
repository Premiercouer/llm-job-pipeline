# Decision: no two-stage screening gate

Date: 2026-10-07. Status: **evaluated, not adopted**. Production stays on the single-stage,
single-turn worker ([benchmark](../benchmarks/screening_single_turn.md)).

Full case study, data and reproduction script: [experiments/two_stage_gate](../../experiments/two_stage_gate/README.md).

**Idea.** A short "hard-reject gate" call ends clear-cut rejects (years, seniority, language,
student status) before the full screen, to lower cost without a single extra false reject.

**Result.**
- Quality: 0 of 35 historical ACCEPTs rejected by the gate; all 55 gate rejects judged correct in a
  case-by-case review performed by Claude (not by a person).
- Cost: every call writes the whole posting to the prompt cache, so every posting the gate passes
  on pays that write twice. Estimated cost per posting on the production mix: $0.0367 → $0.0481
  (+31%); the gate would pay off only if at most ~8% of postings went on to the full screen
  (63% in the sample, measured; 43% on the production mix, reweighted estimate).

**Decision.** Keep the single stage. The reason is the cost structure, not quality.

**Revisit if** a much cheaper gate model is available (≤ ~$0.012 per call to save 20%), the cache
pricing ratio changes, or a deterministic pre-filter passes its own precision study.
