# Common

## Purpose

Code shared by the stages: where data lives, who the candidate is, how one model call is made and
what it returns, private working directories for workers, a last check on generated text, and
pseudonymous IDs for logs. Nothing here runs on its own; importing any module has no side effects
beyond reading config.

## Inputs and outputs

| Module | Provides |
|---|---|
| [`paths.py`](paths.py) | `WORKSPACE` and `CANDIDATE_DIR`: environment variable (`LJP_WORKSPACE`, `LJP_CANDIDATE_DIR`), else `config/local.toml` (or the file named by `LJP_CONFIG`), else `workspace/` and `examples/candidate/` |
| [`candidate.py`](candidate.py) | validated `candidate.json`: name, contact, fixed experience and education lines, knowledge-file names; fills the HTML header |
| [`llm.py`](llm.py) | one model call: `backend.complete(system_prompt, user_prompt, timeout) -> Result(text, usage)` |
| [`single_turn.py`](single_turn.py) | system prompts and CLI commands for the single-turn resume workers; the post-call contract check |
| [`run_dirs.py`](run_dirs.py), [`worker_dir.py`](worker_dir.py) | private 0700 run directories under `<workspace>/.runtime/worker_runs/`; staged copies for agent-mode workers |
| [`output_guard.py`](output_guard.py) | rejects foreign links / e-mail addresses, credential-like strings and prompt fragments in generated text |
| [`pseudonym.py`](pseudonym.py) | salted HMAC job IDs for telemetry, so logs do not reveal which companies were targeted |

## The model-call contract (`common.llm`)

Every backend takes a system prompt and a user prompt and returns the text plus a normalized
`Usage` (input, cache-write, cache-read and output tokens, cost, models). Failures map onto exactly
three classes: **transient** (a fresh retry may work), **input** (this input can never succeed) and
**fatal** (stop the run). Callers decide the retry policy; the backends never retry on their own.

| | `ClaudeCliBackend` (`cli`, default) | `AnthropicApiBackend` (`api`, optional `anthropic` extra) |
|---|---|---|
| Call | `claude -p` with no tools, `dontAsk`, no MCP, no slash commands; system prompt in a private 0600 file | Messages API, system prompt as one cached block (1 h TTL), one user message, no tools |
| Isolation | empty private cwd; instruction-file discovery and auto-memory disabled by environment | no local files involved |
| Contract check | after the call: 1 turn, no permission denials, no server tool use, one model | stop reasons: refusal → input error, `max_tokens` → transient, any tool use → fatal |
| Cost | the CLI's own `total_cost_usd` | list-price estimate from the `PRICES` table in `llm.py` |
| Options | model | model, effort, `max_tokens`; server-side refusal fallbacks opt-in (off: a run must use one model) |

A shared interface **reduces** harness differences between backends; it does not make them
identical. The two sides still differ in how cost is reported, in cache-TTL control, in the effort
setting and in what hidden context the CLI adds, so a backend comparison
([`eval/compare_models.py`](../eval/compare_models.py)) is not a pure model comparison.

## Who uses what

| Caller | Model calls go through |
|---|---|
| [`discovery/title_screen.py`](../discovery/title_screen.py) | `common.llm` always (`--backend cli` or `api`) |
| screening, `--backend api` | `common.llm.AnthropicApiBackend`, with screening's own guardrails, budget and validation on top |
| screening, CLI (default) | screening's own subprocess path in [`screening_core.py`](../screening/scripts/screening_core.py), not `common.llm` |
| resume stages 2 and 4, cover letter | the CLI directly, via `single_turn.command()` (single-turn) or `--agent` (rollback); not `common.llm` |
| [`eval/`](../eval/) scripts | `title_screen_agreement.py`: `common.llm`; `compare_models.py`: the screening worker, so either of the two screening paths above |

Usage and cost are recorded by each caller: the title screen prints one JSON summary line,
screening prints a per-run usage report, and resume stages append one counters-only JSONL record
per attempt ([`resume/script/pipeline_telemetry.py`](../resume/script/pipeline_telemetry.py)).

## Design choices

- **Three failure classes, one retry policy.** The SDK's built-in retries are off
  (`max_retries=0`), so the pipeline's own rule (one fresh retry for transient errors) is the only
  one, and an authentication failure stops the run instead of being retried.
- **Workers see nothing but their prompt.** Private run directories are 0700, prompt files 0600,
  cwds empty; directories are removed on exit, on SIGTERM, or swept by the next process after a
  kill.
- **Generated text is checked before it can leave the machine.** Links that appear in the job
  description are rejected too: the description is attacker-controlled, so "it was in the JD" is
  exactly how an injected link would get through.
- **Config is read, never created.** Paths resolve without creating directories, so a mistyped
  workspace fails loudly in the stage that needs it.

## Run

Nothing to run directly. Configure paths once:

```bash
cp config/example.toml config/local.toml      # or set LJP_WORKSPACE / LJP_CANDIDATE_DIR
pip install ".[api]"                          # only for --backend api
```

## Validation and limitations

- Tests: `python3 tools/run_tests.py common` — request shape, error mapping, stop reasons, usage
  and list-price cost, fallbacks off by default, and a CLI round trip against
  [`tools/fake_claude.py`](../tools/fake_claude.py).
- The API backend is tested only against a fake SDK; no real CLI-vs-API comparison has been run.
- `PRICES` is a hand-maintained table (dated in the source). Long-context pricing tiers are not
  modelled.
- Resume workers are not on `common.llm` yet, so they cannot use the API backend
  ([open problems](../docs/operational_experience.md#open-problems)).

## Key files

[`llm.py`](llm.py) · [`single_turn.py`](single_turn.py) · [`paths.py`](paths.py) ·
[`candidate.py`](candidate.py) · [`output_guard.py`](output_guard.py) · [`run_dirs.py`](run_dirs.py)
