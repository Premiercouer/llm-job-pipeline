# Tools

Development and publishing helpers. None of them is part of the pipeline's data flow.

| Tool | Purpose |
|---|---|
| [`run_tests.py`](run_tests.py) | runs every offline test suite, each in its own process with a temporary `LJP_WORKSPACE` |
| [`fake_claude.py`](fake_claude.py) | deterministic stand-in for `claude -p --output-format json`, used by tests, CI and the [demo](../demo/README.md) |
| [`check_public_safety.py`](check_public_safety.py) | pre-publish scan for secrets and personal data |

## Run

From the repository root:

```bash
python3 tools/run_tests.py                 # all suites
python3 tools/run_tests.py screening       # suites whose name contains "screening"
python3 tools/check_public_safety.py
python3 tools/check_public_safety.py --denylist ~/private/denylist.txt
```

Suites: `common`, `discovery`, `discovery/greenhouse`, `discovery/ashby`, `screening`, `scoring`,
`resume`, and `experiments/candidate_fact_rag` (its numpy-dependent tests are skipped unless the
optional `rag` extra is installed). Tests use only the standard library's `unittest`.

## The fake CLI

[`fake_claude.py`](fake_claude.py) reads the system-prompt file and the user prompt the way the real
CLI would and answers like a well-behaved single-turn worker: one turn, no tools, a cold first
request and then a constant cached prefix in its usage block, and an output that passes the
stage's validator. Its "judgement" is fixed rules (for example, reject on 5+ years, French or
current-student requirements; copy anchors verbatim from the description). It tests the pipeline's
mechanics, not model quality, and reports the model name `fake-model`.

## The public-safety scan

It scans **every file Git would publish from the current working tree**: tracked files plus
untracked files that are not ignored (`git ls-files --cached --others --exclude-standard`).

| Layer | Checks |
|---|---|
| Generic rules (in the repository, run in CI) | API keys and tokens, private keys, home-directory paths, e-mail addresses other than example / noreply domains, phone numbers outside the fictional 555-01xx range, browser-profile / cookie / application-list / transcript file names, PDFs outside `examples/`, files over 1 MB |
| Private deny-list (outside the repository) | one case-insensitive regex per line (real names, employers, product names), given by `--denylist` or `$LJP_PRIVATE_DENYLIST`; keeping these words in the repository would publish exactly what the scan protects |

Exit code 0 means no finding, 1 means findings (`path:line: rule: excerpt`).

What it does **not** do:

- it does not scan **Git history**: a secret removed from the working tree but present in an
  earlier commit is not found;
- it does not read binary files (images, PDFs) beyond their names and sizes;
- zero findings means none of these patterns matched, not that a file is free of personal data.
  Data ported from private experiments was also checked by hand
  ([example](../experiments/candidate_fact_rag/README.md#8-reproduce)).
