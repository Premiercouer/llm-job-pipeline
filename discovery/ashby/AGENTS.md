# Ashby Job Pipeline

Stage 01 reads `config/ashby_board_pool.json`; it does not scan or modify the Master, Excluded, or Indeterminate data.

Prepare:

```bash
python3 scripts/01_search_jobs.py
```

The current Codex session reads `instructions/title_screening.md` and
`.runtime/01_title_screen_input.json` only once, writes the complete two-partition result to
`.runtime/01_title_screen_results.json`, and then runs:

```bash
python3 scripts/01_search_jobs.py --phase finalize --ai-invocations 1
```

Title screening does not use keywords in Python, and AI is not called on all jobs before the cheap filter.
If the title results are incomplete or final output validation fails, finalize must fail and must not commit seen state.

The production seen state is `.runtime/ashby_seen_jobs.json`, with the internal identity
`ashby:{board_slug}:{id}`. The final Job Match file strictly uses the five-field job schema, and its
`source_key` equals `apply_url`, the same as Greenhouse.

Tests must pass `--limit-boards`, which automatically switches to an isolated test seen/checkpoint/artifact.
Tests may use `--max-title-jobs 10` to limit the real title sample; truncated eligible jobs are not written to seen.
Do not automatically run the 297-board production run after testing.
