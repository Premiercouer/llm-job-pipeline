"""Shared Job Match screening core: deterministic helpers + the single-job screening engine.

Used by 02_run_screening.py (daily global-pool controller), 03_merge_results.py (merge) and
04_rescreen_eligibility.py (targeted re-screen). Importing this module has no side effects:
no argument parsing, no Claude processes, no threads, no file writes.

Contents:
  paths / constants, platform normalization (PLATFORMS), exceptions,
  batch discovery + result paths, ScreeningResult / checkpoint validation,
  Claude CLI discovery + worker command, worker-facing input + prompt,
  blank-JD local resolution (FRENCH_TITLE gate), CLI error classification,
  worker modes (feature flag: "single-turn" = no tools, rules + profile embedded in the system
  prompt by the controller, the default; "agent" = `--agent job-screener` + Read, rollback mode),
  Worker / Budget, serial warm-up helper, screen_job() / screen_with_retry(), runtime control
  shared with worker threads (stop_event, process registry, metrics, log), atomic JSON writes.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import unicodedata
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # screening/: rules, prompts, agent definition
sys.path.insert(0, str(ROOT.parent))  # repository root
from common import paths, run_dirs  # noqa: E402

DATA_ROOT = paths.data_dir("screening")  # batches, checkpoints, runtime, handoff output
BATCH_ROOT = DATA_ROOT / "input_batches"
RESULT_ROOT = DATA_ROOT / "results"
AGENT = "job-screener"
DEFAULT_TIMEOUT = 300
MAX_ATTEMPTS = 2  # first try + 1 retry


# --------------------------------------------------------------------------
# Platform config: how each splitter's raw job maps onto the normalized input
# --------------------------------------------------------------------------

def _normalize_greenhouse(job):
    return {
        "source_key": job.get("source_key"),  # upstream sets it to apply_url
        "platform": "greenhouse",
        "date": None,  # greenhouse records carry no per-job date
        "position": job.get("position"),
        "company": job.get("company"),
        "location": None,  # not present in greenhouse schema
        "apply_url": job.get("apply_url"),
        "jd": job.get("jd"),
        "experience_levels": job.get("experience_levels"),
        "min_experience_years": job.get("min_experience_years"),
    }


def _normalize_ashby(job):
    return {
        "source_key": job.get("source_key"),  # upstream sets it to apply_url
        "platform": "ashby",
        "date": None,  # ashby records carry no per-job date
        "position": job.get("position"),
        "company": job.get("company"),
        "location": None,  # not present in ashby schema
        "apply_url": job.get("apply_url"),
        "jd": job.get("jd"),
        "experience_levels": job.get("experience_levels"),
        "min_experience_years": job.get("min_experience_years"),
    }


PLATFORMS = {
    "greenhouse": {"label": "Greenhouse", "normalize": _normalize_greenhouse},
    "ashby": {"label": "Ashby", "normalize": _normalize_ashby},
}


class PipelineError(Exception):
    """Fail-closed error: stop processing and report.

    As a worker failure: `retryable` says whether a fresh worker may try the same job again.
    """
    retryable = True


class JobFatalError(PipelineError):
    """This job's input itself makes the worker fail (e.g. prompt too long): retrying is pointless."""
    retryable = False


class StopRequested(PipelineError):
    """The run is stopping (Ctrl+C / global fatal): this job was not started."""
    retryable = False


class NonRetryableJobError(PipelineError):
    """single-turn: this job's output failed (unparsable / schema / anchor); only transient
    network / API / timeout failures are retried in that mode."""
    retryable = False


class BudgetExceeded(StopRequested):
    """--max-budget-usd reached: this job was not started."""


class GlobalFatalError(PipelineError):
    """Environment failure unrelated to the job (auth, missing agent, unusable CLI).

    Every remaining worker would fail the same way, so the controller stops the whole run.
    """
    retryable = False


# --------------------------------------------------------------------------
# Batch discovery
# --------------------------------------------------------------------------

def batch_dir(platform):
    return BATCH_ROOT / platform


def result_dir(platform, day):
    return RESULT_ROOT / platform / day


def result_path(platform, day, batch_index):
    return result_dir(platform, day) / f"result_{batch_index:03d}.json"


def load_batches(platform, day):
    """Return [(path, batch_dict, [normalized_job, ...]), ...] in batch order.

    Validates the splitter output; raises PipelineError on anything inconsistent.
    """
    pattern = re.compile(rf"^{re.escape(day)}_{platform}_batch_(\d{{3}})\.json$")
    files = sorted(p for p in batch_dir(platform).glob(f"{day}_{platform}_batch_*.json")
                   if pattern.match(p.name))
    if not files:
        return []

    batches, seen_keys = [], set()
    for expected_index, path in enumerate(files, 1):
        file_index = int(pattern.match(path.name).group(1))
        try:
            batch = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            raise PipelineError(f"{path.name}: unreadable batch ({e})")

        problems = []
        if batch.get("platform") != platform:
            problems.append(f"platform={batch.get('platform')!r}")
        if batch.get("date") != day:
            problems.append(f"date={batch.get('date')!r}")
        if file_index != expected_index or batch.get("batch_index") != file_index:
            problems.append(f"batch_index={batch.get('batch_index')!r} (expected {expected_index})")
        if batch.get("batch_count") != len(files):
            problems.append(f"batch_count={batch.get('batch_count')!r} but {len(files)} files")
        jobs = batch.get("jobs")
        if not isinstance(jobs, list) or not jobs or batch.get("job_count") != len(jobs):
            problems.append("jobs / job_count mismatch")
        if problems:
            raise PipelineError(f"{platform} {path.name}: invalid batch: {'; '.join(problems)}")

        normalized = [PLATFORMS[platform]["normalize"](j) for j in jobs]
        for n in normalized:
            key = n["source_key"]
            if not isinstance(key, str) or not key:
                raise PipelineError(f"{platform} {path.name}: job without source_key")
            if key in seen_keys:
                raise PipelineError(f"{platform} {path.name}: duplicate source_key {key}")
            seen_keys.add(key)
        batches.append((path, batch, normalized))
    return batches


