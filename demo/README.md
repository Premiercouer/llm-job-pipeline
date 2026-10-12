# Offline demo

Runs the real pipeline code end to end on fictional data, with no API key, network or account:
8 fictional postings → screening → merge → Resume V1 → quality review → Resume V2 → one-page PDF →
application queue. It goes through the same [`run_pipeline.py`](../run_pipeline.py) entry point as
production, starting at screening.

## What is real and what is replaced

| Part | In the demo |
|---|---|
| Every stage script, validator, guardrail, checkpoint and handoff | **real code**, run as subprocesses exactly as in production |
| The model | [`tools/fake_claude.py`](../tools/fake_claude.py): a deterministic, rule-based stand-in for `claude -p` that follows the single-turn contract (one turn, no tools, plausible usage) |
| The quality-review provider | `scoring.mock`: offline keyword coverage ([scoring](../scoring/README.md)) |
| The candidate and postings | fictional: [`examples/candidate/`](../examples/candidate/) and [`examples/job_postings/greenhouse_sample.json`](../examples/job_postings/greenhouse_sample.json) |
| Discovery | skipped: the demo writes the sample postings as a discovery output file, because discovery needs the public board APIs |

## Run

From the repository root:

```bash
pip install "pymupdf>=1.24"
python3 demo/run_demo.py                              # workspace: workspace/demo/ (git-ignored, recreated)
python3 demo/run_demo.py --workspace /tmp/ljp-demo    # somewhere else
python3 demo/run_demo.py --keep                       # keep an existing workspace instead of starting fresh
```

Requirements: Python 3.11+, PyMuPDF, and Chrome or Chromium for the PDF step (set `CHROME_BIN` if it
is not found). Without Chrome, stage 4 stops before Resume V2 (it looks for Chrome first), so the
demo ends after screening, Resume V1 and quality review and reports that PDF rendering and
queueing were skipped. A run takes a few seconds.

The demo sets `LJP_WORKSPACE`, `LJP_CANDIDATE_DIR`, `CLAUDE_BIN` (the fake CLI), the worker modes and
`LJP_SCORER=mock` for its own subprocesses only; nothing in your shell or config is changed.

## Output

The end of the output lists each decision and each generated resume:

```text
=== Summary
screened 8 | ACCEPT 5 | REJECT 3 | UNABLE 0
  ACCEPT   Tailspin Analytics — Junior Data Scientist
  REJECT   Adventure Works — Senior Data Engineer (REQUIRED_EXPERIENCE)
  ...
  resume: Tailspin Analytics — Junior Data Scientist | review score 83 | PDF resume/jobs/<batch>/<job>/resume.pdf
```

Artefacts under the workspace:

| Path | Content |
|---|---|
| `screening/input_batches/`, `screening/results/` | batches and per-batch checkpoints |
| `screening/.runtime/<date>/screened_all.json` | every decision with its evidence fields |
| `screening/output/<date>_<HHMM>_jobs.json` | the ACCEPT handoff to resume generation |
| `resume/jobs/<batch>/<job>/` | `job.json` (V1, feedback, V2), `resume.html`, `resume.pdf` |
| `resume/.runtime/` | handoff manifests, review journals, telemetry |
| `resume/application_list.json` | the queue a person would review |

CI runs the same demo and uploads the PDFs ([`.github/workflows/ci.yml`](../.github/workflows/ci.yml)).

## What it shows, and what it does not

It shows the **engineering mechanics** working together: batching and checkpoints, the
single-turn contract and its guardrails, verbatim-anchor validation, merge and deduplication,
handoff manifests between stages, the exactly-once review engine with a provider, HTML → PDF
rendering, the one-page check with the trim loop, output guarding and queueing.

It does **not** show model quality. The fake model decides by fixed rules (for example, reject on
5+ years, French or current-student requirements) and builds resumes from the first bullets of
each bank. Its decisions, resume text and review scores say nothing about how the real model
screens or writes. For real-model behaviour, see the example decision in the
[root README](../README.md#example-one-screening-decision) and the
[benchmarks](../docs/README.md).

## Running for real instead

Real runs need the Claude Code CLI (logged in) or Anthropic API credentials, your own candidate
folder, and they spend money on model calls: see [Running for real](../README.md#running-for-real).
`python3 run_pipeline.py --dry-run` prints the plan without running anything.

A separate offline demo for the evaluated candidate-fact retrieval alternative lives in
[`experiments/candidate_fact_rag/`](../experiments/candidate_fact_rag/README.md#8-reproduce).
