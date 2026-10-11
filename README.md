# llm-job-pipeline

LLM-powered document triage and generation pipeline: discover job postings, screen each one
against a candidate profile with grounded, schema-validated LLM decisions, then generate a
tailored one-page resume for every accepted job.

> Work in progress. Full documentation (architecture, design decisions, evaluation) is coming.
> All candidate data in `examples/` is fictional.

| Layer | Path | Status |
|---|---|---|
| Discovery (Greenhouse, Ashby public board APIs) | `discovery/` | migrated |
| Screening (single-turn LLM worker, validation, checkpoints, cross-source dedupe) | `screening/` | migrated |
| Resume generation (V1 → quality review → V2 → PDF → one-page trim) | `resume/` | migrated |
| Resume Quality Review (provider interface, crash-safe state machine, mock / manual providers) | `scoring/` | new |
| Shared config / candidate identity | `common/`, `config/` | migrated |