# --------------------------------------------------------------------------
# ScreeningResult validation (contract from instructions/screen_job.md)
# --------------------------------------------------------------------------

DECISIONS = {"ACCEPT", "REJECT", "UNABLE TO ASSESS"}
HARD_REJECT_TYPES = {"SENIORITY", "REQUIRED_EXPERIENCE", "SPECIALIZED_EXPERIENCE",
                     "STUDENT_ELIGIBILITY", "LANGUAGE", "MANDATORY_QUALIFICATION"}
MATCH_TYPES = {"DIRECT", "ADJACENT", "LEARNABLE", "STRUCTURAL"}
GAP_TYPES = {"NONE", "ADJACENT", "LEARNABLE", "STRUCTURAL"}
RESULT_KEYS = {"source_key", "hard_reject", "core_responsibilities",
               "anchor_direct_match", "main_gap", "decision", "reason"}

# Typographic variants mapped to ASCII so anchors survive quote/dash differences.
_CHAR_MAP = str.maketrans({
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"',
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "−": "-",
    "•": " ", "·": " ",
})


def normalize_text(s):
    s = unicodedata.normalize("NFKC", s).translate(_CHAR_MAP)
    return re.sub(r"\s+", " ", s).strip().casefold()


def anchor_in_jd(anchor, jd_norm):
    # Tolerate quotes/ellipsis wrapped around the anchor; the body must be verbatim.
    core = normalize_text(anchor).strip(" \"'.…")
    return bool(core) and core in jd_norm


def _is_nonempty_str(v):
    return isinstance(v, str) and v.strip() != ""


def validate_result(result, job):
    """Return None if valid, else a short failure reason."""
    if not isinstance(result, dict):
        return "output is not a JSON object"
    keys = set(result)
    if keys != RESULT_KEYS:
        missing, extra = RESULT_KEYS - keys, keys - RESULT_KEYS
        return f"bad keys (missing={sorted(missing)}, extra={sorted(extra)})"
    if result["source_key"] != job["source_key"]:
        return f"source_key mismatch: {result['source_key']!r}"

    decision = result["decision"]
    if decision not in DECISIONS:
        return f"invalid decision {decision!r}"
    if not _is_nonempty_str(result["reason"]):
        return "reason missing"

    jd_norm = normalize_text(job["jd"] or "")

    hr = result["hard_reject"]
    if not isinstance(hr, dict) or set(hr) != {"hit", "type", "jd_anchor"} \
            or not isinstance(hr["hit"], bool):
        return "hard_reject schema invalid"
    if hr["hit"]:
        if hr["type"] not in HARD_REJECT_TYPES:
            return f"invalid hard_reject.type {hr['type']!r}"
        if not _is_nonempty_str(hr["jd_anchor"]):
            return "hard_reject.jd_anchor missing"
        # Sole exception (screen_job.md §3 LANGUAGE): a French job title may anchor a LANGUAGE reject.
        title_ok = hr["type"] == "LANGUAGE" and anchor_in_jd(hr["jd_anchor"], normalize_text(job["position"] or ""))
        if not anchor_in_jd(hr["jd_anchor"], jd_norm) and not title_ok:
            return f"hard_reject.jd_anchor not found in JD: {hr['jd_anchor'][:80]!r}"
        if decision != "REJECT":
            return "hard_reject.hit=true but decision is not REJECT"
    elif hr["type"] is not None or hr["jd_anchor"] is not None:
        return "hard_reject.hit=false requires type=null and jd_anchor=null"

    crs = result["core_responsibilities"]
    if not isinstance(crs, list) or len(crs) > 5:
        return "core_responsibilities must be a list of at most 5"
    if not hr["hit"] and decision != "UNABLE TO ASSESS" and len(crs) < 3:
        return f"core_responsibilities needs 3-5 items, got {len(crs)}"
    for i, cr in enumerate(crs, 1):
        if not isinstance(cr, dict) or set(cr) != {"responsibility", "jd_anchor", "match"}:
            return f"core_responsibilities[{i}] schema invalid"
        if not _is_nonempty_str(cr["responsibility"]) or not _is_nonempty_str(cr["jd_anchor"]):
            return f"core_responsibilities[{i}] empty field"
        if cr["match"] not in MATCH_TYPES:
            return f"core_responsibilities[{i}] invalid match {cr['match']!r}"
        if not anchor_in_jd(cr["jd_anchor"], jd_norm):
            return f"core_responsibilities[{i}].jd_anchor not found in JD: {cr['jd_anchor'][:80]!r}"

    adm = result["anchor_direct_match"]
    if adm is not None and not _is_nonempty_str(adm):
        return "anchor_direct_match must be a non-empty string or null"
    has_direct = any(cr["match"] == "DIRECT" for cr in crs)
    if has_direct and adm is None:
        return "core_responsibilities has DIRECT but anchor_direct_match is null"
    if not has_direct and adm is not None:
        return "anchor_direct_match is set but no core responsibility is DIRECT"

    gap = result["main_gap"]
    if not isinstance(gap, dict) or set(gap) != {"type", "description"}:
        return "main_gap schema invalid"
    if gap["type"] not in GAP_TYPES:
        return f"invalid main_gap.type {gap['type']!r}"
    if gap["description"] is not None and not isinstance(gap["description"], str):
        return "main_gap.description must be a string or null"
    if gap["type"] != "NONE" and not _is_nonempty_str(gap["description"]):
        return "main_gap.description missing"
    return None


