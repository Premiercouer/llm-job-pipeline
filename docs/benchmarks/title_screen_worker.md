# Check: title-screen worker vs. earlier agent-session decisions

Date: 2026-10-11. Script: [`eval/title_screen_agreement.py`](../../eval/title_screen_agreement.py).

Discovery's title screen used to be run by hand: an interactive agent session read the prepared
titles and the rules and wrote the results file. [`discovery/title_screen.py`](../../discovery/title_screen.py)
replaces that step with a callable worker: single-turn, tool-less model calls on chunks of up to 150
titles, the same rule file in the system prompt, an exact-partition check on every chunk, and no
results file unless every title is covered.

This check asks one question before the worker replaces the manual step: does it make roughly the
same keep/drop decisions? The earlier decisions were made by a different model in an agent session,
so they are a reference, not ground truth.

## Data

- Eight production Ashby runs recorded which job IDs the title screen accepted and rejected: 320
  decisions (85 accept, 235 reject).
- 308 titles could be recovered from the latest board snapshot. The other 12 postings had been taken
  down.
- The worker screened all 308 titles in 3 calls with the production model: 0 retries, $0.54, 150 s.

## Results

| | Worker keeps | Worker drops | Total |
|---|---:|---:|---:|
| Earlier decision: keep | 79 | 6 | 85 |
| Earlier decision: drop | 23 | 200 | 223 |

- Agreement: **279 / 308 (90.6%)**.
- The worker is **more permissive overall**: it keeps 102 titles where the earlier run kept 85. For a
  high-recall first filter this is the safer direction, because a posting kept by mistake is still
  read in full by the screening stage.
- The 23 extra keeps are mostly product-management, product-marketing and integration-architect
  titles. The rules call these borderline ("when uncertain, keep").
- Of the 6 earlier keeps the worker drops, 4 fall clearly in the rules' reject list: a store
  assistant manager, a fund-administration manager, a lead architect / principal engineer, and a
  deployment-operations director. 2 are borderline: a vague consulting title and an HR-systems
  analyst.
- What these 6 drops would have cost cannot be measured. They come from a run whose output never
  reached full-JD screening, so it is unknown whether any of them would have been accepted.

## Decision

The worker replaces the manual step as the default. [`run_pipeline.py`](../../run_pipeline.py) runs it
between stage 01 prepare and finalize. An agent session can still write the results file by hand,
because finalize validates the file in the same way whoever wrote it.

## Limitations

- One source (Ashby) and one snapshot; 308 titles.
- The reference is an earlier model's decisions, not human labels.
- The rules were not tuned for this check. Making the worker keep vague titles more consistently
  would be a rule change, and it should be measured the same way.
