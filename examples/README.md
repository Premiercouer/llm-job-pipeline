# Examples

Fictional input data. **Everything here is invented**: the candidate, employers, schools, contact
details, projects and postings. None of it is historical experiment data or a real person's
material; the real candidate files are kept outside the repository.

```
examples/
  candidate/
    candidate.json             identity: name, contact, experience and education lines, file names
    capability_profile.md      the profile screening judges fit against
    knowledge/
      fabrikam_health_bullet_bank.md    canonical bullets for one employer (FAB-B01 ...)
      northwind_freight_bullet_bank.md  canonical bullets for the other (NWF-B01 ...)
      other_projects_library.md         four projects, one of them framework-only
      skills_master.md                  core skills and an ATS expansion vocabulary
  job_postings/
    greenhouse_sample.json     8 postings in the five-field discovery output schema
```

The layout is the one every stage expects from `candidate_dir`; see
[`common/candidate.py`](../common/candidate.py) for the `candidate.json` schema. To use your own
candidate, copy this folder outside the repository and point `LJP_CANDIDATE_DIR` (or
`config/local.toml`) at it.

## Who uses it

| User | Uses |
|---|---|
| [`demo/run_demo.py`](../demo/run_demo.py) | the candidate and all 8 postings, with the fake model |
| several test suites (screening, resume, discovery) | the candidate as the default `candidate_dir` or as fixtures |
| [`experiments/candidate_fact_rag`](../experiments/candidate_fact_rag/README.md) | the candidate as the fact corpus for its offline demo and tests |
| [`config/example.toml`](../config/example.toml) | the default `candidate_dir` |

In the demo, the fake model accepts five postings and rejects three (required years, language,
student eligibility); that split comes from its fixed rules, not from a real model.

## Example outputs

No generated output is committed here. Two kinds are shown elsewhere and labelled at the source:

- the screening decision for the *Junior Data Scientist* posting in the
  [root README](../README.md#example-one-screening-decision) is described there as real model
  output for this fictional posting;
- everything the [demo](../demo/README.md) produces under `workspace/demo/` comes from the fake model
  ([`tools/fake_claude.py`](../tools/fake_claude.py)) and the mock review provider.