def validate_checkpoint(path, platform, batch_index, jobs):
    """Return None if an existing result file is a complete, valid checkpoint."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return f"unreadable ({e})"
    if not isinstance(data, dict):
        return "not a JSON object"
    if data.get("platform") != platform:
        return f"platform={data.get('platform')!r}"
    if data.get("batch_index") != batch_index:
        return f"batch_index={data.get('batch_index')!r}"
    if data.get("job_count") != len(jobs):
        return f"job_count={data.get('job_count')!r}, batch has {len(jobs)}"
    results = data.get("results")
    if not isinstance(results, list) or len(results) != len(jobs):
        return "result count does not match batch"
    got = [r.get("source_key") if isinstance(r, dict) else None for r in results]
    if Counter(got) != Counter(j["source_key"] for j in jobs):
        return "source_key set differs from batch"
    by_key = {j["source_key"]: j for j in jobs}
    for r in results:
        err = validate_result(r, by_key[r["source_key"]])
        if err:
            return f"{r['source_key']}: {err}"
    return None


# --------------------------------------------------------------------------
# Worker invocation
# --------------------------------------------------------------------------

def find_claude():
    """Locate the Claude Code CLI: $CLAUDE_BIN, then PATH, then the desktop-app bundle."""
    explicit = os.environ.get("CLAUDE_BIN")
    if explicit:
        if not os.access(explicit, os.X_OK):
            raise PipelineError(f"CLAUDE_BIN is not executable: {explicit}")
        return explicit
    on_path = shutil.which("claude")
    if on_path:
        return on_path
    bundle_root = Path.home() / "Library/Application Support/Claude/claude-code"
    # Layout is <version>/claude.app/... or, in newer builds, <version>/<build-hash>/claude.app/...
    bundled = [p for pattern in ("*/claude.app/Contents/MacOS/claude", "*/*/claude.app/Contents/MacOS/claude")
               for p in bundle_root.glob(pattern) if os.access(p, os.X_OK)]

    def version_key(p):
        # <bundle_root>/<version>/...; compare leading integers of each dotted part
        # ("2.1.284" -> (2, 1, 284), "2.2.0-beta" -> (2, 2, 0)); non-numeric parts count as 0.
        version = p.relative_to(bundle_root).parts[0]
        return tuple(int(m.group()) if (m := re.match(r"\d+", part)) else 0
                     for part in version.split("."))

    if bundled:
        return str(max(bundled, key=version_key))
    raise PipelineError("Claude Code CLI not found; set CLAUDE_BIN to its path")


def worker_command(claude_bin, model=None):
    cmd = [
        claude_bin, "-p",
        "--agent", AGENT,
        "--output-format", "json",       # envelope with is_error + result text
        "--no-session-persistence",      # nothing saved, nothing resumable
        "--strict-mcp-config",           # no MCP servers in the worker
        "--disable-slash-commands",
        "--tools", "Read",
        "--allowedTools", "Read",
        "--permission-mode", "dontAsk",  # anything not pre-allowed is denied, never prompts
    ]
    if model:
        cmd += ["--model", model]
    return cmd


# The worker sees only what screen_job.md lets it judge on: the JD, the title (French-title
# LANGUAGE rule) and the source_key it must echo. Other metadata stays controller-side.
WORKER_FIELDS = ("source_key", "position", "jd")


def worker_input(job):
    return {k: job[k] for k in WORKER_FIELDS}


def build_prompt(job):
    return (
        "请严格按照 instructions/screen_job.md 筛选下面这 1 个 normalized job，"
        "只返回 1 个 ScreeningResult JSON object，不要输出任何其他内容。\n\n"
        + json.dumps(worker_input(job), ensure_ascii=False, indent=2)
    )


# --------------------------------------------------------------------------
# Worker modes (feature flag) and the single-turn worker
# --------------------------------------------------------------------------
# agent       = `claude -p --agent job-screener` with Read; the worker reads the rules itself (3 turns).
# single-turn = no --agent, no tools; the controller embeds screen_job.md + the profile in the system
#               prompt, the job is the user prompt, one model request. Reproduces the environment
#               benchmarked in .runtime/benchmark_single_turn/ (benchmark_report.md).

WORKER_AGENT = "agent"
WORKER_SINGLE_TURN = "single-turn"
WORKER_MODES = (WORKER_AGENT, WORKER_SINGLE_TURN)
WORKER_ENV = "JOB_MATCH_WORKER"  # default worker mode when --worker is not given
DEFAULT_WORKER = WORKER_SINGLE_TURN

RULES_FILE = ROOT / "instructions/screen_job.md"
PROFILE_FILE = paths.CANDIDATE_DIR / "capability_profile.md"
AGENT_FILE = ROOT / ".claude/agents" / f"{AGENT}.md"
SINGLE_TURN_ROLE_FILE = ROOT / "instructions/single_turn_worker.md"
SINGLE_TURN_INSTRUCTION = "请按 SCREENING_RULES 筛选下面这 1 个 normalized job，只返回 1 个 ScreeningResult JSON object。"
# Run from an empty dir outside the project with CLAUDE.md discovery and auto-memory off,
# so the request carries only the system prompt + the job.
SINGLE_TURN_ENV = {"CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1", "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}

# Unexpected-context bounds (single-turn). Benchmark 2026-10-07: system prompt 0.47 tokens/char,
# job ~0.45 tokens/char + <= ~760 tokens of fixed overhead. Hidden context such as CLAUDE.md (~7k
# tokens) exceeds these; normal jobs stay well inside.
SYSTEM_TOKENS_PER_CHAR, SYSTEM_TOKENS_MARGIN = 0.55, 1500
JOB_TOKENS_PER_CHAR, JOB_TOKENS_MARGIN = 0.6, 2000

COST_GUESS_USD = {WORKER_AGENT: 0.35, WORKER_SINGLE_TURN: 0.15}  # budget reservation before any cost is seen


def resolve_worker_mode(cli_value=None):
    mode = cli_value or os.environ.get(WORKER_ENV) or DEFAULT_WORKER
    if mode not in WORKER_MODES:
        raise PipelineError(f"unknown worker mode {mode!r} (choose from {', '.join(WORKER_MODES)})")
    return mode


def display_path(path):
    """Path for log lines: relative to the workspace or repository when possible."""
    for base in (paths.WORKSPACE, paths.REPO_ROOT):
        try:
            return str(Path(path).relative_to(base))
        except ValueError:
            pass
    return str(path)


def make_agent_run_dir(run_dir):
    """agent mode: the worker Reads `instructions/screen_job.md` and `candidate_capability_profile.md`
    relative to its cwd and loads `.claude/agents/job-screener.md`; stage exact copies in an empty temp
    dir so the candidate profile can live outside the repository (source hashes are still checked
    against the originals)."""
    run_dir = Path(run_dir)
    (run_dir / "instructions").mkdir()
    (run_dir / ".claude" / "agents").mkdir(parents=True)
    shutil.copyfile(RULES_FILE, run_dir / "instructions" / "screen_job.md")
    shutil.copyfile(PROFILE_FILE, run_dir / "candidate_capability_profile.md")
    shutil.copyfile(AGENT_FILE, run_dir / ".claude" / "agents" / f"{AGENT}.md")
    return str(run_dir)


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def source_hashes():
    return {"screen_job_sha256": sha256_text(RULES_FILE.read_text(encoding="utf-8")),
            "candidate_profile_sha256": sha256_text(PROFILE_FILE.read_text(encoding="utf-8"))}


def build_single_turn_system_prompt():
    """Role + full screen_job.md + full profile, in this order (identical to the benchmark prompt)."""
    role = SINGLE_TURN_ROLE_FILE.read_text(encoding="utf-8").rstrip("\n")
    rules = RULES_FILE.read_text(encoding="utf-8")
    profile = PROFILE_FILE.read_text(encoding="utf-8")
    return (f"{role}\n\n<SCREENING_RULES>\n{rules}\n</SCREENING_RULES>\n\n"
            f"<CANDIDATE_CAPABILITY_PROFILE>\n{profile}\n</CANDIDATE_CAPABILITY_PROFILE>\n")


def embedded_source_hashes(system_prompt):
    rules = re.search(r"<SCREENING_RULES>\n(.*)\n</SCREENING_RULES>", system_prompt, re.S)
    profile = re.search(r"<CANDIDATE_CAPABILITY_PROFILE>\n(.*)\n</CANDIDATE_CAPABILITY_PROFILE>", system_prompt, re.S)
    return {"screen_job_sha256": sha256_text(rules.group(1)) if rules else None,
            "candidate_profile_sha256": sha256_text(profile.group(1)) if profile else None}


def single_turn_command(claude_bin, system_prompt_file, model=None):
    """The system prompt (rules + full candidate profile) goes in a 0600 file, never on the
    command line, so it does not show up in process listings or crash diagnostics."""
    cmd = [
        claude_bin, "-p",
        "--output-format", "json",
        "--no-session-persistence",
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--tools", "",                   # no tools at all
        "--permission-mode", "dontAsk",
        "--system-prompt-file", str(system_prompt_file),
    ]
    if model:
        cmd += ["--model", model]
    return cmd


def build_single_turn_prompt(job):
    return SINGLE_TURN_INSTRUCTION + "\n\n" + json.dumps(worker_input(job), ensure_ascii=False, indent=2)


def claude_cli_version(claude_bin):
    try:
        p = subprocess.run([claude_bin, "--version"], capture_output=True, text=True, timeout=60)
        return p.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


class Budget:
    """Thread-safe spend cap: every launch reserves an estimate until its real cost is known.

    When the next launch would exceed the cap, `stop` is set (and stop_event, so no other thread
    launches); running workers finish and are recorded, not-yet-started jobs raise BudgetExceeded.
    """

    def __init__(self, limit_usd, mode):
        self.limit = limit_usd
        self.spent = 0.0
        self.reserved = 0.0
        self.estimate = COST_GUESS_USD[mode]
        self.stop = None
        self._lock = threading.Lock()

    def reserve(self):
        with self._lock:
            if self.stop is None and self.spent + self.reserved + self.estimate > self.limit:
                self.stop = (f"budget limit ${self.limit:.2f} reached (spent ${self.spent:.4f}, "
                             f"in flight ${self.reserved:.2f}, next ~${self.estimate:.2f})")
            if self.stop:
                stop_event.set()
                raise BudgetExceeded(self.stop)
            self.reserved += self.estimate
            return self.estimate

    def settle(self, reserved, cost):
        """cost None (timeout / no envelope) counts the reservation as spent."""
        with self._lock:
            self.reserved -= reserved
            self.spent += reserved if cost is None else cost
            if cost:
                self.estimate = max(cost, self.estimate * 0.5)


class Worker:
    """How to launch one fresh worker for one job, in the selected mode. Use as a context manager:
    the temp cwd (both modes) is removed on exit (after every worker has finished)."""

    def __init__(self, mode, claude_bin, model=None, budget_usd=None):
        self.mode = mode
        self.model = model
        self.claude_bin = claude_bin
        self.cli_version = claude_cli_version(claude_bin)
        self.sources = source_hashes()
        self.budget = Budget(budget_usd, mode) if budget_usd is not None else None
        self.models_seen = set()
        self.prefix_tokens = None  # single-turn: smallest cached prefix seen (= the system prompt)
        self.prefix_slack = 0      # largest job-prompt bound seen (a repeat may cache that much more)
        self._lock = threading.Lock()
        self.cwd = self.run_dir = self.system_prompt_file = None
        if mode == WORKER_SINGLE_TURN:
            self.system_prompt = build_single_turn_system_prompt()
            self.system_prompt_sha256 = sha256_text(self.system_prompt)
            if embedded_source_hashes(self.system_prompt) != self.sources:
                raise GlobalFatalError("single-turn system prompt does not embed the current source files")
            self.run_dir = run_dirs.new_run_dir("screening")
            self.system_prompt_file = self.run_dir / "system_prompt.md"
            run_dirs.write_private(self.system_prompt_file, self.system_prompt)
            self.cwd = str(self.run_dir / "cwd")
            os.mkdir(self.cwd, 0o700)  # empty: the worker sees no files at all
            self.cmd = single_turn_command(claude_bin, self.system_prompt_file, model)
            self.env_extra = dict(SINGLE_TURN_ENV)
        else:
            self.system_prompt = self.system_prompt_sha256 = None
            self.cmd = worker_command(claude_bin, model)
            self.run_dir = run_dirs.new_run_dir("screening-agent")
            self.cwd = make_agent_run_dir(self.run_dir)
            self.env_extra = {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        if self.run_dir:
            run_dirs.remove(self.run_dir)
            self.run_dir = self.cwd = None

    def run_cwd(self):
        return self.cwd

    def prompt(self, job):
        return build_single_turn_prompt(job) if self.mode == WORKER_SINGLE_TURN else build_prompt(job)

    def manifest(self):
        """Recorded at the top of every checkpoint written this run (ignored by validation / 03)."""
        with self._lock:
            models = sorted(self.models_seen)
        return {"worker_mode": self.mode,
                "effective_model": models[0] if len(models) == 1 else (models or None),
                "system_prompt_sha256": self.system_prompt_sha256,
                "screen_job_sha256": self.sources["screen_job_sha256"],
                "candidate_profile_sha256": self.sources["candidate_profile_sha256"],
                "claude_cli_version": self.cli_version}

    def describe(self):
        sp = f", system prompt sha256 {self.system_prompt_sha256[:12]}" if self.system_prompt_sha256 else ""
        return f"worker: {self.mode}{sp}, cli {self.cli_version or '?'}"

    def pre_check(self, job, prompt):
        """single-turn, before launch: prompt hash / embedded sources / user prompt shape. Fatal if off."""
        if self.mode != WORKER_SINGLE_TURN:
            return
        try:
            on_disk = self.system_prompt_file.read_text(encoding="utf-8")
        except OSError:
            on_disk = None
        if self.cmd[self.cmd.index("--system-prompt-file") + 1] != str(self.system_prompt_file) \
                or on_disk is None or sha256_text(on_disk) != self.system_prompt_sha256 \
                or sha256_text(self.system_prompt) != self.system_prompt_sha256:
            raise GlobalFatalError("single-turn system prompt hash changed during the run")
        if source_hashes() != self.sources:
            raise GlobalFatalError("screen_job.md / candidate profile changed during the run "
                                   "(system prompt no longer matches its sources)")
        head, sep, body = prompt.partition("\n\n")
        try:
            payload = json.loads(body) if sep else None
        except json.JSONDecodeError:
            payload = None
        if head != SINGLE_TURN_INSTRUCTION or not isinstance(payload, dict) \
                or list(payload) != list(WORKER_FIELDS) or any(payload[k] != job[k] for k in WORKER_FIELDS):
            raise GlobalFatalError("single-turn user prompt is not exactly instruction + source_key/position/jd")

    def post_check(self, envelope, prompt):
        """After a run with an envelope. Returns per-request usage; single-turn violations are fatal.

        Fatal (single-turn): tool use (num_turns != 1, permission denials, server tool use), more than
        one / a changed model or a fallback, missing usage fields, unexpected context.
        """
        usage = envelope.get("usage") if isinstance(envelope.get("usage"), dict) else {}
        model_usage = envelope.get("modelUsage") if isinstance(envelope.get("modelUsage"), dict) else {}
        models = sorted(model_usage)
        u = {k: usage.get(k) for k in ("input_tokens", "cache_creation_input_tokens",
                                       "cache_read_input_tokens", "output_tokens")}
        u["cost_usd"] = envelope.get("total_cost_usd")
        u["models"] = models
        with self._lock:
            self.models_seen.update(models)
        if self.mode != WORKER_SINGLE_TURN:
            return u

        problems = []
        turns, denials = envelope.get("num_turns"), envelope.get("permission_denials")
        success = not envelope.get("is_error") and envelope.get("subtype") == "success"
        # More than one turn means a tool call happened; a successful request must be exactly one turn.
        if isinstance(turns, int) and (turns > 1 or (success and turns != 1)):
            problems.append(f"num_turns={turns} (must be 1)")
        if denials:
            problems.append(f"permission_denials not empty: {str(denials)[:200]}")
        stu = usage.get("server_tool_use")
        if isinstance(stu, dict) and any(v for v in stu.values() if isinstance(v, (int, float))):
            problems.append(f"server tool use: {stu}")
        if success:  # transient API errors may legitimately lack usage; those are retried instead
            missing = [k for k, v in u.items() if k not in ("models",) and not isinstance(v, (int, float))]
            if turns is None:
                missing.append("num_turns")
            if not models:
                missing.append("modelUsage")
            if missing:
                problems.append(f"envelope/usage fields missing: {missing}")
        for name, mu in model_usage.items():
            canon = (mu or {}).get("canonicalModel") if isinstance(mu, dict) else None
            if canon not in (None, name):
                problems.append(f"model {name} reports canonicalModel {canon}")
        with self._lock:
            if len(models) > 1:
                problems.append(f"multiple models in one request: {models}")
            if len(self.models_seen) > 1:
                problems.append(f"model changed during the run (fallback?): {sorted(self.models_seen)}")
            if success and not problems:
                problems += self._context_problems(u, prompt)
        if problems:
            raise GlobalFatalError("single-turn guardrail: " + "; ".join(problems))
        return u

    def _context_problems(self, u, prompt):
        """Unexpected context: the cached prefix must be the system prompt, plus at most this job's
        own prompt (an identical request repeated within the cache TTL, e.g. a retry, also reads
        its job text from cache); the uncached part must be explainable by this job's user prompt.
        The run's system-prompt prefix is the smallest cache read seen. Caller holds self._lock."""
        sys_cap = SYSTEM_TOKENS_PER_CHAR * len(self.system_prompt) + SYSTEM_TOKENS_MARGIN
        job_cap = JOB_TOKENS_PER_CHAR * len(prompt) + JOB_TOKENS_MARGIN
        inp, cw, cr = u["input_tokens"], u["cache_creation_input_tokens"], u["cache_read_input_tokens"]
        if cr:
            if cr > sys_cap + job_cap:
                return [f"cached prefix {cr} tokens > system prompt + job bound {sys_cap + job_cap:.0f}"]
            if self.prefix_tokens is not None:
                if cr - self.prefix_tokens > job_cap:
                    return [f"cached prefix {cr} tokens exceeds this run's {self.prefix_tokens} by more "
                            f"than this job's prompt ({job_cap:.0f})"]
                if self.prefix_tokens - cr > self.prefix_slack:
                    return [f"cached prefix {cr} tokens is smaller than this run's {self.prefix_tokens} by "
                            f"more than a job prompt ({self.prefix_slack:.0f})"]
            if self.prefix_tokens is None or cr < self.prefix_tokens:
                self.prefix_tokens = cr      # smaller read = closer to the bare system prompt
            self.prefix_slack = max(getattr(self, "prefix_slack", 0), job_cap)
            uncached = inp + cw
        elif self.prefix_tokens is not None:  # cache expired mid-run: prefix re-written
            uncached = inp + cw - self.prefix_tokens
        else:  # cold first request: system prompt + job both written
            if inp + cw > sys_cap + job_cap:
                return [f"cold request {inp + cw} tokens > bound {sys_cap + job_cap:.0f}"]
            return []
        if uncached > job_cap:
            return [f"uncached input {uncached} tokens > bound {job_cap:.0f} for a {len(prompt)}-char job"]
        return []


# Titles that may be French (screen_job.md §3 LANGUAGE: a French title alone is a hard reject).
# Recall-oriented: accented letters or common French role words ("Analyste", "Conseiller(ère)", ...),
# so it also matches some English titles ("Montréal", "Agent"). Used to pick jobs for 04's
# eligibility re-screen, and to keep blank-JD jobs with such titles going to the worker.
FRENCH_TITLE = re.compile(
    r"[éèêàâçôûîœ]|\b(analyste|conseill[eè]re?|gestionnaire|adjointe?|directeur|directrice|responsable"
    r"|technicien(ne)?|commis|coordonnat(eur|rice)|d[ée]veloppeu(r|se)|agente?|charg[ée]e?|stagiaire)\b", re.I)


def is_blank_jd(job):
    """True only when there is certainly no JD text: null or whitespace-only (jd is str or null)."""
    jd = job["jd"]
    return jd is None or (isinstance(jd, str) and not jd.strip())


def resolves_locally(job):
    """Blank JD and a title that cannot be French: the outcome is certainly UNABLE TO ASSESS.

    A blank JD under a possibly-French title still goes to the worker, which applies the
    LANGUAGE rule (title as jd_anchor) — a local UNABLE there could contradict screen_job.md.
    """
    return is_blank_jd(job) and not FRENCH_TITLE.search(job["position"] or "")


def local_result(job):
    """UNABLE TO ASSESS without starting Claude when resolves_locally(job); else None."""
    if not resolves_locally(job):
        return None
    result = {
        "source_key": job["source_key"],
        "hard_reject": {"hit": False, "type": None, "jd_anchor": None},
        "core_responsibilities": [],
        "anchor_direct_match": None,
        "main_gap": {"type": "NONE", "description": "Not assessed: the JD is missing or empty."},
        "decision": "UNABLE TO ASSESS",
        "reason": "The JD is missing or empty, so the job cannot be assessed.",
    }
    err = validate_result(result, job)
    if err:
        raise PipelineError(f"local blank-JD result invalid: {err}")
    return result


# Failures the Claude CLI reports before any screening happens, identical for every job.
# Matched only against the CLI's own messages / structured fields (observed with CLI 2.1.286).
GLOBAL_FATAL_MESSAGES = (
    "Not logged in",      # envelope result "Not logged in · Please run /login" (no credentials)
)
# stderr, empty stdout: "--agent 'job-screener' not found. Available agents: ..."
AGENT_NOT_FOUND = re.compile(r"^--agent '[^']*' not found")
JOB_FATAL_MESSAGES = (
    "Prompt is too long",  # this job's input exceeds the context window; a retry sends the same input
)


def load_envelope(stdout):
    try:
        envelope = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return None
    return envelope if isinstance(envelope, dict) else None


def classify_cli_failure(envelope, stderr):
    """Raise GlobalFatalError / JobFatalError for reliably recognised CLI failures; else return."""
    if envelope and envelope.get("is_error"):
        message = str(envelope.get("result") or "").strip()
        status = envelope.get("api_error_status")
        if status in (401, 403):  # e.g. "Failed to authenticate. API Error: 401 API key is invalid."
            raise GlobalFatalError(f"API authentication/authorization failed (HTTP {status}): {message[:200]}")
        if message.startswith(GLOBAL_FATAL_MESSAGES):
            raise GlobalFatalError(message[:200])
        if message.startswith(JOB_FATAL_MESSAGES):
            raise JobFatalError(message[:200])
    first = ((stderr or "").strip().splitlines() or [""])[0]
    if AGENT_NOT_FOUND.match(first):
        raise GlobalFatalError(f"worker agent cannot be loaded: {first[:200]}")
    # Commander rejects the worker command line itself (flag / agent / option value): CLI config problem.
    if first.startswith(("error: unknown option", "error: option '")):
        raise GlobalFatalError(f"Claude CLI rejected the worker command: {first[:200]}")


def parse_worker_output(stdout):
    """Unwrap the CLI JSON envelope and parse the worker's ScreeningResult."""
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError:
        raise PipelineError("CLI output is not JSON")
    if not isinstance(envelope, dict) or envelope.get("is_error") or envelope.get("subtype") != "success":
        detail = envelope.get("subtype") if isinstance(envelope, dict) else "?"
        raise PipelineError(f"worker reported error ({detail})")
    text = (envelope.get("result") or "").strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.S)
    if fenced:
        text = fenced.group(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        raise PipelineError("invalid JSON in worker result")


# --------------------------------------------------------------------------
# Runtime control shared by worker threads and the controller
# --------------------------------------------------------------------------
# The controller sets stop_event on Ctrl+C / global fatal; screen_with_retry also sets it the
# moment it sees a GlobalFatalError, so no other thread launches another worker meanwhile.
stop_event = threading.Event()  # set: no new worker processes, no retries
_active = set()                 # running worker processes, terminated by terminate_active_workers()
_active_lock = threading.Lock()


def _usage_bucket():
    return {"calls": 0, "cost_usd": 0.0, "input_tokens": 0, "output_tokens": 0}


# Claude processes started / of which retries; usage split by first attempt vs retry. Under _active_lock.
metrics = {"invocations": 0, "retries": 0, "first": _usage_bucket(), "retry": _usage_bucket()}
_print_lock = threading.Lock()


def terminate_active_workers():
    """Terminate every running worker process (call after stop_event.set())."""
    with _active_lock:
        procs = list(_active)
    for proc in procs:
        try:
            proc.terminate()
        except OSError:
            pass


def log(*args, file=None):
    """print() that keeps lines whole when worker threads and the controller write at once."""
    with _print_lock:
        print(*args, file=file or sys.stdout)


def _record_usage(attempt, u):
    total_in = sum(u.get(k) or 0 for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    with _active_lock:
        b = metrics["first" if attempt == 1 else "retry"]
        b["calls"] += 1
        b["cost_usd"] += u.get("cost_usd") or 0
        b["input_tokens"] += total_in
        b["output_tokens"] += u.get("output_tokens") or 0


def screen_job(job, worker, timeout, attempt=1):
    """Run one fresh worker for one job; return a validated ScreeningResult."""
    single = worker.mode == WORKER_SINGLE_TURN
    prompt = worker.prompt(job)
    worker.pre_check(job, prompt)
    env = os.environ.copy()
    env.pop("CLAUDECODE", None)  # allow launching from inside a Claude Code terminal
    env.update(worker.env_extra)
    reserved = 0.0
    with _active_lock:
        if stop_event.is_set():
            if worker.budget and worker.budget.stop:
                raise BudgetExceeded(worker.budget.stop)
            raise StopRequested("interrupted")
        if worker.budget:
            reserved = worker.budget.reserve()
        try:
            proc = subprocess.Popen(worker.cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, cwd=worker.run_cwd(), env=env)
        except OSError as e:
            if worker.budget:
                worker.budget.settle(reserved, 0)
            raise GlobalFatalError(f"cannot start Claude CLI {worker.cmd[0]}: {e}")
        _active.add(proc)
        metrics["invocations"] += 1
        if attempt > 1:
            metrics["retries"] += 1
    cost = None
    try:
        try:
            stdout, stderr = proc.communicate(prompt, timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()  # like subprocess.run: don't wait on pipes a grandchild may still hold
            raise PipelineError(f"timeout after {timeout}s")
        finally:
            with _active_lock:
                _active.discard(proc)
            for pipe in (proc.stdin, proc.stdout, proc.stderr):
                if pipe:
                    pipe.close()
        envelope = load_envelope(stdout)
        if envelope:
            u = worker.post_check(envelope, prompt)  # single-turn guardrail violations raise GlobalFatalError
            cost = u.get("cost_usd") if isinstance(u.get("cost_usd"), (int, float)) else None
            _record_usage(attempt, u)
        classify_cli_failure(envelope, stderr)
        if proc.returncode != 0:
            # Prefer the CLI envelope's message over raw output. Transient (network / API): retryable.
            detail = str(envelope.get("result") or "") if envelope else ""
            if not detail:
                detail = ((stderr or stdout).strip().splitlines()[-1:] or ["no output"])[0]
            raise PipelineError(f"claude exited {proc.returncode}: {detail[:200]}")
        if single and (not envelope or envelope.get("is_error") or envelope.get("subtype") != "success"):
            detail = (envelope or {}).get("subtype") or "no envelope"
            raise PipelineError(f"worker reported error ({detail})")  # API-side error: retryable
        try:
            result = parse_worker_output(stdout)
            err = validate_result(result, job)
        except PipelineError as e:
            if not single:
                raise
            raise NonRetryableJobError(str(e))
        if err:
            raise (NonRetryableJobError if single else PipelineError)(err)
        return result
    finally:
        if worker.budget:
            worker.budget.settle(reserved, cost)


def screen_with_retry(job, worker, timeout):
    local = local_result(job)
    if local is not None:
        return local
    last = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return screen_job(job, worker, timeout, attempt)
        except GlobalFatalError:
            stop_event.set()  # immediately: no other thread may launch another worker
            raise
        except PipelineError as e:
            last = e
            if stop_event.is_set() or not e.retryable:
                break
            if attempt < MAX_ATTEMPTS:
                log(f"      {job['source_key']}: attempt {attempt} failed: {e} — retrying with a fresh worker")
    raise last


def warm_up(worker, queue, job_of, timeout):
    """Serial warm-up before a concurrent pool (02 and 04), single-turn mode only.

    Screens the first queued item on the caller's thread so the system-prompt cache exists before
    concurrent workers start. Returns (warm, rest): warm is (item, result, error) or None, rest the
    items still to submit to the pool (the warm-up item is never submitted again). Items in `queue`
    must all need a model call. A GlobalFatalError propagates: the caller must not start the pool.
    """
    if worker.mode != WORKER_SINGLE_TURN or not queue:
        return None, list(queue)
    item, rest = queue[0], list(queue[1:])
    log(f"warm-up: screening 1 job serially before starting the pool")
    try:
        result = screen_with_retry(job_of(item), worker, timeout)
    except GlobalFatalError:
        raise
    except BudgetExceeded:
        raise
    except PipelineError as e:
        return (item, None, e), rest
    except BaseException:
        stop_event.set()
        terminate_active_workers()
        raise
    return (item, result, None), rest


def usage_report(worker):
    """Run-metrics lines: first attempts and retries reported separately."""
    lines = []
    with _active_lock:
        f, r = dict(metrics["first"]), dict(metrics["retry"])
    for name, b in (("first attempts", f), ("retries", r)):
        lines.append(f"{name}: {b['calls']} call(s), ${b['cost_usd']:.4f}, input-related tokens "
                     f"{b['input_tokens']:,}, output tokens {b['output_tokens']:,}")
    m = worker.manifest()
    lines.append(f"worker mode: {m['worker_mode']}  model: {m['effective_model']}  cli: {m['claude_cli_version']}")
    if worker.budget:
        lines.append(f"budget: ${worker.budget.spent:.4f} of ${worker.budget.limit:.2f}"
                     + (f"  (STOPPED: {worker.budget.stop})" if worker.budget.stop else ""))
    return lines


def write_json_atomic(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
